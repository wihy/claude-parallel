"""
webcontent — WebContent 进程自动发现与采集。

WKWebView 的 JS 执行和 WebKit 渲染在独立的 WebContent 进程中运行，
xctrace --attach <App> 无法采集。本模块自动发现 WebContent PID，
以独立子进程运行 xctrace 采集，输出到 logs/webcontent_hotspots.jsonl。

由于 xctrace 单 slot 互斥，WebContent 采集与 App 的 sampling sidecar
不能同时运行。使用策略：交替采集（App cycle → WebContent cycle → ...）
或分两轮顺序采集。
"""

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Any, List, Optional

from .sampling import (
    export_xctrace_schema,
    parse_timeprofiler_xml,
    aggregate_top_n,
    HotspotSnapshot,
)
from .templates import build_xctrace_record_cmd, BUILTIN_TEMPLATES

logger = logging.getLogger(__name__)


def find_webcontent_pids(device_udid: str) -> List[Dict[str, Any]]:
    """
    在 iOS 设备上查找所有 WebKit 相关进程。

    Returns:
        [{"pid": int, "name": str, "path": str}, ...]
    """
    try:
        proc = subprocess.run(
            ["xcrun", "devicectl", "device", "info", "processes",
             "--device", device_udid],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return []
    except Exception:
        return []

    results = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # 格式: "1050   /path/to/com.apple.WebKit.WebContent"
        m = re.match(r"(\d+)\s+(.+)", line)
        if not m:
            continue
        pid = int(m.group(1))
        path = m.group(2).strip()
        name = path.rsplit("/", 1)[-1] if "/" in path else path

        if "WebKit.WebContent" in path:
            results.append({"pid": pid, "name": "WebContent", "path": path, "role": "js"})
        elif "WebKit.GPU" in path:
            results.append({"pid": pid, "name": "WebKit.GPU", "path": path, "role": "gpu"})
        elif "WebKit.Networking" in path:
            results.append({"pid": pid, "name": "WebKit.Networking", "path": path, "role": "network"})

    return results


class WebContentProfiler:
    """
    WebContent 进程 Time Profiler 采集 (带 PID 动态刷新)。

    以独立子进程运行 xctrace record，采集 WebContent 进程的
    JS/WebKit 热点函数。采集完成后导出解析到 webcontent_hotspots.jsonl。

    改进:
    - PID 动态刷新: 每隔 N 个 cycle 重新扫描 WebContent PID
    - PID 变化事件记录到 JSONL
    - WebContent 进程重建后自动迁移采集目标
    """

    # PID 刷新间隔 (每 N 个 cycle 检查一次)
    PID_REFRESH_INTERVAL = 5

    def __init__(
        self,
        session_root: Path,
        device_udid: str,
        interval_sec: int = 10,
        top_n: int = 15,
    ):
        self.session_root = Path(session_root)
        self.device_udid = device_udid
        self.interval_sec = interval_sec
        self.top_n = top_n

        self.logs_dir = self.session_root / "logs"
        self.hotspots_file = self.logs_dir / "webcontent_hotspots.jsonl"
        self.stderr_file = self.logs_dir / "webcontent.stderr"
        self._traces_tmp = self.session_root / "traces" / "_webcontent_tmp"
        self._exports_tmp = self.session_root / "exports" / "_webcontent_tmp"
        self._daemon_pid: int = 0
        self._pid_file = self.session_root / ".webcontent_daemon.pid"

    def start(self) -> Dict[str, Any]:
        """
        自动发现 WebContent PID 并启动采集。

        Returns:
            {"enabled": bool, "pid": int, "webcontent_pid": int, ...}
        """
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._traces_tmp.mkdir(parents=True, exist_ok=True)
        self._exports_tmp.mkdir(parents=True, exist_ok=True)

        # 清理旧 daemon
        self._cleanup_stale_daemon()

        # 发现 WebContent 进程
        wk_procs = find_webcontent_pids(self.device_udid)
        wc = next((p for p in wk_procs if p["role"] == "js"), None)

        if not wc:
            return {
                "enabled": False,
                "reason": "WebContent process not found (no active WKWebView?)",
                "hint": "Open a game in SoulApp first",
            }

        wc_pid = wc["pid"]

        # 启动 daemon 子进程 — 传入 PID_REFRESH_INTERVAL
        daemon_code = (
            "import sys, signal, json, time; "
            "from pathlib import Path; "
            "from src.perf.webcontent import _webcontent_cycle_loop; "
            f"_webcontent_cycle_loop("
            f"Path({str(self.session_root)!r}),"
            f"{self.device_udid!r},"
            f"{wc_pid},"
            f"{self.interval_sec},"
            f"{self.top_n},"
            f"{self.PID_REFRESH_INTERVAL})"
        )
        cmd = [sys.executable, "-c", daemon_code]

        log_f = open(self.stderr_file, "a", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            stdout=log_f,
            stderr=log_f,
            start_new_session=True,
        )
        log_f.close()
        self._daemon_pid = proc.pid
        self._pid_file.write_text(str(self._daemon_pid))

        # 同时记录所有 WebKit 进程信息
        gpu = next((p for p in wk_procs if p["role"] == "gpu"), None)

        return {
            "enabled": True,
            "daemon_pid": self._daemon_pid,
            "webcontent_pid": wc_pid,
            "gpu_pid": gpu["pid"] if gpu else None,
            "hotspots_file": str(self.hotspots_file),
            "all_webkit_processes": wk_procs,
            "pid_refresh_interval": self.PID_REFRESH_INTERVAL,
        }

    def stop(self):
        if self._daemon_pid:
            try:
                os.kill(self._daemon_pid, signal.SIGTERM)
                deadline = time.time() + 10
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
            self._daemon_pid = 0

        shutil.rmtree(self._traces_tmp, ignore_errors=True)
        shutil.rmtree(self._exports_tmp, ignore_errors=True)
        try:
            self._pid_file.unlink(missing_ok=True)
        except OSError:
            pass

    def _cleanup_stale_daemon(self):
        if not self._pid_file.exists():
            return
        try:
            old_pid = int(self._pid_file.read_text().strip())
            os.kill(old_pid, 0)
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


def _webcontent_cycle_loop(
    session_root: Path,
    device_udid: str,
    webcontent_pid: int,
    interval_sec: int,
    top_n: int,
    pid_refresh_interval: int = 5,
):
    """WebContent 采集 daemon 入口 (带 PID 动态刷新)。SIGTERM 退出。

    改进:
    - 每隔 pid_refresh_interval 个 cycle 重新扫描 WebContent PID
    - PID 变化时记录事件到 JSONL，并自动迁移到新 PID
    - 连续多次找不到 WebContent 进程时自动停止
    """
    import signal as _sig
    from concurrent.futures import ThreadPoolExecutor, Future

    running = True

    def _stop(*_):
        nonlocal running
        running = False

    _sig.signal(_sig.SIGTERM, _stop)

    logs_dir = session_root / "logs"
    traces_tmp = session_root / "traces" / "_webcontent_tmp"
    exports_tmp = session_root / "exports" / "_webcontent_tmp"
    hotspots_file = logs_dir / "webcontent_hotspots.jsonl"
    stderr_file = logs_dir / "webcontent.stderr"

    for d in (logs_dir, traces_tmp, exports_tmp):
        d.mkdir(parents=True, exist_ok=True)

    tpl = BUILTIN_TEMPLATES["time"]
    cycle = 0
    current_pid = webcontent_pid
    consecutive_failures = 0
    pid_not_found_count = 0
    pool = ThreadPoolExecutor(max_workers=1)
    pending: Optional[Future] = None

    def _log(msg):
        try:
            with open(stderr_file, "a") as f:
                f.write(f"{time.time():.0f} {msg}\n")
        except Exception:
            pass

    def _write_pid_event(event_type: str, old_pid: int, new_pid: int, detail: str = ""):
        """记录 PID 变化事件到 JSONL。"""
        try:
            import fcntl
            line = json.dumps({
                "ts": time.time(),
                "cycle": cycle,
                "event": event_type,
                "old_pid": old_pid,
                "new_pid": new_pid,
                "detail": detail,
            }, ensure_ascii=False)
            with open(hotspots_file, "a", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    f.write(line + "\n")
                    f.flush()
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
        except Exception:
            pass

    try:
        while running:
            cycle += 1

            # ── PID 刷新检查 ──
            if cycle > 1 and (cycle % pid_refresh_interval) == 0:
                _log(f"cycle {cycle}: 刷新 WebContent PID...")
                new_procs = find_webcontent_pids(device_udid)
                new_wc = next((p for p in new_procs if p["role"] == "js"), None)

                if new_wc:
                    pid_not_found_count = 0
                    if new_wc["pid"] != current_pid:
                        old_pid = current_pid
                        current_pid = new_wc["pid"]
                        _log(
                            f"cycle {cycle}: PID 变化 "
                            f"{old_pid} -> {current_pid}"
                        )
                        _write_pid_event(
                            "webcontent_pid_change",
                            old_pid=old_pid,
                            new_pid=current_pid,
                            detail=f"detected at cycle {cycle}",
                        )
                    else:
                        _log(f"cycle {cycle}: PID 未变 ({current_pid})")
                else:
                    pid_not_found_count += 1
                    _log(
                        f"cycle {cycle}: WebContent 进程未找到 "
                        f"(连续 {pid_not_found_count} 次)"
                    )
                    _write_pid_event(
                        "webcontent_pid_lost",
                        old_pid=current_pid,
                        new_pid=0,
                        detail=f"not found at cycle {cycle}, "
                               f"consecutive={pid_not_found_count}",
                    )
                    if pid_not_found_count >= 3:
                        _log("auto-stop: WebContent 进程连续 3 次未找到")
                        break
                    # 不退出，等下一个刷新周期再检查
                    consecutive_failures += 1
                    continue

            # ── 验证当前 PID 是否存活 ──
            if current_pid <= 0:
                _log(f"cycle {cycle}: 无有效 PID，跳过")
                consecutive_failures += 1
                continue

            trace_path = traces_tmp / f"wc_cycle_{cycle}.trace"

            # Record
            cmd = build_xctrace_record_cmd(
                template=tpl,
                device=device_udid,
                attach=str(current_pid),
                duration_sec=interval_sec,
                output_path=str(trace_path),
            )

            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                _, stderr = proc.communicate(timeout=interval_sec + 30)

                if stderr:
                    stderr_text = stderr.decode("utf-8", errors="replace").strip()
                    if stderr_text:
                        _log(f"cycle {cycle}: stderr={stderr_text[:200]}")
                    if "already recording" in stderr_text.lower():
                        consecutive_failures += 1
                        if consecutive_failures >= 5:
                            _log("auto-stop: xctrace slot occupied")
                            break
                        time.sleep(3)
                        continue
                    if "not found" in stderr_text.lower() or "does not exist" in stderr_text.lower():
                        # PID 对应的进程不存在
                        _log(f"cycle {cycle}: PID {current_pid} 不存在")
                        _write_pid_event(
                            "webcontent_pid_gone",
                            old_pid=current_pid,
                            new_pid=0,
                            detail=f"attach failed at cycle {cycle}",
                        )
                        consecutive_failures += 1
                        continue

                if proc.returncode != 0:
                    _log(f"cycle {cycle}: exit={proc.returncode}")
                    consecutive_failures += 1
                    if consecutive_failures >= 5:
                        break
                    continue

            except subprocess.TimeoutExpired:
                _log(f"cycle {cycle}: timeout")
                consecutive_failures += 1
                continue
            except Exception as e:
                _log(f"cycle {cycle}: record error: {e}")
                consecutive_failures += 1
                continue

            if not trace_path.exists():
                consecutive_failures += 1
                continue

            # Wait previous export
            if pending:
                try:
                    pending.result(timeout=30)
                except Exception:
                    pass

            # Submit export
            if running:
                pending = pool.submit(
                    _export_webcontent_cycle,
                    cycle, trace_path, exports_tmp, hotspots_file,
                    top_n, interval_sec, current_pid, _log,
                )

            consecutive_failures = 0

        # Wait last export
        if pending:
            try:
                pending.result(timeout=30)
            except Exception:
                pass
    finally:
        pool.shutdown(wait=False)


def _export_webcontent_cycle(
    cycle: int,
    trace_path: Path,
    exports_tmp: Path,
    hotspots_file: Path,
    top_n: int,
    interval_sec: int,
    current_pid: int,
    log_fn,
):
    """导出 + 解析 + 追加 JSONL。"""
    xml_path = exports_tmp / f"wc_cycle_{cycle}.xml"
    try:
        export_xctrace_schema(trace_path, "time-profile", xml_path)

        if not xml_path.exists() or xml_path.stat().st_size < 50:
            log_fn(f"cycle {cycle}: xml empty")
            return

        samples = parse_timeprofiler_xml(xml_path)
        if not samples:
            return

        top = aggregate_top_n(samples, top_n)
        total = sum(w for _, w in samples)

        line = json.dumps({
            "ts": time.time(),
            "cycle": cycle,
            "process": "WebContent",
            "pid": current_pid,
            "duration_s": interval_sec,
            "sample_count": int(total),
            "top": top,
        }, ensure_ascii=False)

        import fcntl
        with open(hotspots_file, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(line + "\n")
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    except Exception as e:
        log_fn(f"cycle {cycle}: export error: {e}")
    finally:
        try:
            if trace_path.is_dir():
                shutil.rmtree(trace_path, ignore_errors=True)
            elif trace_path.exists():
                trace_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            if xml_path.exists():
                xml_path.unlink(missing_ok=True)
        except OSError:
            pass


# ── Read/Format helpers ──


def read_webcontent_hotspots(
    hotspots_file: Path, last_n: int = 0,
) -> List[Dict[str, Any]]:
    if not hotspots_file.exists():
        return []
    lines = hotspots_file.read_text(encoding="utf-8").strip().splitlines()
    snaps = []
    for line in lines:
        try:
            snaps.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if last_n > 0:
        snaps = snaps[-last_n:]
    return snaps


def format_webcontent_hotspots(
    snapshots: List[Dict[str, Any]], top_n: int = 10,
) -> str:
    if not snapshots:
        return "  (无 WebContent 热点数据)"

    lines = []
    for snap in snapshots:
        # PID 变化事件
        if snap.get("event") in ("webcontent_pid_change", "webcontent_pid_lost", "webcontent_pid_gone"):
            evt = snap["event"]
            old_pid = snap.get("old_pid", "?")
            new_pid = snap.get("new_pid", "?")
            detail = snap.get("detail", "")
            ts = snap.get("ts", 0)
            ts_str = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "?"
            cycle = snap.get("cycle", "?")
            if evt == "webcontent_pid_change":
                lines.append(
                    f"  ── [PID 变化] Cycle {cycle} @ {ts_str}: "
                    f"PID {old_pid} -> {new_pid}  ({detail}) ──"
                )
            elif evt == "webcontent_pid_lost":
                lines.append(
                    f"  ── [PID 丢失] Cycle {cycle} @ {ts_str}: "
                    f"PID {old_pid} not found  ({detail}) ──"
                )
            else:
                lines.append(
                    f"  ── [PID 消失] Cycle {cycle} @ {ts_str}: "
                    f"PID {old_pid} gone  ({detail}) ──"
                )
            lines.append("")
            continue

        # 普通热点数据
        ts = snap.get("ts", 0)
        ts_str = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "?"
        cycle = snap.get("cycle", "?")
        samples = snap.get("sample_count", 0)
        pid = snap.get("pid", "?")
        lines.append(
            f"  ── WebContent Cycle {cycle} @ {ts_str} "
            f"(PID={pid}, {samples} samples, {snap.get('duration_s', '?')}s) ──"
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
