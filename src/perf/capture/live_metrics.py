"""
LiveMetricsStreamer — 边录边导出 xctrace 指标的实时流式引擎。

工作方式:
1. xctrace record 后台录制中
2. 定期执行 xctrace export 导出增量 XML 数据
3. 解析 XML 提取: CPU%、Display 功耗、Networking 功耗、GPU 帧率、内存
4. 滚动窗口计算 avg/peak/jitter
5. 超阈值自动告警
6. 输出时序快照序列 (JSONL)
"""

import json
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, List, Any, Callable


# ── 数据结构 ──

@dataclass
class MetricSnapshot:
    """单次指标快照"""
    ts: float                       # 采集时间戳
    display_mw: Optional[float] = None    # Display 功耗 (mW)
    cpu_mw: Optional[float] = None        # CPU 功耗 (mW)
    networking_mw: Optional[float] = None # Networking 功耗 (mW)
    cpu_pct: Optional[float] = None       # CPU 利用率 (%)
    gpu_fps: Optional[float] = None       # GPU 帧率 (fps)
    mem_mb: Optional[float] = None        # 内存使用 (MB)
    battery_pct: Optional[float] = None   # 电池电量 (%)
    thermal_state: Optional[str] = None   # 热状态 (Nominal/Fair/Serious/Critical)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "display_mw": self.display_mw,
            "cpu_mw": self.cpu_mw,
            "networking_mw": self.networking_mw,
            "cpu_pct": self.cpu_pct,
            "gpu_fps": self.gpu_fps,
            "mem_mb": self.mem_mb,
            "battery_pct": self.battery_pct,
            "thermal_state": self.thermal_state,
        }


@dataclass
class MetricThreshold:
    """单指标告警阈值"""
    name: str           # 指标名
    field: str          # MetricSnapshot 字段名
    max_value: float    # 超过此值告警
    level: str = "warn" # 告警级别


DEFAULT_THRESHOLDS = [
    MetricThreshold("display_high", "display_mw", 800.0, "warn"),
    MetricThreshold("display_critical", "display_mw", 1200.0, "critical"),
    MetricThreshold("cpu_high", "cpu_mw", 300.0, "warn"),
    MetricThreshold("cpu_pct_high", "cpu_pct", 80.0, "warn"),
    MetricThreshold("networking_high", "networking_mw", 100.0, "warn"),
    MetricThreshold("gpu_low_fps", "gpu_fps", 30.0, "warn"),
    MetricThreshold("mem_high", "mem_mb", 1500.0, "warn"),
]


# ── 指标提取器 ──

# xctrace export XML 中常见的 schema 和对应列名
SCHEMA_COLUMNS = {
    # Power Profiler 模板
    "SystemPowerLevel": {
        "Display": "display_mw",
        "CPU": "cpu_mw",
        "Networking": "networking_mw",
    },
    "ProcessSubsystemPowerImpact": {
        "CPU": "cpu_mw",
        "Networking": "networking_mw",
    },
    # Legacy schema names (Xcode < 16)
    "CPUCore": {
        "CPU Total": "cpu_pct",
    },
    "CoreAnimationFPS": {
        "FPS": "gpu_fps",
    },
    "ProcessMemory": {
        "Physical Memory": "mem_mb",
    },
    # System Trace 模板 (Xcode 16+ 真机实际 schema)
    "system-load": {
        "Load": "cpu_pct",
    },
    "device-thermal-state-intervals": {
        "Thermal State": "_thermal_state",
    },
    # ── Composite 模式额外 schema ──
    "time-profile": {
        "CPU Time": "cpu_time_ms",
    },
    "TimeProfiler": {
        "Weight": "cpu_weight",
    },
    "networking": {
        "Bytes In": "net_bytes_in",
        "Bytes Out": "net_bytes_out",
        "Connections": "net_connections",
    },
    "GPU": {
        "GPU Time": "gpu_time_ms",
        "Vertex Fetch": "gpu_vertex_fetch",
    },
    "cpu-track": {
        "CPU": "cpu_pct",
    },
    "physical-memory-track": {
        "Physical Memory": "mem_mb",
    },
}


# ── 预编译正则 ──
_RE_COL_NAME_ATTR = re.compile(r'<col[^>]*name="([^"]+)"')
_RE_COL_NAME_TAG = re.compile(r'<col>\s*(?:<mnemonic>[^<]*</mnemonic>\s*)?<name>([^<]+)</name>')
_RE_ROW = re.compile(r"<row>(.*?)</row>", re.S)
_RE_CELL = re.compile(r"<c[^>]*>(.*?)</c>", re.S)
_RE_FMT = re.compile(r'fmt="([^"]*)"')


def _parse_fmt_number(fmt_str: str) -> float:
    """安全解析 fmt 字符串为数值。'570 mW' → 570, '-1.5 %' → -1.5, '1.00 ms' → 1.0"""
    parts = fmt_str.strip().split()
    if not parts:
        raise ValueError("empty fmt")
    # 取第一段，保留负号和小数点
    token = parts[0]
    # 移除非数字字符但保留开头的负号和小数点
    cleaned = ""
    for j, ch in enumerate(token):
        if ch in "0123456789.":
            cleaned += ch
        elif ch == "-" and j == 0:
            cleaned += ch
    if not cleaned:
        raise ValueError(f"no number in: {fmt_str}")
    return float(cleaned)


def _parse_exported_xml(xml_path: Path) -> Dict[str, List[float]]:
    """
    解析 xctrace export 输出的 XML 文件，提取所有数值列。
    返回 {列名: [值列表]}

    优先使用 iterparse 流式解析（内存恒定），fallback 到正则。
    """
    if not xml_path.exists():
        return {}

    # 尝试 iterparse
    try:
        return _parse_xml_iterparse(xml_path)
    except Exception:
        pass

    # Fallback: 正则解析
    try:
        text = xml_path.read_text(errors="replace")
    except Exception:
        return {}

    return _parse_xml_regex(text)


def _parse_xml_iterparse(xml_path: Path) -> Dict[str, List[float]]:
    """iterparse 流式解析指标 XML。"""
    from xml.etree.ElementTree import iterparse

    columns: List[str] = []
    result: Dict[str, List[float]] = {}
    row_values: List[Optional[float]] = []
    in_row = False
    in_schema = False

    for event, elem in iterparse(str(xml_path), events=("start", "end")):
        tag = elem.tag

        if event == "start":
            if tag == "row":
                in_row = True
                row_values = []
            elif tag in ("schema", "row-schema"):
                in_schema = True
            continue

        # event == "end"
        if tag == "name" and in_schema:
            col_name = (elem.text or "").strip()
            if col_name and col_name not in result:
                columns.append(col_name)
                result[col_name] = []

        elif tag in ("schema", "row-schema"):
            in_schema = False

        elif tag == "col" and not columns:
            # Format: <col name="Display"/>
            col_name = elem.get("name", "")
            if col_name and col_name not in result:
                columns.append(col_name)
                result[col_name] = []

        elif in_row:
            # 提取数值：优先 fmt 属性，否则 text
            fmt = elem.get("fmt", "")
            if fmt and columns:
                try:
                    val = _parse_fmt_number(fmt)
                    row_values.append(val)
                except (ValueError, IndexError):
                    row_values.append(None)
            elif tag == "c" and elem.text:
                try:
                    row_values.append(float(elem.text.strip()))
                except (ValueError, TypeError):
                    row_values.append(None)

        if tag == "row":
            in_row = False
            for i, col_name in enumerate(columns):
                if i < len(row_values) and row_values[i] is not None:
                    result[col_name].append(row_values[i])
            elem.clear()

    return result


def _parse_xml_regex(text: str) -> Dict[str, List[float]]:
    """正则 fallback 解析。"""
    columns = []
    for m in _RE_COL_NAME_ATTR.finditer(text):
        columns.append(m.group(1))
    if not columns:
        for m in _RE_COL_NAME_TAG.finditer(text):
            columns.append(m.group(1))
    if not columns:
        return {}

    result: Dict[str, List[float]] = {name: [] for name in columns}
    for row_m in _RE_ROW.finditer(text):
        row = row_m.group(1)
        cells = _RE_CELL.findall(row)
        if cells:
            for i, name in enumerate(columns):
                if i < len(cells):
                    try:
                        result[name].append(float(cells[i].strip()))
                    except (ValueError, TypeError):
                        continue
        else:
            fmt_vals = _RE_FMT.findall(row)
            for i, name in enumerate(columns):
                if i < len(fmt_vals):
                    try:
                        result[name].append(_parse_fmt_number(fmt_vals[i]))
                    except (ValueError, IndexError):
                        continue
    return result


def _parse_exported_xml_strings(xml_path: Path) -> Dict[str, List[str]]:
    """解析 XML 文件中的字符串值列（用于 thermal state 等非数值字段）。"""
    if not xml_path.exists():
        return {}
    try:
        text = xml_path.read_text(errors="replace")
    except Exception:
        return {}

    columns = []
    for m in _RE_COL_NAME_ATTR.finditer(text):
        columns.append(m.group(1))
    if not columns:
        for m in _RE_COL_NAME_TAG.finditer(text):
            columns.append(m.group(1))
    if not columns:
        return {}

    result: Dict[str, List[str]] = {name: [] for name in columns}
    for row_m in _RE_ROW.finditer(text):
        row = row_m.group(1)
        fmt_vals = _RE_FMT.findall(row)
        for i, name in enumerate(columns):
            if i < len(fmt_vals):
                result[name].append(fmt_vals[i])
    return result


def _export_one_schema(
    schema: str, trace_file: Path, exports_dir: Path,
) -> Optional[Path]:
    """单个 schema 的 xctrace export（用于并行调度）。"""
    xml_out = exports_dir / f"{schema}.xml"
    try:
        cmd = [
            "xcrun", "xctrace", "export",
            "--input", str(trace_file),
            "--xpath", f'/trace-toc/run/data/table[@schema="{schema}"]',
            "--output", str(xml_out),
        ]
        subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, text=True, timeout=30,
        )
        return xml_out if xml_out.exists() else None
    except Exception:
        return None


def build_snapshot_from_exports(
    exports_dir: Path,
    trace_file: Path,
) -> MetricSnapshot:
    """
    从 trace 文件并行导出所有已知 schema，合并为单次快照。
    使用 ThreadPoolExecutor 并行调用 xctrace export（I/O 密集）。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    snap = MetricSnapshot(ts=time.time())

    # 并行导出所有 schema
    schema_results: Dict[str, Optional[Path]] = {}
    schemas = list(SCHEMA_COLUMNS.keys())

    with ThreadPoolExecutor(max_workers=min(len(schemas), 4)) as pool:
        futures = {
            pool.submit(_export_one_schema, schema, trace_file, exports_dir): schema
            for schema in schemas
        }
        for future in as_completed(futures):
            schema = futures[future]
            try:
                schema_results[schema] = future.result()
            except Exception:
                schema_results[schema] = None

    # 解析并合并到 snapshot
    for schema, col_map in SCHEMA_COLUMNS.items():
        xml_out = schema_results.get(schema)
        if not xml_out:
            continue

        parsed = _parse_exported_xml(xml_out)
        parsed_str = _parse_exported_xml_strings(xml_out) if any(
            f.startswith("_") for f in col_map.values()
        ) else {}

        for col_name, field_name in col_map.items():
            # 字符串字段（以 _ 开头标记）
            if field_name.startswith("_"):
                real_field = field_name[1:]
                for key, vals in parsed_str.items():
                    if col_name.lower() in key.lower() and vals:
                        setattr(snap, real_field, vals[-1])
                        break
                continue

            matched = False
            for key, vals in parsed.items():
                if key.lower().strip() == col_name.lower().strip() and vals:
                    setattr(snap, field_name, vals[-1])
                    matched = True
                    break
            if matched:
                continue
            for key, vals in parsed.items():
                if col_name.lower() in key.lower() and vals:
                    setattr(snap, field_name, vals[-1])
                    break

    return snap


# ── 流式引擎 ──

class LiveMetricsStreamer:
    """
    定期从 xctrace trace 中增量导出指标。

    工作方式:
    1. 绑定一个正在录制的 trace 文件
    2. 后台线程定期 export + parse
    3. 滚动窗口计算 avg/peak/jitter
    4. 超阈值告警
    5. 可选: 写时序快照到 JSONL 文件
    """

    def __init__(
        self,
        trace_file: str,
        exports_dir: str,
        interval_sec: float = 10.0,
        window_size: int = 30,
        thresholds: Optional[List[MetricThreshold]] = None,
        alert_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        jsonl_path: Optional[str] = None,
    ):
        self.trace_file = Path(trace_file)
        self.exports_dir = Path(exports_dir)
        self.interval_sec = interval_sec
        self.window_size = window_size
        self.thresholds = thresholds or list(DEFAULT_THRESHOLDS)
        self.alert_callback = alert_callback
        self.jsonl_path = Path(jsonl_path) if jsonl_path else None

        self._running = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._snapshots: deque = deque(maxlen=window_size)
        self._alerts: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._iterations = 0
        self._start_time: float = 0.0

    # ── 生命周期 ──

    def start(self) -> Dict[str, Any]:
        if self._running.is_set():
            return {"status": "already_running"}

        if not self.trace_file.exists():
            return {
                "status": "waiting",
                "message": f"trace file not found: {self.trace_file} (will retry)",
            }

        self.exports_dir.mkdir(parents=True, exist_ok=True)
        if self.jsonl_path:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)

        self._running.set()
        self._start_time = time.time()
        self._thread = threading.Thread(target=self._stream_loop, daemon=True)
        self._thread.start()

        return {
            "status": "running",
            "trace": str(self.trace_file),
            "interval_sec": self.interval_sec,
            "window_size": self.window_size,
            "thresholds": len(self.thresholds),
        }

    def stop(self) -> Dict[str, Any]:
        if not self._running.is_set():
            return {"status": "not_running"}

        self._running.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

        return self.get_summary()

    def is_running(self) -> bool:
        return self._running.is_set()

    # ── 查询 ──

    def get_latest(self) -> Optional[MetricSnapshot]:
        with self._lock:
            return self._snapshots[-1] if self._snapshots else None

    def get_snapshots(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._lock:
            return [s.to_dict() for s in list(self._snapshots)[-limit:]]

    def get_stats(self) -> Dict[str, Any]:
        """滚动窗口统计"""
        with self._lock:
            snaps = list(self._snapshots)

        if not snaps:
            return {"samples": 0}

        stats: Dict[str, Any] = {"samples": len(snaps)}
        for field_name in ("display_mw", "cpu_mw", "networking_mw", "cpu_pct", "gpu_fps", "mem_mb", "battery_pct"):
            vals = [getattr(s, field_name) for s in snaps if getattr(s, field_name) is not None]
            if not vals:
                stats[field_name] = {"avg": None, "peak": None, "min": None}
                continue
            stats[field_name] = {
                "avg": round(sum(vals) / len(vals), 2),
                "peak": round(max(vals), 2),
                "min": round(min(vals), 2),
                "jitter": round(max(vals) - min(vals), 2) if len(vals) > 1 else 0.0,
            }
        return stats

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            snap_count = len(self._snapshots)
            alert_count = len(self._alerts)

        duration = time.time() - self._start_time if self._start_time else 0
        return {
            "status": "running" if self._running.is_set() else "stopped",
            "trace": str(self.trace_file),
            "interval_sec": self.interval_sec,
            "iterations": self._iterations,
            "snapshots": snap_count,
            "alerts": alert_count,
            "duration_sec": round(duration, 1),
            "latest": self.get_latest().to_dict() if self.get_latest() else None,
            "stats": self.get_stats(),
        }

    def get_alerts(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._alerts[-limit:])

    # ── 内部 ──

    def _stream_loop(self):
        """后台定期导出循环"""
        consecutive_errors = 0
        current_interval = self.interval_sec
        while self._running.is_set():
            try:
                self._tick()
                consecutive_errors = 0
                current_interval = self.interval_sec  # 恢复正常间隔
            except Exception as e:
                consecutive_errors += 1
                self._log_error(f"_tick 异常 (连续 {consecutive_errors} 次): {e!r}")
                # 渐进降频：3 次后 2x，5 次后 4x，10 次后停止
                if consecutive_errors >= 10:
                    self._log_error("连续 10 次失败，停止指标流")
                    break
                elif consecutive_errors >= 5:
                    current_interval = self.interval_sec * 4
                elif consecutive_errors >= 3:
                    current_interval = self.interval_sec * 2
            # 分段 sleep，以便快速响应 stop
            deadline = time.time() + current_interval
            while self._running.is_set() and time.time() < deadline:
                time.sleep(min(1.0, max(0.0, deadline - time.time())))

    def _tick(self):
        """单次导出+解析+告警"""
        if not self.trace_file.exists():
            return

        self._iterations += 1
        snap = build_snapshot_from_exports(self.exports_dir, self.trace_file)

        with self._lock:
            self._snapshots.append(snap)

        # 写 JSONL（失败抛出让 _stream_loop 记录）
        if self.jsonl_path:
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(snap.to_dict(), ensure_ascii=False) + "\n")
                f.flush()

        # 检查阈值
        self._check_thresholds(snap)

    def _log_error(self, msg: str):
        """把 _stream_loop 异常写到 jsonl 旁边的 errors.log。"""
        if not self.jsonl_path:
            return
        err_path = Path(self.jsonl_path).parent / "live_metrics.errors.log"
        try:
            ts = time.strftime("%H:%M:%S")
            err_path.parent.mkdir(parents=True, exist_ok=True)
            with open(err_path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass

    def _check_thresholds(self, snap: MetricSnapshot):
        for t in self.thresholds:
            val = getattr(snap, t.field, None)
            if val is None:
                continue
            # 对于 gpu_fps: 低于阈值才告警 (反转)
            if t.field == "gpu_fps":
                if val < t.max_value:
                    self._fire_alert(t, val, snap.ts, below=True)
            else:
                if val > t.max_value:
                    self._fire_alert(t, val, snap.ts, below=False)

    def _fire_alert(self, t: MetricThreshold, val: float, ts: float, below: bool):
        direction = "below" if below else "above"
        alert = {
            "ts": ts,
            "rule": t.name,
            "field": t.field,
            "value": round(val, 2),
            "threshold": t.max_value,
            "direction": direction,
            "level": t.level,
        }
        with self._lock:
            self._alerts.append(alert)

        if self.alert_callback:
            try:
                self.alert_callback(alert)
            except Exception:
                pass

    # ── 手动快照 ──

    def snapshot_now(self) -> Optional[MetricSnapshot]:
        """立即执行一次导出并返回快照（不需要启动后台线程）"""
        if not self.trace_file.exists():
            return None
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        snap = build_snapshot_from_exports(self.exports_dir, self.trace_file)
        with self._lock:
            self._snapshots.append(snap)
        return snap
