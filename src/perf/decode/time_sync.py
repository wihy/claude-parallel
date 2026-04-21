"""
TimeSync — syslog 与 xctrace 时序对齐模块。

功能:
- get_device_uptime: 获取设备 uptime 基准 (ideviceinfo / sysctl)
- parse_syslog_timestamps: 解析 idevicesyslog 时间戳，生成 SyslogEvent 列表
- parse_xctrace_timeline: 从 xctrace 导出 XML 提取 event-time 列
- align_timelines: 基于 syslog/xctrace 首条记录时间差自动对齐
- correlate_events: 按 window 聚合 syslog 事件与 metrics 快照
- format_event_report: 格式化输出事件归因报告 (JSONL)
"""

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple


# ── 数据结构 ──

@dataclass
class SyslogEvent:
    """解析后的 syslog 事件"""
    ts: float                       # 绝对时间戳 (epoch seconds)
    relative_sec: float             # 相对于 syslog 首条记录的秒数
    level: str = ""                 # 日志级别 (Default/Notice/Error/...)
    subsystem: str = ""             # 子系统名
    message: str = ""               # 日志消息

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "ts_iso": _epoch_to_iso(self.ts),
            "relative_sec": round(self.relative_sec, 6),
            "level": self.level,
            "subsystem": self.subsystem,
            "message": self.message,
        }


@dataclass
class CorrelatedEvent:
    """事件归因结果 — syslog 事件 + 前后 metrics 快照"""
    event: SyslogEvent
    before_metrics: List[Dict[str, Any]] = field(default_factory=list)
    after_metrics: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event": self.event.to_dict(),
            "before_metrics": self.before_metrics,
            "after_metrics": self.after_metrics,
            "summary": self.summary,
        }


# ── 辅助函数 ──

def _epoch_to_iso(ts: float) -> str:
    """epoch 秒转 ISO 8601 字符串"""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S.") + \
               f"{ts % 1:.6f}"[2:]
    except Exception:
        return ""


def _parse_syslog_line(line: str, year: Optional[int] = None) -> Optional[Tuple[datetime, str, str, str]]:
    """
    解析单行 idevicesyslog 输出。

    支持格式:
    - 'Apr 15 17:32:23 iPhone ...'
    - 'Apr 15 17:32:23 iPhone Process[pid]: message'
    - '2026-04-15 17:32:23.123456+08:00 iPhone ...'  (带年份的格式)

    返回 (datetime, level, subsystem, message) 或 None
    """
    if not line or not line.strip():
        return None

    line = line.strip()
    if year is None:
        year = datetime.now().year

    # 格式1: 'Apr 15 17:32:23 ...'
    m = re.match(
        r'^(\w{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+\S+\s+(.*)',
        line,
    )
    if m:
        month_str, day_str, hour, minute, second, rest = m.groups()
        try:
            dt = datetime.strptime(
                f"{year} {month_str} {day_str} {hour}:{minute}:{second}",
                "%Y %b %d %H:%M:%S",
            )
        except ValueError:
            return None

        level, subsystem, message = _extract_log_fields(rest)
        return (dt, level, subsystem, message)

    # 格式2: '2026-04-15 17:32:23.123456+08:00 ...'  或 '2026-04-15 17:32:23 ...'
    m = re.match(
        r'^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2}(?:\.\d+)?)'
        r'(?:[+-]\d{2}:\d{2})?\s+\S+\s+(.*)',
        line,
    )
    if m:
        date_str, time_str, rest = m.groups()
        try:
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            try:
                dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None

        level, subsystem, message = _extract_log_fields(rest)
        return (dt, level, subsystem, message)

    return None


def _extract_log_fields(rest: str) -> Tuple[str, str, str]:
    """
    从 syslog 行剩余部分提取 level, subsystem, message。

    常见格式:
    - 'Process[123]: <Notice>: message'
    - 'Process[123]: subsystem: message'
    - 'kernel: message'
    - 'com.apple.thermal(thermalPressureLevel ...): message'
    """
    level = ""
    subsystem = ""
    message = rest

    # 尝试匹配 '<Level>:' 格式
    m = re.search(r'<(\w+)>:\s*(.*)', rest)
    if m:
        level = m.group(1)
        message = m.group(2).strip()
        # 提取 subsystem (在 <> 之前)
        prefix = rest[:m.start()].strip().rstrip(":").strip()
        subsystem = re.split(r'[\[\(]', prefix)[0].strip()
        return (level, subsystem, message)

    # 尝试匹配 'Process[pid]: subsystem: message'
    m = re.match(r'^(\S+?)(?:\[\d+\])?:\s+(.*)', rest)
    if m:
        subsystem = m.group(1)
        message = m.group(2).strip()
        # 如果 message 里还有 'subsystem: message' 模式
        m2 = re.match(r'^(\S+):\s+(.*)', message)
        if m2 and not m2.group(1).isdigit():
            # 判断是不是子系统名 (含点号或已知前缀)
            candidate = m2.group(1)
            if "." in candidate or candidate.startswith("com.") or candidate.startswith("org."):
                subsystem = candidate
                message = m2.group(2).strip()

    return (level, subsystem, message)


def _parse_xctrace_time(time_str: str) -> float:
    """
    解析 xctrace event-time 格式为秒数。

    支持格式:
    - '00:45.123.456'  (MM:SS.mmm.uuu)
    - '01:23:45.678'   (HH:MM:SS.mmm)
    - '45.123'         (SS.mmm)
    - '45.123456'      (SS.uuuuuu)

    返回: 总秒数 (float)
    """
    time_str = time_str.strip().strip('"').strip("'")
    if not time_str:
        return 0.0

    # 格式: MM:SS.mmm.uuu 或 HH:MM:SS.mmm
    # 先尝试 HH:MM:SS.mmm
    m = re.match(r'^(\d+):(\d+):(\d+)\.(\d+)$', time_str)
    if m:
        h, mi, s, frac = m.groups()
        total = int(h) * 3600 + int(mi) * 60 + int(s)
        # frac 可能是毫秒或微秒
        frac_str = frac
        if len(frac_str) <= 3:
            total += int(frac_str) / (10 ** len(frac_str))
        else:
            total += int(frac_str[:3]) / 1000.0
        return total

    # 格式: MM:SS.mmm.uuu
    m = re.match(r'^(\d+):(\d+)\.(\d+)\.(\d+)$', time_str)
    if m:
        mi, s, ms, us = m.groups()
        total = int(mi) * 60 + int(s) + int(ms) / 1000.0 + int(us) / 1_000_000.0
        return total

    # 格式: MM:SS.mmm
    m = re.match(r'^(\d+):(\d+)\.(\d+)$', time_str)
    if m:
        mi, s, ms = m.groups()
        total = int(mi) * 60 + int(s) + int(ms) / (10 ** len(ms))
        return total

    # 格式: SS.mmm 或纯数字
    m = re.match(r'^(\d+):(\d+)$', time_str)
    if m:
        mi, s = m.groups()
        return int(mi) * 60 + int(s)

    # 纯小数或整数
    try:
        return float(time_str)
    except ValueError:
        return 0.0


# ── 核心函数 ──

def get_device_uptime(device_udid: str = "") -> Dict[str, Any]:
    """
    通过 ideviceinfo -k DeviceUptime 获取设备 uptime 基准。

    回退方案: 通过 sysctl 查询 (需 SSH/tunnel)。

    返回: {
        "uptime_sec": float,
        "source": str,
        "device": str,
        "error": str | None,
    }
    """
    result: Dict[str, Any] = {
        "uptime_sec": 0.0,
        "source": "",
        "device": device_udid,
        "error": None,
    }

    # 方案1: ideviceinfo -k DeviceUptime
    cmd = ["ideviceinfo"]
    if device_udid:
        cmd.extend(["-u", device_udid])
    cmd.extend(["-k", "DeviceUptime"])

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            raw = proc.stdout.strip()
            # DeviceUptime 返回格式可能是 "123456" (秒) 或 "1d 2h 3m 4s"
            uptime = _parse_uptime_string(raw)
            if uptime > 0:
                result["uptime_sec"] = uptime
                result["source"] = "ideviceinfo"
                return result
    except FileNotFoundError:
        result["error"] = "ideviceinfo not found — install: brew install libimobiledevice"
    except subprocess.TimeoutExpired:
        result["error"] = "ideviceinfo timed out"
    except Exception as e:
        result["error"] = f"ideviceinfo error: {e}"

    # 方案2: sysctl (需 pymobiledevice3 tunnel 或 SSH)
    cmd2 = ["idevicesysdate"]
    if device_udid:
        cmd2.extend(["-u", device_udid])
    try:
        proc = subprocess.run(
            cmd2, capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            # idevicesysdate 返回设备当前时间
            device_time_str = proc.stdout.strip()
            try:
                dt = datetime.strptime(device_time_str, "%Y-%m-%d %H:%M:%S")
                uptime_approx = time.time() - dt.timestamp()
                # 这不是真正的 uptime，但可作为时钟偏移参考
                result["uptime_sec"] = 0.0
                result["source"] = "sysdate_fallback"
                result["device_time"] = device_time_str
                result["clock_offset_sec"] = uptime_approx
                return result
            except ValueError:
                pass
    except Exception:
        pass

    if not result["error"]:
        result["error"] = "all uptime methods failed"
    return result


def _parse_uptime_string(raw: str) -> float:
    """解析 uptime 字符串为秒数。支持 '123456'、'1d 2h 3m 4s' 等格式。"""
    raw = raw.strip()

    # 纯数字 (秒)
    if re.match(r'^\d+(\.\d+)?$', raw):
        return float(raw)

    # '1d 2h 3m 4s' 格式
    total = 0.0
    for m in re.finditer(r'(\d+(?:\.\d+)?)\s*(d|h|m|s|ms|us)', raw):
        val = float(m.group(1))
        unit = m.group(2)
        if unit == "d":
            total += val * 86400
        elif unit == "h":
            total += val * 3600
        elif unit == "m":
            total += val * 60
        elif unit == "s":
            total += val
        elif unit == "ms":
            total += val / 1000
        elif unit == "us":
            total += val / 1_000_000

    return total


def parse_syslog_timestamps(syslog_path: str) -> List[SyslogEvent]:
    """
    解析 syslog 文件，提取 (abs_timestamp, relative_seconds, message)。

    支持 idevicesyslog 格式: 'Apr 15 17:32:23 iPhone ...'

    参数:
        syslog_path: syslog 文件路径

    返回: SyslogEvent 列表，按时间排序
    """
    path = Path(syslog_path)
    if not path.exists():
        return []

    try:
        text = path.read_text(errors="replace")
    except Exception:
        return []

    lines = text.splitlines()
    if not lines:
        return []

    events: List[SyslogEvent] = []
    year = datetime.now().year

    # 第一遍: 尝试确定年份 — 如果有带年份的行，以之为准
    for line in lines:
        m = re.match(r'^(\d{4})-\d{2}-\d{2}', line)
        if m:
            year = int(m.group(1))
            break

    # 解析每行
    base_ts: Optional[float] = None
    for line in lines:
        parsed = _parse_syslog_line(line, year)
        if parsed is None:
            continue

        dt, level, subsystem, message = parsed
        ts = dt.timestamp()

        if base_ts is None:
            base_ts = ts

        events.append(SyslogEvent(
            ts=ts,
            relative_sec=ts - base_ts,
            level=level,
            subsystem=subsystem,
            message=message,
        ))

    return events


def parse_xctrace_timeline(trace_xml_path: str) -> List[Dict[str, Any]]:
    """
    从 xctrace 导出的 XML 中提取 event-time 列，转为相对秒数。

    xctrace 时间格式: '00:45.123.456' (MM:SS.mmm.uuu)

    参数:
        trace_xml_path: xctrace export 的 XML 文件路径

    返回: [{"time_raw": str, "relative_sec": float, "row_index": int}, ...]
    """
    path = Path(trace_xml_path)
    if not path.exists():
        return []

    try:
        text = path.read_text(errors="replace")
    except Exception:
        return []

    results: List[Dict[str, Any]] = []

    # 查找 event-time 列索引 (从 header/schema 区域)
    col_idx = _find_time_column_index(text)

    # ── 尝试标准 xctrace 格式: <row>...<c>value</c>...</row> ──
    row_pat = re.compile(r'<row>(.*?)</row>', re.S)
    cell_c_pat = re.compile(r'<c[^>]*>(.*?)</c>', re.S)
    # 备用格式: <row>...<col ...>value</col>...</row>
    cell_col_pat = re.compile(r'<col[^>]*>(.*?)</col>', re.S)

    # 检测是否使用 <c> 还是 <col> 作为数据单元格
    # 如果 col_idx 找到了，说明 <col name=...> 同时出现在 header 和 data 中
    # 此时需要区分 header <col> 和 data <col>
    use_c_cells = bool(cell_c_pat.search(text))

    row_idx = 0
    for row_m in row_pat.finditer(text):
        row_text = row_m.group(1)

        # 选择合适的 cell pattern
        if use_c_cells:
            cells = [c.strip() for c in cell_c_pat.findall(row_text)]
        else:
            cells = [c.strip() for c in cell_col_pat.findall(row_text)]

        if not cells:
            continue

        # 确定时间列索引
        idx = col_idx if col_idx is not None else 0
        if idx is None:
            # 自动检测: 找第一个看起来像时间的 cell
            for ci, cv in enumerate(cells):
                cv = cv.strip()
                if re.match(r'^\d+:\d+', cv) or re.match(r'^\d+\.\d+$', cv):
                    idx = ci
                    break
            if idx is None:
                idx = 0

        if idx < len(cells):
            time_raw = cells[idx].strip()
            if time_raw and time_raw != "-":
                relative_sec = _parse_xctrace_time(time_raw)
                if relative_sec > 0:
                    results.append({
                        "time_raw": time_raw,
                        "relative_sec": relative_sec,
                        "row_index": row_idx,
                    })
                    row_idx += 1

    return results


def _find_time_column_index(text: str) -> Optional[int]:
    """
    在 xctrace XML header/schema 中找到 event-time 列的索引。

    xctrace export XML 的 header 区域包含列定义:
    <header>
      <col type="s" name="event-time"/>
      <col type="s" name="cpu-pct"/>
    </header>

    注意: 需要区分 header 中的 <col> 定义和 row 中的 <col> 数据。
    策略: 只在 <header>...</header> 或 <table-schema>...</table-schema> 内搜索。
    """
    # 方案1: 在 <header> 区域内查找
    header_m = re.search(r'<header[^>]*>(.*?)</header>', text, re.S)
    if header_m:
        header_text = header_m.group(1)
        col_pat = re.compile(r'<col[^>]*name="([^"]+)"[^>]*/?\s*>')
        for i, m in enumerate(col_pat.finditer(header_text)):
            col_name = m.group(1).lower()
            if col_name in ("event-time", "time", "timestamp", "start-time"):
                return i

    # 方案2: 在 <table-schema> 内查找
    schema_m = re.search(r'<table-schema[^>]*>(.*?)</table-schema>', text, re.S)
    if schema_m:
        schema_text = schema_m.group(1)
        col_pat = re.compile(r'<col[^>]*name="([^"]+)"[^>]*/?\s*>')
        for i, m in enumerate(col_pat.finditer(schema_text)):
            col_name = m.group(1).lower()
            if col_name in ("event-time", "time", "timestamp", "start-time"):
                return i

    # 方案3: 在首个 <row> 之前查找所有 <col name=...> (header 区域)
    first_row = re.search(r'<row>', text)
    if first_row:
        pre_row_text = text[:first_row.start()]
        col_pat = re.compile(r'<col[^>]*name="([^"]+)"[^>]*>')
        for i, m in enumerate(col_pat.finditer(pre_row_text)):
            col_name = m.group(1).lower()
            if col_name in ("event-time", "time", "timestamp", "start-time"):
                return i

    return None


def align_timelines(
    syslog_entries: List[SyslogEvent],
    xctrace_times: List[Dict[str, Any]],
    offset_seconds: float = 0,
) -> Dict[str, Any]:
    """
    基于 syslog 第一条与 xctrace 第一条的时间差自动计算 offset，对齐两条时间轴。

    对齐策略:
    - 如果 offset_seconds != 0，使用指定偏移
    - 否则，以 syslog 首条记录为基准 (relative_sec=0)，将 xctrace 时间映射到同一时间轴
    - offset = syslog_first_relative - xctrace_first_relative + manual_offset

    参数:
        syslog_entries: parse_syslog_timestamps 的输出
        xctrace_times: parse_xctrace_timeline 的输出
        offset_seconds: 手动偏移修正量 (秒)

    返回: {
        "offset_sec": float,
        "syslog_count": int,
        "xctrace_count": int,
        "aligned_syslog": [...],   # relative_sec 已调整为统一时间轴
        "aligned_xctrace": [...],  # relative_sec 已调整为统一时间轴
    }
    """
    if not syslog_entries and not xctrace_times:
        return {
            "offset_sec": 0.0,
            "syslog_count": 0,
            "xctrace_count": 0,
            "aligned_syslog": [],
            "aligned_xctrace": [],
        }

    # 基准点
    syslog_first = syslog_entries[0].relative_sec if syslog_entries else 0.0
    xctrace_first = xctrace_times[0]["relative_sec"] if xctrace_times else 0.0

    # 自动计算 offset: 让 xctrace 首条对齐到 syslog 首条
    auto_offset = syslog_first - xctrace_first + offset_seconds

    # 对齐 syslog: 以 syslog 首条为 0 点 (原始 relative_sec 已经是这样)
    aligned_syslog = []
    for entry in syslog_entries:
        d = entry.to_dict()
        d["aligned_sec"] = round(entry.relative_sec, 6)
        aligned_syslog.append(d)

    # 对齐 xctrace: 加上 offset
    aligned_xctrace = []
    for xt in xctrace_times:
        aligned_xctrace.append({
            "time_raw": xt["time_raw"],
            "original_sec": round(xt["relative_sec"], 6),
            "aligned_sec": round(xt["relative_sec"] + auto_offset, 6),
            "row_index": xt["row_index"],
        })

    return {
        "offset_sec": round(auto_offset, 6),
        "syslog_count": len(syslog_entries),
        "xctrace_count": len(xctrace_times),
        "aligned_syslog": aligned_syslog,
        "aligned_xctrace": aligned_xctrace,
    }


def correlate_events(
    aligned_syslog: List[Dict[str, Any]],
    metrics_snapshots: List[Dict[str, Any]],
    window_seconds: float = 5.0,
) -> List[CorrelatedEvent]:
    """
    对每个 syslog 事件，取前后 N 秒的 metrics 快照，生成事件归因摘要。

    参数:
        aligned_syslog: align_timelines 输出中的 aligned_syslog 列表
                        每项需有 "aligned_sec" 字段
        metrics_snapshots: metrics 快照列表
                           每项需有 "ts" (相对秒数) 或 "aligned_sec" 字段
        window_seconds: 前后窗口大小 (秒)

    返回: CorrelatedEvent 列表
    """
    if not aligned_syslog or not metrics_snapshots:
        return []

    # 预处理 metrics — 提取时间轴
    metrics_times: List[float] = []
    for snap in metrics_snapshots:
        t = snap.get("aligned_sec", snap.get("ts", snap.get("relative_sec", 0.0)))
        metrics_times.append(float(t))

    correlated: List[CorrelatedEvent] = []

    for entry in aligned_syslog:
        event_sec = entry.get("aligned_sec", entry.get("relative_sec", 0.0))

        # 收集 window 内的 metrics
        before = []
        after = []
        for i, mt in enumerate(metrics_times):
            delta = mt - event_sec
            if -window_seconds <= delta < 0:
                before.append(metrics_snapshots[i])
            elif 0 <= delta <= window_seconds:
                after.append(metrics_snapshots[i])

        # 生成摘要
        summary = _generate_event_summary(entry, before, after, window_seconds)

        correlated.append(CorrelatedEvent(
            event=SyslogEvent(
                ts=entry.get("ts", 0.0),
                relative_sec=event_sec,
                level=entry.get("level", ""),
                subsystem=entry.get("subsystem", ""),
                message=entry.get("message", ""),
            ),
            before_metrics=before,
            after_metrics=after,
            summary=summary,
        ))

    return correlated


def _generate_event_summary(
    event: Dict[str, Any],
    before: List[Dict[str, Any]],
    after: List[Dict[str, Any]],
    window_seconds: float,
) -> str:
    """为单个事件生成归因摘要文本。"""
    parts = []

    event_msg = event.get("message", "")[:80]
    level = event.get("level", "")
    subsystem = event.get("subsystem", "")
    aligned_sec = event.get("aligned_sec", event.get("relative_sec", 0.0))

    header = f"@{aligned_sec:.2f}s"
    if level:
        header += f" [{level}]"
    if subsystem:
        header += f" {subsystem}"
    header += f": {event_msg}"
    parts.append(header)

    # 分析 before/after 的关键指标变化
    metrics_to_check = [
        ("cpu_pct", "CPU%"),
        ("cpu_mw", "CPU mW"),
        ("display_mw", "Display mW"),
        ("mem_mb", "Mem MB"),
        ("gpu_fps", "FPS"),
        ("networking_mw", "Net mW"),
    ]

    for key, label in metrics_to_check:
        before_vals = [m.get(key) for m in before if m.get(key) is not None]
        after_vals = [m.get(key) for m in after if m.get(key) is not None]

        if before_vals and after_vals:
            b_avg = sum(before_vals) / len(before_vals)
            a_avg = sum(after_vals) / len(after_vals)
            if b_avg > 0:
                pct_change = (a_avg - b_avg) / b_avg * 100
                arrow = "↑" if pct_change > 0 else "↓"
                if abs(pct_change) >= 5:
                    parts.append(
                        f"  {label}: {b_avg:.1f} → {a_avg:.1f} ({arrow}{abs(pct_change):.0f}%)"
                    )
            else:
                if abs(a_avg - b_avg) >= 1:
                    parts.append(
                        f"  {label}: {b_avg:.1f} → {a_avg:.1f}"
                    )
        elif after_vals and not before_vals:
            a_avg = sum(after_vals) / len(after_vals)
            if a_avg > 0:
                parts.append(f"  {label}: (no baseline) → {a_avg:.1f}")

    parts.append(f"  window: ±{window_seconds:.0f}s | before={len(before)} after={len(after)}")

    return "\n".join(parts)


def format_event_report(
    correlated: List[CorrelatedEvent],
    output_path: Optional[str] = None,
) -> str:
    """
    格式化输出事件归因报告。

    输出 JSONL 格式，每行一个 JSON 对象。
    如果 output_path 指定，同时写入文件。

    参数:
        correlated: correlate_events 的输出
        output_path: 输出文件路径 (默认写入 session logs/ 目录)

    返回: 格式化后的报告文本
    """
    lines = []

    # 报告头
    report_header = {
        "type": "time_sync_report",
        "generated_at": time.time(),
        "generated_iso": datetime.now().isoformat(),
        "total_events": len(correlated),
    }
    lines.append(json.dumps(report_header, ensure_ascii=False))

    # 每个事件一行
    for ce in correlated:
        obj = ce.to_dict()
        obj["type"] = "correlated_event"
        lines.append(json.dumps(obj, ensure_ascii=False))

    report_text = "\n".join(lines) + "\n"

    # 写入文件
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "a", encoding="utf-8") as f:
            f.write(report_text)

    return report_text


# ── 高级 API ──

def run_time_sync(
    syslog_path: str,
    trace_xml_path: str,
    metrics_jsonl_path: Optional[str] = None,
    output_dir: Optional[str] = None,
    offset_seconds: float = 0,
    window_seconds: float = 5.0,
) -> Dict[str, Any]:
    """
    一键执行完整的时序对齐流程。

    步骤:
    1. 解析 syslog 时间戳
    2. 解析 xctrace 时间线
    3. 加载 metrics 快照 (JSONL)
    4. 对齐时间轴
    5. 关联事件
    6. 输出报告

    参数:
        syslog_path: syslog 文件路径
        trace_xml_path: xctrace 导出 XML 路径
        metrics_jsonl_path: metrics JSONL 文件路径 (可选)
        output_dir: 输出目录 (默认 syslog 同目录的 logs/ 子目录)
        offset_seconds: 手动时间偏移修正
        window_seconds: 事件关联窗口大小

    返回: 汇总结果 dict
    """
    # 1. 解析 syslog
    syslog_entries = parse_syslog_timestamps(syslog_path)

    # 2. 解析 xctrace timeline
    xctrace_times = parse_xctrace_timeline(trace_xml_path)

    # 3. 对齐时间轴
    aligned = align_timelines(syslog_entries, xctrace_times, offset_seconds)

    # 4. 加载 metrics 快照
    metrics_snapshots: List[Dict[str, Any]] = []
    if metrics_jsonl_path:
        mpath = Path(metrics_jsonl_path)
        if mpath.exists():
            try:
                raw = mpath.read_text(errors="replace")
                for line in raw.splitlines():
                    line = line.strip()
                    if line:
                        try:
                            metrics_snapshots.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except Exception:
                pass

    # 5. 如果 metrics 有绝对时间戳，转换为相对时间
    if metrics_snapshots and syslog_entries:
        base_ts = syslog_entries[0].ts
        for snap in metrics_snapshots:
            if "relative_sec" not in snap and "ts" in snap:
                snap["relative_sec"] = snap["ts"] - base_ts

    # 6. 关联事件
    correlated = correlate_events(
        aligned["aligned_syslog"],
        metrics_snapshots,
        window_seconds=window_seconds,
    )

    # 7. 确定输出目录
    if output_dir is None:
        output_dir = str(Path(syslog_path).parent / "logs")
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # 8. 输出报告
    report_path = out_path / "time_sync_report.jsonl"
    report_text = format_event_report(correlated, str(report_path))

    # 9. 同时输出对齐结果
    aligned_path = out_path / "time_sync_aligned.json"
    aligned_data = {
        "offset_sec": aligned["offset_sec"],
        "syslog_count": aligned["syslog_count"],
        "xctrace_count": aligned["xctrace_count"],
        "syslog_time_range": {
            "first": aligned["aligned_syslog"][0]["aligned_sec"] if aligned["aligned_syslog"] else None,
            "last": aligned["aligned_syslog"][-1]["aligned_sec"] if aligned["aligned_syslog"] else None,
        },
        "xctrace_time_range": {
            "first": aligned["aligned_xctrace"][0]["aligned_sec"] if aligned["aligned_xctrace"] else None,
            "last": aligned["aligned_xctrace"][-1]["aligned_sec"] if aligned["aligned_xctrace"] else None,
        },
    }
    with open(aligned_path, "w", encoding="utf-8") as f:
        json.dump(aligned_data, f, ensure_ascii=False, indent=2)

    return {
        "status": "ok",
        "syslog_entries": len(syslog_entries),
        "xctrace_entries": len(xctrace_times),
        "offset_sec": aligned["offset_sec"],
        "correlated_events": len(correlated),
        "report_path": str(report_path),
        "aligned_path": str(aligned_path),
    }
