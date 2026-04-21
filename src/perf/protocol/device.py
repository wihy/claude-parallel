"""
device_metrics — 非 xctrace 通道的设备指标采集。

通过 ideviceinfo (libimobiledevice) 和 pymobiledevice3 sysmontap
采集电池功耗和 per-process CPU/内存指标，不占用 xctrace slot，
使 sampling sidecar (Time Profiler) 可以并行运行。

采集器:
- BatteryPoller:          ideviceinfo 周期轮询 → battery.jsonl
- ProcessMetricsStreamer: pymobiledevice3 dvt sysmon → process_metrics.jsonl
"""

import json
import logging
import os
import plistlib
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


# ── BatteryPoller ──


class BatteryPoller:
    """
    通过 ideviceinfo 周期轮询电池指标。

    不需要任何额外 setup，直接通过 USB/lockdownd 采集。
    输出 battery.jsonl，每行一个 JSON 对象。
    """

    def __init__(
        self,
        device_udid: str,
        interval_sec: float = 10.0,
        output_file: Optional[Path] = None,
    ):
        self.device_udid = device_udid
        self.interval_sec = interval_sec
        self.output_file = output_file
        self._daemon_pid: int = 0

    def start(self) -> int:
        """以独立子进程启动轮询，返回 PID。"""
        if self.output_file:
            self.output_file.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            sys.executable, "-c",
            f"from src.perf.protocol.device import _battery_poll_loop; "
            f"_battery_poll_loop("
            f"{self.device_udid!r}, {self.interval_sec}, "
            f"{str(self.output_file)!r})"
        ]
        stderr_path = (
            str(self.output_file).replace(".jsonl", ".stderr")
            if self.output_file else None
        )
        stderr_f = (
            open(stderr_path, "a", encoding="utf-8")
            if stderr_path else subprocess.DEVNULL
        )
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=stderr_f,
            start_new_session=True,
        )
        if hasattr(stderr_f, "close"):
            stderr_f.close()

        # 验证进程启动成功（等 1s 检查是否秒退）
        time.sleep(1)
        if proc.poll() is not None:
            logger.warning(
                "[battery] daemon exited immediately (rc=%d)",
                proc.returncode,
            )
            self._daemon_pid = 0
            return 0

        self._daemon_pid = proc.pid
        return proc.pid

    def stop(self):
        if self._daemon_pid:
            _kill_pid(self._daemon_pid)
            self._daemon_pid = 0


class ProcessMetricsStreamer:
    """
    通过 pymobiledevice3 dvt sysmon 采集 per-process 指标。

    需要 `sudo pymobiledevice3 remote tunneld` 在后台运行 (iOS 17+)。
    如果 tunneld 不可用，start() 返回 0 表示启动失败。
    """

    def __init__(
        self,
        device_udid: str,
        process_name: str,
        interval_ms: int = 1000,
        output_file: Optional[Path] = None,
    ):
        self.device_udid = device_udid
        self.process_name = process_name
        self.interval_ms = interval_ms
        self.output_file = output_file
        self._daemon_pid: int = 0

    @staticmethod
    def check_available() -> bool:
        """检查 pymobiledevice3 是否可用。"""
        try:
            import pymobiledevice3  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def check_tunneld_running() -> bool:
        """检查 tunneld 是否在运行。"""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "pymobiledevice3.*tunneld"],
                capture_output=True, timeout=3,
            )
            return result.returncode == 0
        except Exception as e:
            logger.debug("tunneld 进程检查失败: %s", e)
            return False

    def start(self) -> int:
        """以独立子进程启动流式采集，返回 PID。失败返回 0。"""
        if not self.check_available():
            logger.warning("[device_metrics] pymobiledevice3 not installed")
            return 0

        if self.output_file:
            self.output_file.parent.mkdir(parents=True, exist_ok=True)

        # 用 pymobiledevice3 CLI 子进程（避免 Python API 兼容性问题）
        cmd = [
            "pymobiledevice3", "developer", "dvt", "sysmon",
            "process", "monitor", "process",
            "--udid", self.device_udid,
            "--filter", f"name={self.process_name}",
            "--choose", "first",
            "--interval", str(self.interval_ms),
            "--output", str(self.output_file),
        ]

        stderr_path = str(self.output_file).replace(".jsonl", ".stderr")
        stderr_f = open(stderr_path, "a", encoding="utf-8")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=stderr_f,
                start_new_session=True,
            )
            stderr_f.close()

            # 等 2 秒检查是否立即退出（tunnel 不可用时会秒退）
            time.sleep(2)
            if proc.poll() is not None:
                logger.warning(
                    "[device_metrics] pymobiledevice3 sysmon exited "
                    "immediately (rc=%d). Is tunneld running? "
                    "Run: sudo pymobiledevice3 remote tunneld",
                    proc.returncode,
                )
                return 0

            self._daemon_pid = proc.pid
            return proc.pid

        except FileNotFoundError:
            stderr_f.close()
            logger.warning("[device_metrics] pymobiledevice3 CLI not found")
            return 0
        except Exception as e:
            stderr_f.close()
            logger.warning("[device_metrics] sysmon start failed: %s", e)
            return 0

    def stop(self):
        if self._daemon_pid:
            _kill_pid(self._daemon_pid)
            self._daemon_pid = 0


# ── Daemon loop for BatteryPoller (run as subprocess) ──


def _battery_poll_loop(
    device_udid: str, interval_sec: float, output_path: str,
):
    """BatteryPoller 子进程入口。SIGTERM 退出。"""
    import signal as _sig

    running = True

    def _stop(*_):
        nonlocal running
        running = False

    _sig.signal(_sig.SIGTERM, _stop)

    while running:
        try:
            data = _read_battery(device_udid)
            if data and output_path:
                line = json.dumps(data, ensure_ascii=False)
                with open(output_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                    f.flush()
        except Exception as e:
            sys.stderr.write(f"{time.time():.0f} battery poll error: {e}\n")

        # Interruptible sleep
        deadline = time.time() + interval_sec
        while running and time.time() < deadline:
            time.sleep(min(1.0, deadline - time.time()))


def _read_battery(device_udid: str) -> Optional[Dict[str, Any]]:
    """调用 ideviceinfo 读取电池指标。"""
    try:
        proc = subprocess.run(
            ["ideviceinfo", "-u", device_udid, "-q",
             "com.apple.mobile.battery", "--xml"],
            capture_output=True, timeout=10, text=True,
        )
        if proc.returncode != 0:
            return None

        plist = plistlib.loads(proc.stdout.encode("utf-8"))
        return {
            "ts": time.time(),
            "level_pct": plist.get("BatteryCurrentCapacity"),
            "is_charging": plist.get("BatteryIsCharging", False),
            "external_connected": plist.get("ExternalConnected", False),
            "fully_charged": plist.get("FullyCharged", False),
        }
    except Exception as e:
        logger.debug("电池数据采集失败: %s", e)
        return None


# ── JSONL readers ──


def read_battery_jsonl(
    path: Path, last_n: int = 0,
) -> List[Dict[str, Any]]:
    """读取 battery.jsonl。"""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if last_n > 0:
        records = records[-last_n:]
    return records


def read_process_metrics_jsonl(
    path: Path, last_n: int = 0,
) -> List[Dict[str, Any]]:
    """读取 process_metrics.jsonl (pymobiledevice3 输出)。"""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    records = []
    for line in lines:
        try:
            d = json.loads(line)
            records.append(d)
        except json.JSONDecodeError:
            continue
    if last_n > 0:
        records = records[-last_n:]
    return records


def format_battery_text(records: List[Dict[str, Any]]) -> str:
    """格式化电池数据。"""
    if not records:
        return "  (无电池数据)"
    lines = []
    for r in records:
        ts = r.get("ts", 0)
        ts_str = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "?"
        level = r.get("level_pct", "?")
        charging = "charging" if r.get("is_charging") else "discharging"
        ext = " [USB]" if r.get("external_connected") else ""
        lines.append(f"  {ts_str}  {level}%  {charging}{ext}")
    return "\n".join(lines)


def format_process_metrics_text(
    records: List[Dict[str, Any]], top_n: int = 5,
) -> str:
    """格式化 per-process 指标。"""
    if not records:
        return "  (无进程指标数据)"
    lines = []
    for r in records[-top_n:]:
        cpu = r.get("cpuUsage", r.get("cpu_usage", "?"))
        mem_bytes = r.get("physFootprint", r.get("phys_footprint", 0))
        mem_mb = mem_bytes / (1024 * 1024) if isinstance(mem_bytes, (int, float)) else "?"
        pid = r.get("pid", "?")
        name = r.get("name", "?")
        lines.append(f"  pid={pid}  {name}  CPU={cpu}%  MEM={mem_mb:.1f}MB")
    return "\n".join(lines)


# ── Shared helpers ──


def _kill_pid(pid: int, grace_seconds: float = 5.0):
    """SIGTERM → wait → SIGKILL."""
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception as e:
        logger.debug("发送 SIGTERM 失败 pid=%s: %s", pid, e)
        return

    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, Exception):
        pass
