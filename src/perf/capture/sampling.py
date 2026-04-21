"""
SamplingProfilerSidecar — xctrace Time Profiler 短周期旁路采集。

以固定间隔（默认 10s）循环录制 Time Profiler，每 cycle 即时导出聚合
Top-N 热点函数，追加到 logs/hotspots.jsonl。与主 xctrace 长录制通道
并行，只负责"运行时预览"。

同时包含 TimeProfiler XML 解析、聚合、格式化等共享函数，供
PerfSessionManager.callstack() 复用。
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from ..decode.templates import build_xctrace_record_cmd, BUILTIN_TEMPLATES

# 纯解析函数 (Time Profiler XML → samples → Top-N) 现已搬到 decode/ 层。
# 这里 re-export 以保持 `from src.perf.capture.sampling import ...` 的外部
# API 兼容 (tests 里的 patch("src.perf.capture.sampling.export_xctrace_schema")
# 依然有效,因为名字绑定在本模块上)。
from ..decode.timeprofiler import (  # noqa: F401 (re-export)
    aggregate_top_n,
    export_xctrace_schema,
    extract_mnemonic_value,
    parse_timeprofiler_xml,
)

logger = logging.getLogger(__name__)


def _coerce_addr_to_int(addr) -> Optional[int]:
    """把 entry.addr 标准化成 int (resolver.resolve_batch 的 key 类型)。

    支持:
      - int 直接返回
      - str hex (如 "0x100" / "100" / "abcdef") 尝试解析为 int
      - 其它情况返回 None (跳过 resolver 调用)
    """
    if isinstance(addr, int):
        return addr
    if isinstance(addr, str) and addr:
        try:
            return int(addr, 16)
        except ValueError:
            return None
    return None


def _enrich_top_with_resolver(top_entries: list, resolver) -> list:
    """在 aggregate_top_n 产出后批量符号化。

    - resolver 为 None 或 warmup 未完成时退化为 hex + source=unresolved
    - 无 addr 字段的 entry (旧格式/非地址型条目) 保留原 symbol,不打 source 标签
    - 每条 entry 原地更新 symbol + source (不重建 list,保留其它字段和顺序)
    - addr 兼容 int 或 hex 字符串 (parse_timeprofiler_xml 产出 string)

    resolver 必须是 SymbolResolver 或兼容 duck-type (需有 resolve_batch 方法)
    """
    if not top_entries:
        return top_entries

    # 收集能解析成 int 的 addr - 与 entry 一一对应
    pairs = []  # (entry, addr_int)
    for e in top_entries:
        addr_int = _coerce_addr_to_int(e.get("addr"))
        if addr_int is not None:
            pairs.append((e, addr_int))

    if resolver is None or not pairs:
        # 纯 hex 降级 - 只有带 addr 的 entry 才打 unresolved 标签
        for e, _ in pairs:
            e.setdefault("source", "unresolved")
        return top_entries

    addrs = [a for _, a in pairs]
    resolved = resolver.resolve_batch(addrs)
    for e, addr_int in pairs:
        sym = resolved.get(addr_int)
        if sym is not None:
            e["symbol"] = sym.name
            e["source"] = sym.source
        else:
            e["source"] = "unresolved"
    return top_entries


def aggregate_per_thread(
    samples: List[Tuple], top_threads: int = 10, top_funcs_per_thread: int = 3,
) -> List[Dict[str, Any]]:
    """按线程聚合 — 找出"哪条线程最忙 + 它在跑什么"。

    samples 应为 4 元组: (name, weight, addr, thread)
    旧格式 noop 返回空列表。
    """
    if not samples:
        return []
    # 跳过没 thread 维度的采样（旧格式）
    has_thread = any(len(s) >= 4 and s[3] for s in samples)
    if not has_thread:
        return []

    # thread → {total_weight, {func: weight}}
    thread_data: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"total": 0.0, "funcs": defaultdict(float)}
    )
    grand_total = 0.0
    for s in samples:
        if len(s) < 4:
            continue
        frame, weight, _addr, thread = s[0], s[1], s[2], s[3]
        if not thread:
            thread = "(unnamed)"
        leaf = frame.rsplit(" → ", 1)[-1]
        thread_data[thread]["total"] += weight
        thread_data[thread]["funcs"][leaf] += weight
        grand_total += weight

    if grand_total <= 0:
        return []

    sorted_threads = sorted(
        thread_data.items(), key=lambda kv: kv[1]["total"], reverse=True
    )[:top_threads]

    out = []
    for tname, d in sorted_threads:
        top_funcs = sorted(d["funcs"].items(), key=lambda x: x[1], reverse=True)
        out.append({
            "thread": tname,
            "weight": int(round(d["total"])),
            "pct": round(d["total"] / grand_total * 100.0, 1),
            "top_funcs": [
                {
                    "symbol": f,
                    "pct_in_thread": round(w / d["total"] * 100.0, 1),
                    "samples": int(round(w)),
                }
                for f, w in top_funcs[:top_funcs_per_thread]
            ],
        })
    return out


# ── JSONL read / format helpers ──


def read_hotspots_jsonl(
    hotspots_file: Path,
    last_n: int = 0,
    aggregate: bool = False,
) -> List[Dict[str, Any]]:
    """读取 hotspots.jsonl 并返回快照列表（加锁防止读写竞争）。"""
    if not hotspots_file.exists():
        return []

    import fcntl

    try:
        with open(hotspots_file, "r", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)  # 共享读锁
            try:
                lines = f.read().strip().splitlines()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        lines = hotspots_file.read_text(encoding="utf-8").strip().splitlines()
    snapshots = []
    for line in lines:
        try:
            snapshots.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if last_n > 0 and not aggregate:
        snapshots = snapshots[-last_n:]

    if aggregate and snapshots:
        func_weight: Dict[str, float] = defaultdict(float)
        total_samples = 0
        for snap in snapshots:
            for entry in snap.get("top", []):
                func_weight[entry["symbol"]] += entry.get("samples", 0)
                total_samples += entry.get("samples", 0)

        if total_samples > 0:
            hot = sorted(func_weight.items(), key=lambda x: x[1], reverse=True)
            agg_top = [
                {
                    "symbol": func,
                    "samples": int(round(w)),
                    "pct": round(w / total_samples * 100.0, 1),
                }
                for func, w in hot[:30]
            ]
            return [
                {
                    "aggregate": True,
                    "cycles": len(snapshots),
                    "total_samples": total_samples,
                    "top": agg_top,
                }
            ]

    return snapshots


def format_hotspots_text(
    snapshots: List[Dict[str, Any]], top_n: int = 10,
) -> str:
    """将热点快照格式化为可读文本。"""
    if not snapshots:
        return "  (无热点数据)"

    lines: List[str] = []
    for snap in snapshots:
        if snap.get("aggregate"):
            lines.append(
                f"  ── 全会话聚合 ({snap['cycles']} cycles, "
                f"{snap['total_samples']} samples) ──"
            )
        else:
            ts = snap.get("ts", 0)
            cycle = snap.get("cycle", "?")
            samples = snap.get("sample_count", 0)
            ts_str = (
                time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "?"
            )
            lines.append(
                f"  ── Cycle {cycle} @ {ts_str} "
                f"({samples} samples, {snap.get('duration_s', '?')}s) ──"
            )

        top = snap.get("top", [])[:top_n]
        if not top:
            lines.append("    (无采样)")
        else:
            max_sym = max(len(e["symbol"][:60]) for e in top)
            for i, e in enumerate(top):
                bar_len = int(e.get("pct", 0) / 2)
                bar = "█" * bar_len
                sym = e["symbol"][:60]
                lines.append(
                    f"  {i + 1:2d}. {sym:<{max_sym}}  "
                    f"{e.get('pct', 0):5.1f}%  "
                    f"({e.get('samples', 0)} samples)  {bar}"
                )
        lines.append("")

    return "\n".join(lines)


# ── Sidecar ──


@dataclass
class HotspotSnapshot:
    ts: float
    cycle: int
    duration_s: int
    sample_count: int
    top: List[Dict[str, Any]]
    per_thread: List[Dict[str, Any]] = field(default_factory=list)


class SamplingProfilerSidecar:
    """
    xctrace Time Profiler 短周期旁路采集。

    以 interval_sec 为周期循环录制，每 cycle 导出 → 解析 → 追加 JSONL。
    与主 xctrace 长录制通道并行，cycle trace 用完即删。
    """

    MIN_INTERVAL = 3
    MAX_CONSECUTIVE_FAILURES = 3

    def __init__(
        self,
        session_root: Path,
        device_udid: str,
        process: str,
        interval_sec: int = 10,
        top_n: int = 10,
        retention: int = 30,
        resolver=None,
    ):
        self.session_root = Path(session_root)
        self.device_udid = device_udid
        self.process = process
        self.top_n = top_n
        self.retention = retention
        self._resolver = resolver  # None 或 SymbolResolver - 让 cycle 批量符号化

        if interval_sec < self.MIN_INTERVAL:
            logger.warning(
                "sampling interval %ds < %ds, clamped",
                interval_sec,
                self.MIN_INTERVAL,
            )
            interval_sec = self.MIN_INTERVAL
        self.interval_sec = interval_sec

        self.logs_dir = self.session_root / "logs"
        self.hotspots_file = self.logs_dir / "hotspots.jsonl"
        self.stderr_file = self.logs_dir / "sampling.stderr"
        self._pid_file = self.session_root / ".sampling_daemon.pid"
        self._traces_tmp = self.session_root / "traces" / "_sampling_tmp"
        self._exports_tmp = self.session_root / "exports" / "_sampling_tmp"

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._cycle_count = 0
        self._current_proc: Optional[subprocess.Popen] = None
        self._proc_lock = threading.Lock()
        # subprocess-mode state (for standalone cpar perf start)
        self._daemon_proc: Optional[subprocess.Popen] = None
        self._daemon_pid: int = 0

    def start(self, as_subprocess: bool = True) -> bool:
        """
        启动旁路采集。

        Args:
            as_subprocess: True=独立子进程（cpar perf start 场景），
                          False=in-process 线程（Orchestrator 场景）。
        """
        if as_subprocess:
            return self._start_subprocess()
        return self._start_thread()

    def _start_subprocess(self) -> bool:
        """以独立子进程启动 sampling daemon，进程退出后仍运行。"""
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._traces_tmp.mkdir(parents=True, exist_ok=True)
        self._exports_tmp.mkdir(parents=True, exist_ok=True)

        # 清理上次残留的孤儿进程
        self._cleanup_stale_daemon()

        daemon_code = (
            "import sys, signal; "
            "from src.perf.sampling import SamplingProfilerSidecar; "
            "from pathlib import Path; "
            f"s=SamplingProfilerSidecar(Path({str(self.session_root)!r}),"
            f"{self.device_udid!r},{self.process!r},"
            f"interval_sec={self.interval_sec},"
            f"top_n={self.top_n},retention={self.retention}); "
            "signal.signal(signal.SIGTERM,"
            "lambda *_:(s._stop_event.set(),s._kill_current_proc())); "
            "s.logs_dir.mkdir(parents=True,exist_ok=True); "
            "s._traces_tmp.mkdir(parents=True,exist_ok=True); "
            "s._exports_tmp.mkdir(parents=True,exist_ok=True); "
            "s._cycle_loop()"
        )
        cmd = [sys.executable, "-c", daemon_code]
        log_f = open(self.stderr_file, "a", encoding="utf-8")
        self._daemon_proc = subprocess.Popen(
            cmd,
            stdout=log_f,
            stderr=log_f,
            start_new_session=True,  # 脱离父进程
        )
        log_f.close()
        self._daemon_pid = self._daemon_proc.pid

        # 持久化 PID 到文件，防止崩溃后孤儿进程
        self._pid_file.write_text(str(self._daemon_pid))

        return True

    def _start_thread(self) -> bool:
        """以 in-process 线程启动（Orchestrator 长运行场景）。"""
        if self._thread and self._thread.is_alive():
            return True

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._traces_tmp.mkdir(parents=True, exist_ok=True)
        self._exports_tmp.mkdir(parents=True, exist_ok=True)

        self._stop_event.clear()
        self._cycle_count = 0
        self._thread = threading.Thread(
            target=self._cycle_loop,
            name="sampling-profiler",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = 15.0) -> Dict[str, Any]:
        """停止旁路采集（兼容子进程和线程两种模式）。"""
        # 子进程模式
        if self._daemon_pid:
            try:
                os.kill(self._daemon_pid, signal.SIGTERM)
                # 等待退出
                deadline = time.time() + timeout
                while time.time() < deadline:
                    try:
                        os.kill(self._daemon_pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.3)
                else:
                    try:
                        os.kill(self._daemon_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            except ProcessLookupError:
                pass
            except Exception:
                pass
            self._daemon_pid = 0

        # 线程模式
        if self._thread:
            self._stop_event.set()
            self._kill_current_proc()
            self._thread.join(timeout=timeout)

        shutil.rmtree(self._traces_tmp, ignore_errors=True)
        shutil.rmtree(self._exports_tmp, ignore_errors=True)
        try:
            self._pid_file.unlink(missing_ok=True)
        except OSError:
            pass

        return {
            "cycles_completed": self._cycle_count,
            "hotspots_file": str(self.hotspots_file),
        }

    def is_alive(self) -> bool:
        if self._daemon_pid:
            try:
                os.kill(self._daemon_pid, 0)
                return True
            except ProcessLookupError:
                return False
        return self._thread is not None and self._thread.is_alive()

    # ── internal ──

    def _cycle_loop(self):
        """Pipeline cycle loop: record 与上一轮 export+parse 重叠执行。"""
        from concurrent.futures import ThreadPoolExecutor, Future

        consecutive_failures = 0
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="export")
        pending_future: Optional[Future] = None

        try:
            while not self._stop_event.is_set():
                cycle_num = self._cycle_count + 1

                # Phase 1: record（阻塞，等 xctrace 完成）
                trace_path = self._record_phase(cycle_num)

                # 等上一轮 export 完成（如果有）
                if pending_future:
                    try:
                        result = pending_future.result(timeout=30)
                        if result:
                            consecutive_failures = 0
                        else:
                            consecutive_failures += 1
                    except Exception as e:
                        consecutive_failures += 1
                        self._log_error(f"export future exception: {e}")

                if consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                    self._log_error(
                        f"auto-stop: {consecutive_failures} consecutive failures"
                    )
                    break

                # Phase 2: 提交本轮 export（非阻塞，后台执行）
                if trace_path and not self._stop_event.is_set():
                    pending_future = pool.submit(
                        self._export_and_append, cycle_num, trace_path,
                    )
                else:
                    consecutive_failures += 1
                    pending_future = None

                self._cycle_count = cycle_num

            # 等最后一轮 export
            if pending_future:
                try:
                    pending_future.result(timeout=30)
                except Exception:
                    pass
        finally:
            pool.shutdown(wait=False)

    def _record_phase(self, cycle_num: int) -> Optional[Path]:
        """只做 xctrace record，返回 trace_path 或 None。"""
        if self._stop_event.is_set():
            return None

        trace_path = self._traces_tmp / f"cycle_{cycle_num}.trace"
        tpl = BUILTIN_TEMPLATES["time"]
        cmd = build_xctrace_record_cmd(
            template=tpl,
            device=self.device_udid,
            attach=self.process,
            duration_sec=self.interval_sec,
            output_path=str(trace_path),
        )

        rc = self._record(cmd, cycle_num)
        if rc is None or self._stop_event.is_set():
            self._cleanup_path(trace_path)
            return None

        if not trace_path.exists():
            self._log_error(f"cycle {cycle_num}: trace not found at {trace_path}")
            return None

        return trace_path

    def _export_and_append(
        self, cycle_num: int, trace_path: Path,
    ) -> bool:
        """后台线程：export → parse → aggregate → append JSONL → cleanup。"""
        xml_path = self._exports_tmp / f"cycle_{cycle_num}.xml"
        try:
            export_xctrace_schema(trace_path, "time-profile", xml_path)

            if not xml_path.exists() or xml_path.stat().st_size < 50:
                self._log_error(f"cycle {cycle_num}: xml empty or missing")
                return False

            samples = parse_timeprofiler_xml(xml_path)
            if not samples:
                return False

            top = aggregate_top_n(samples, self.top_n)
            # 批量符号化 hex addr → 业务函数名 + source 标签
            top = _enrich_top_with_resolver(top, self._resolver)
            # 兼容 2/3/4 元组: weight 总在 index 1
            total = sum(s[1] for s in samples)
            # per-thread 聚合 (4 元组才有效, 旧格式自动 noop)
            per_thread = aggregate_per_thread(samples, top_threads=10, top_funcs_per_thread=3)

            snapshot = HotspotSnapshot(
                ts=time.time(),
                cycle=cycle_num,
                duration_s=self.interval_sec,
                sample_count=int(total),
                top=top,
                per_thread=per_thread,
            )
            self._append_snapshot(snapshot)
            self._rotate_if_needed()
            return True

        except Exception as e:
            self._log_error(f"cycle {cycle_num}: export_and_append failed — {e}")
            return False
        finally:
            self._cleanup_path(trace_path)
            self._cleanup_path(xml_path)

    def _run_one_cycle(
        self, cycle_num: int,
    ) -> Optional[HotspotSnapshot]:
        if self._stop_event.is_set():
            return None

        trace_path = self._traces_tmp / f"cycle_{cycle_num}.trace"
        xml_path = self._exports_tmp / f"cycle_{cycle_num}.xml"

        tpl = BUILTIN_TEMPLATES["time"]
        cmd = build_xctrace_record_cmd(
            template=tpl,
            device=self.device_udid,
            attach=self.process,
            duration_sec=self.interval_sec,
            output_path=str(trace_path),
        )

        rc = self._record(cmd, cycle_num)
        if rc is None or self._stop_event.is_set():
            self._cleanup_path(trace_path)
            return None

        if not trace_path.exists():
            self._log_error(f"cycle {cycle_num}: trace not found at {trace_path}")
            return None

        try:
            export_xctrace_schema(trace_path, "time-profile", xml_path)
        except Exception as e:
            self._log_error(f"cycle {cycle_num}: export failed — {e}")
            self._cleanup_path(trace_path)
            return None

        if not xml_path.exists() or xml_path.stat().st_size < 50:
            self._log_error(f"cycle {cycle_num}: xml empty or missing")
            self._cleanup_path(trace_path)
            return None

        samples = parse_timeprofiler_xml(xml_path)

        self._cleanup_path(trace_path)
        self._cleanup_path(xml_path)

        if not samples:
            return None

        top = aggregate_top_n(samples, self.top_n)
        total = sum(s[1] for s in samples)
        per_thread = aggregate_per_thread(samples, top_threads=10, top_funcs_per_thread=3)

        return HotspotSnapshot(
            ts=time.time(),
            cycle=cycle_num,
            duration_s=self.interval_sec,
            sample_count=int(total),
            top=top,
            per_thread=per_thread,
        )

    def _record(self, cmd: List[str], cycle_num: int) -> Optional[int]:
        """执行 xctrace record, 返回 returncode 或 None（失败）。"""
        try:
            with self._proc_lock:
                if self._stop_event.is_set():
                    return None
                self._current_proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            proc = self._current_proc

            _, stderr_bytes = proc.communicate(
                timeout=self.interval_sec + 30,
            )

            with self._proc_lock:
                self._current_proc = None

            if stderr_bytes:
                stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
                if stderr_text:
                    # 记录所有 stderr 内容
                    self._log_error(
                        f"cycle {cycle_num}: stderr={stderr_text[:300]}"
                    )
                    if "already recording" in stderr_text.lower():
                        return None

            if proc.returncode != 0:
                self._log_error(
                    f"cycle {cycle_num}: xctrace exit={proc.returncode}"
                )
            return proc.returncode

        except subprocess.TimeoutExpired:
            self._kill_current_proc()
            self._log_error(f"cycle {cycle_num}: xctrace timed out")
            return None
        except Exception as e:
            self._log_error(f"cycle {cycle_num}: record failed — {e}")
            return None

    def _append_snapshot(self, snapshot: HotspotSnapshot):
        line = json.dumps(
            {
                "ts": snapshot.ts,
                "cycle": snapshot.cycle,
                "duration_s": snapshot.duration_s,
                "sample_count": snapshot.sample_count,
                "top": snapshot.top,
            },
            ensure_ascii=False,
        )
        try:
            import fcntl

            with open(self.hotspots_file, "a", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.write(line + "\n")
                    f.flush()
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
        except Exception as e:
            self._log_error(f"jsonl write failed: {e}")

    def _rotate_if_needed(self):
        if not self.hotspots_file.exists():
            return
        try:
            import fcntl
            import tempfile

            with open(self.hotspots_file, "r+", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    lines = f.read().strip().splitlines()
                    if len(lines) > self.retention:
                        keep = lines[-self.retention :]
                        # 原子写：先写 tmp 再 rename
                        tmp_fd, tmp_path = tempfile.mkstemp(
                            dir=str(self.hotspots_file.parent),
                            prefix=".hotspots.",
                        )
                        try:
                            with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_f:
                                tmp_f.write("\n".join(keep) + "\n")
                            os.replace(tmp_path, self.hotspots_file)
                        except Exception:
                            try:
                                os.unlink(tmp_path)
                            except OSError:
                                pass
                            raise
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
        except Exception:
            pass

    def _kill_current_proc(self):
        with self._proc_lock:
            proc = self._current_proc
            if proc is None:
                return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass

    def _cleanup_stale_daemon(self):
        """启动前检查并清理上次残留的 daemon 进程。"""
        if not self._pid_file.exists():
            return
        try:
            old_pid = int(self._pid_file.read_text().strip())
            os.kill(old_pid, 0)  # 检查进程是否存活
            logger.warning(
                "[sampling] killing stale daemon pid=%d", old_pid,
            )
            os.kill(old_pid, signal.SIGTERM)
            time.sleep(1)
            try:
                os.kill(old_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except (ProcessLookupError, ValueError):
            pass
        finally:
            try:
                self._pid_file.unlink(missing_ok=True)
            except OSError:
                pass

    def _cleanup_path(self, path: Path):
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink(missing_ok=True)
        except OSError:
            pass

    def _log_error(self, msg: str):
        logger.warning("[sampling] %s", msg)
        try:
            with open(self.stderr_file, "a", encoding="utf-8") as f:
                f.write(f"{time.time():.0f} {msg}\n")
        except Exception:
            pass


# ── Subprocess daemon entry point ──


def _run_daemon():
    """当以 python -m src.perf.sampling 启动时，运行 cycle loop 直到 SIGTERM。"""
    import argparse as _ap

    p = _ap.ArgumentParser()
    p.add_argument("--session-root", required=True)
    p.add_argument("--device", required=True)
    p.add_argument("--process", required=True)
    p.add_argument("--interval", type=int, default=10)
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--retention", type=int, default=30)
    args = p.parse_args()

    sidecar = SamplingProfilerSidecar(
        session_root=Path(args.session_root),
        device_udid=args.device,
        process=args.process,
        interval_sec=args.interval,
        top_n=args.top_n,
        retention=args.retention,
    )

    # SIGTERM → graceful shutdown
    def _on_sigterm(signum, frame):
        sidecar._stop_event.set()
        sidecar._kill_current_proc()

    signal.signal(signal.SIGTERM, _on_sigterm)

    # Run cycle loop in-process (blocking)
    sidecar.logs_dir.mkdir(parents=True, exist_ok=True)
    sidecar._traces_tmp.mkdir(parents=True, exist_ok=True)
    sidecar._exports_tmp.mkdir(parents=True, exist_ok=True)
    sidecar._cycle_loop()

    # Cleanup
    shutil.rmtree(sidecar._traces_tmp, ignore_errors=True)
    shutil.rmtree(sidecar._exports_tmp, ignore_errors=True)


if __name__ == "__main__":
    _run_daemon()
