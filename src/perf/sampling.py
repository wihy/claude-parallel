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
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from .templates import build_xctrace_record_cmd, BUILTIN_TEMPLATES

logger = logging.getLogger(__name__)


# ── Shared xctrace export / parse helpers ──


def export_xctrace_schema(trace_file: Path, schema: str, output: Path):
    """调用 xcrun xctrace export 导出指定 schema 的 XML。"""
    cmd = [
        "xcrun", "xctrace", "export",
        "--input", str(trace_file),
        "--xpath", f'/trace-toc/run/data/table[@schema="{schema}"]',
        "--output", str(output),
    ]
    subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False, text=True,
    )


def extract_mnemonic_value(
    row_text: str, mnemonic: str, default: str = "",
) -> str:
    """从 xctrace XML row 中提取指定 mnemonic 的 fmt 值。"""
    patterns = [
        re.compile(rf'<{re.escape(mnemonic)}[^>]*fmt="([^"]*)"', re.S),
        re.compile(
            rf'<{re.escape(mnemonic)}[^>]*>(.*?)</{re.escape(mnemonic)}>',
            re.S,
        ),
    ]
    for pat in patterns:
        m = pat.search(row_text)
        if m:
            val = m.group(1).strip()
            if val:
                return val
    return default


def parse_timeprofiler_xml(
    xml_path: Path,
    keep_full_stack: bool = False,
    time_range: Optional[Tuple[float, float]] = None,
) -> List[Any]:
    """
    解析 xctrace export 的 Time Profiler XML。

    自动检测两种格式:
    - Xcode 16+: schema="time-profile", 含 <backtrace><frame name="...">
    - Legacy: schema="TimeProfiler", 含 <symbol-name>/<sample-count>

    Args:
        keep_full_stack: True 返回完整调用链 dict，False 返回 (leaf, weight) tuple
        time_range: (from_sec, to_sec) 只保留此时间段内的采样

    Returns:
        keep_full_stack=False: [(frame_string, weight), ...]
        keep_full_stack=True:  [{"ts_offset_ms": float, "stack": [...], "weight": float}, ...]
    """
    if not xml_path.exists():
        return []

    # 快速检测格式（只读前 2KB）
    try:
        with open(xml_path, "r", errors="replace") as f:
            head = f.read(2048)
    except Exception:
        return []

    if "<backtrace" in head or "<tagged-backtrace" in head:
        return _parse_time_profile_iterparse(xml_path, keep_full_stack, time_range)

    # Legacy 格式回退到正则（文件通常很小）
    try:
        text = xml_path.read_text(errors="replace")
    except Exception:
        return []
    return _parse_legacy_timeprofiler_format(text)


def _parse_weight_ms(fmt_str: str) -> float:
    """Parse weight fmt like '1.00 ms' / '500 µs' / '2.00 s' → ms."""
    try:
        parts = fmt_str.strip().split()
        val = float(parts[0])
        if len(parts) > 1:
            unit = parts[1].lower()
            if "µ" in unit or unit == "us":
                val /= 1000.0
            elif unit == "s":
                val *= 1000.0
        return val
    except (ValueError, IndexError):
        return 1.0


def _parse_sample_time_sec(fmt_str: str) -> float:
    """Parse sample-time fmt like '00:05.123.456' → 5.123 seconds."""
    try:
        parts = fmt_str.split(":")
        if len(parts) == 2:
            mins = int(parts[0])
            sec_parts = parts[1].split(".")
            secs = int(sec_parts[0])
            ms = int(sec_parts[1]) if len(sec_parts) > 1 else 0
            return mins * 60 + secs + ms / 1000.0
        return float(fmt_str)
    except (ValueError, IndexError):
        return 0.0


def _is_symbolicated(name: str) -> bool:
    """判断 frame name 是否已符号化。"""
    return bool(
        name
        and not name.startswith("0x")
        and name != "?"
        and not name.startswith("<")
    )


def _parse_time_profile_iterparse(
    xml_path: Path,
    keep_full_stack: bool = False,
    time_range: Optional[Tuple[float, float]] = None,
) -> List[Any]:
    """
    单遍 iterparse 解析 Xcode 16+ time-profile XML。
    内存恒定（每行处理后 clear）。
    """
    from xml.etree.ElementTree import iterparse

    # id → value 映射（渐进构建）
    # frame_map 存 (name, addr) 二元组, addr 用于后续 LinkMap 反查
    frame_map: Dict[str, Tuple[str, str]] = {}  # frame id → (name, addr)
    weight_map: Dict[str, float] = {}           # weight id → ms
    bt_map: Dict[str, List[Tuple[str, str]]] = {}  # tagged-backtrace id → [(name, addr), ...]

    samples: List[Any] = []
    # 当前 row 的临时状态
    in_row = False
    row_weight: float = 1.0
    row_frames: List[Tuple[str, str]] = []   # (name, addr)
    row_ts: float = 0.0
    current_bt_id: Optional[str] = None
    current_bt_frames: List[Tuple[str, str]] = []
    in_backtrace = False

    try:
        for event, elem in iterparse(str(xml_path), events=("start", "end")):
            tag = elem.tag

            if event == "start":
                if tag == "row":
                    in_row = True
                    row_weight = 1.0
                    row_frames = []
                    row_ts = 0.0
                elif tag == "tagged-backtrace" and in_row:
                    bt_id = elem.get("id")
                    bt_ref = elem.get("ref")
                    if bt_ref and bt_ref in bt_map:
                        row_frames = list(bt_map[bt_ref])
                    elif bt_id:
                        current_bt_id = bt_id
                        current_bt_frames = []
                        in_backtrace = True
                elif tag == "backtrace":
                    pass  # 进入 backtrace 容器
                continue

            # event == "end"
            if tag == "frame":
                fid = elem.get("id")
                fref = elem.get("ref")
                name = elem.get("name", "")
                # 提取地址 (xctrace XML 通常用 addr 属性, 兼容 address)
                addr = elem.get("addr") or elem.get("address") or ""
                if fid and (name or addr):
                    frame_map[fid] = (name, addr)
                if fref:
                    name, addr = frame_map.get(fref, ("", ""))
                if in_backtrace and (name or addr):
                    current_bt_frames.append((name, addr))

            elif tag == "weight":
                wid = elem.get("id")
                wref = elem.get("ref")
                fmt = elem.get("fmt", "")
                if wid and fmt:
                    weight_map[wid] = _parse_weight_ms(fmt)
                if in_row:
                    if wid:
                        row_weight = weight_map.get(wid, 1.0)
                    elif wref:
                        row_weight = weight_map.get(wref, 1.0)

            elif tag == "sample-time":
                if in_row:
                    fmt = elem.get("fmt", "")
                    if fmt:
                        row_ts = _parse_sample_time_sec(fmt)

            elif tag == "tagged-backtrace":
                if in_backtrace and current_bt_id:
                    bt_map[current_bt_id] = current_bt_frames
                    if in_row and not row_frames:
                        row_frames = list(current_bt_frames)
                in_backtrace = False
                current_bt_id = None

            elif tag == "row":
                in_row = False
                if row_frames:
                    # 时间段过滤
                    if time_range:
                        from_s, to_s = time_range
                        if row_ts < from_s or row_ts > to_s:
                            elem.clear()
                            continue

                    if keep_full_stack:
                        # 过滤出符号化的 frames - 保留 (name, addr)
                        sym_frames = [
                            {"name": n, "addr": a} for (n, a) in row_frames
                            if _is_symbolicated(n) or a
                        ]
                        if sym_frames:
                            samples.append({
                                "ts_offset_s": round(row_ts, 3),
                                "stack": sym_frames,
                                "weight": row_weight,
                            })
                    else:
                        # 只取叶子（第一个符号化 frame）
                        leaf_name, leaf_addr = "", ""
                        for (n, a) in row_frames:
                            if _is_symbolicated(n):
                                leaf_name, leaf_addr = n, a
                                break
                            elif a:  # 未符号化但有 addr - 保留以便 LinkMap 反查
                                leaf_name, leaf_addr = n or f"<0x{a}>", a
                                break
                        if leaf_name:
                            samples.append((leaf_name, row_weight, leaf_addr))

                elem.clear()  # 释放内存

    except Exception:
        pass

    return samples


def _parse_legacy_timeprofiler_format(text: str) -> List[Tuple[str, float]]:
    """Parse legacy TimeProfiler schema with symbol-name/sample-count columns."""
    col_names = [
        m.group(1) for m in re.finditer(r"<col><name>([^<]+)</name>", text)
    ]
    if not col_names:
        return []

    col_idx = {name.lower(): i for i, name in enumerate(col_names)}
    symbol_idx = col_idx.get(
        "symbol name",
        col_idx.get("symbol", col_idx.get("name", -1)),
    )
    weight_idx = col_idx.get(
        "sample count",
        col_idx.get("weight", col_idx.get("count", -1)),
    )

    if symbol_idx == -1:
        return []

    row_pat = re.compile(r"<row>(.*?)</row>", re.S)
    cell_pat = re.compile(r"<c[^>]*>(.*?)</c>", re.S)
    tag_strip = re.compile(r"<[^>]+/>")
    samples: List[Tuple[str, float]] = []

    for row_m in row_pat.finditer(text):
        row_text = row_m.group(1)

        symbol = extract_mnemonic_value(
            row_text,
            "symbol-name",
            extract_mnemonic_value(row_text, "symbol", ""),
        )
        if not symbol:
            symbol = extract_mnemonic_value(row_text, "name", "")

        if not symbol:
            cells = cell_pat.findall(row_text)
            if symbol_idx < len(cells):
                symbol = tag_strip.sub("", cells[symbol_idx]).strip()

        if not symbol or symbol == "?":
            continue

        weight = 1.0
        if weight_idx != -1:
            w_str = extract_mnemonic_value(
                row_text,
                "sample-count",
                extract_mnemonic_value(
                    row_text,
                    "weight",
                    extract_mnemonic_value(row_text, "count", ""),
                ),
            )
            if w_str:
                try:
                    weight = float(re.sub(r"[^\d.]", "", w_str.split()[0]))
                except (ValueError, IndexError):
                    weight = 1.0

        caller = extract_mnemonic_value(row_text, "caller", "")
        frame = f"{caller} → {symbol}" if caller else symbol
        samples.append((frame, weight))

    return samples


def aggregate_top_n(
    samples: List[Tuple], top_n: int,
) -> List[Dict[str, Any]]:
    """将原始采样聚合为 Top-N 热点函数（取 leaf 符号）。

    samples 可为:
      [(name, weight)]              旧格式，向后兼容
      [(name, weight, addr)]        新格式，含原始地址用于 LinkMap 反查
    """
    if not samples:
        return []

    # (leaf_name, addr) → weight
    # 保留 addr 让后续 resymbolize 阶段能 LinkMap 反查
    bucket: Dict[Tuple[str, str], float] = defaultdict(float)
    for s in samples:
        if len(s) >= 3:
            frame, weight, addr = s[0], s[1], s[2]
        else:
            frame, weight = s[0], s[1]
            addr = ""
        leaf = frame.rsplit(" → ", 1)[-1]
        bucket[(leaf, addr)] += weight

    total = sum(bucket.values())
    if total <= 0:
        return []

    hot = sorted(bucket.items(), key=lambda x: x[1], reverse=True)
    out = []
    for (func, addr), w in hot[:top_n]:
        item = {
            "symbol": func,
            "samples": int(round(w)),
            "pct": round(w / total * 100.0, 1),
        }
        if addr:
            item["addr"] = addr
        out.append(item)
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
    ):
        self.session_root = Path(session_root)
        self.device_udid = device_udid
        self.process = process
        self.top_n = top_n
        self.retention = retention

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
            total = sum(w for _, w in samples)

            snapshot = HotspotSnapshot(
                ts=time.time(),
                cycle=cycle_num,
                duration_s=self.interval_sec,
                sample_count=int(total),
                top=top,
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
        total = sum(w for _, w in samples)

        return HotspotSnapshot(
            ts=time.time(),
            cycle=cycle_num,
            duration_s=self.interval_sec,
            sample_count=int(total),
            top=top,
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
