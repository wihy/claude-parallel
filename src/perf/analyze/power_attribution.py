"""
power_attribution — 进程级功耗归因模块。

将 xctrace Power Profiler 导出的系统功耗数据按各进程 CPU% 占比
分摊到每个进程，实现"谁在耗电"的精细分析。

数据源:
- SystemPowerLevel XML: xctrace export --xpath 'schema="system-power-level"'
- process_metrics.jsonl: per-process CPU% 采样 (DvtBridge 或 ProcessMetricsStreamer)
- time-profile XML: xctrace Time Profiler 导出 (备选 CPU% 来源)

核心公式:
  process_power_mw = total_power_mw × (process_cpu_pct / sum_all_cpu_pct)

也支持:
- 设备进程树发现 (xcrun devicectl)
- 进程生命周期追踪 (周期轮询)
- 异常检测 (僵尸进程、功耗飙升、内存持续增长)
"""

import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree.ElementTree import iterparse

logger = logging.getLogger(__name__)


# ── 数据结构 ──


@dataclass
class ProcessPower:
    """单个进程的功耗归因结果。"""
    name: str
    pid: int
    avg_cpu_pct: float       # 平均 CPU%
    power_mw: float          # 归因功耗 (mW) — 总功耗 (CPU 分摊)
    pct_of_total: float      # 占总功耗百分比
    # P2: 多维功耗归因字段
    cpu_power_mw: float = 0.0      # CPU 子系统功耗 (mW)
    gpu_power_mw: float = 0.0      # GPU 子系统功耗 (mW)
    network_power_mw: float = 0.0  # 网络子系统功耗 (mW)
    display_power_mw: float = 0.0  # Display 子系统功耗 (mW)
    avg_rx_bytes: float = 0.0      # 平均接收字节数 (from DvtBridge network)
    avg_tx_bytes: float = 0.0      # 平均发送字节数
    gpu_time_pct: float = 0.0      # GPU 时间占比 (from DvtBridge graphics)
    source: str = "cpu_linear"      # 归因方法: cpu_linear / multidim

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "pid": self.pid,
            "avg_cpu_pct": round(self.avg_cpu_pct, 2),
            "power_mw": round(self.power_mw, 2),
            "pct_of_total": round(self.pct_of_total, 2),
            "cpu_power_mw": round(self.cpu_power_mw, 2),
            "gpu_power_mw": round(self.gpu_power_mw, 2),
            "network_power_mw": round(self.network_power_mw, 2),
            "display_power_mw": round(self.display_power_mw, 2),
            "source": self.source,
        }


@dataclass
class ProcessLifecycleEvent:
    """进程启停事件。"""
    ts: float
    event_type: str  # "start" / "exit"
    pid: int
    name: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "event_type": self.event_type,
            "pid": self.pid,
            "name": self.name,
        }


@dataclass
class AnomalyEvent:
    """异常事件。"""
    ts: float
    anomaly_type: str   # "zombie" / "power_spike" / "memory_growth"
    process_name: str
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "anomaly_type": self.anomaly_type,
            "process_name": self.process_name,
            "detail": self.detail,
        }


# ── 1. 解析 SystemPowerLevel XML ──


def parse_system_power(xml_path: Path) -> List[Dict[str, Any]]:
    """
    解析 xctrace 导出的 SystemPowerLevel schema XML。

    SystemPowerLevel XML 格式 (xctrace export):
      <table schema="system-power-level">
        <row>
          <c fmt="1234.56">timestamp</c>
          <c fmt="200.0">cpu_mw</c>
          <c fmt="150.0">gpu_mw</c>
          <c fmt="300.0">display_mw</c>
          <c fmt="50.0">network_mw</c>
          <c fmt="700.0">total_mw</c>
          ...
        </row>
      </table>

    Returns:
        [{ts, cpu_mw, gpu_mw, display_mw, network_mw, total_mw}, ...]
    """
    xml_path = Path(xml_path)
    if not xml_path.exists():
        logger.warning("parse_system_power: file not found: %s", xml_path)
        return []

    # 列名映射 (xctrace 可能使用不同的列名)
    COL_MAP = {
        "timestamp": "ts",
        "time": "ts",
        "Start Time": "ts",
        "CPU": "cpu_mw",
        "CPU Power": "cpu_mw",
        "cpu": "cpu_mw",
        "GPU": "gpu_mw",
        "GPU Power": "gpu_mw",
        "gpu": "gpu_mw",
        "Display": "display_mw",
        "Display Power": "display_mw",
        "display": "display_mw",
        "Networking": "network_mw",
        "Network Power": "network_mw",
        "network": "network_mw",
        "Total": "total_mw",
        "Total Power": "total_mw",
        "total": "total_mw",
    }

    samples: List[Dict[str, Any]] = []

    def _row_handler(row: Dict[str, str]):
        sample: Dict[str, Any] = {
            "ts": 0.0,
            "cpu_mw": 0.0,
            "gpu_mw": 0.0,
            "display_mw": 0.0,
            "network_mw": 0.0,
            "total_mw": 0.0,
        }
        for col_name, value in row.items():
            mapped = COL_MAP.get(col_name, col_name)
            if mapped in sample:
                parsed = _parse_mw_value(value)
                if parsed is not None:
                    sample[mapped] = parsed
        # 只有 ts 有效才保留
        if sample["ts"] > 0 or any(
            v > 0 for k, v in sample.items() if k != "ts"
        ):
            samples.append(sample)

    _iterparse_power_rows(xml_path, _row_handler)

    # 若 total_mw 全为 0，尝试从各分量求和
    for s in samples:
        if s["total_mw"] <= 0:
            s["total_mw"] = (
                s["cpu_mw"] + s["gpu_mw"]
                + s["display_mw"] + s["network_mw"]
            )

    logger.info(
        "parse_system_power: %d samples from %s", len(samples), xml_path
    )
    return samples


def _iterparse_power_rows(
    xml_path: Path, row_handler,
) -> None:
    """
    流式解析 SystemPowerLevel XML 的 <row> 元素。

    支持两种格式:
    - 标准格式: <row><c fmt="123.45">val</c>...</row> (有 col 定义列名)
    - 简化格式: <row><cpu fmt="200.0">...</cpu><gpu fmt="150.0">...</gpu>...</row>
    - xctrace 格式: <row-schema><col name="..."/></row-schema> + <row><c>val</c></row>
    """
    columns: List[str] = []
    in_row = False
    in_row_schema = False
    row_values: Dict[str, str] = {}
    col_idx = 0
    col_name_buf = ""

    for event, elem in iterparse(str(xml_path), events=("start", "end")):
        tag = elem.tag

        if event == "start":
            if tag == "row":
                in_row = True
                row_values = {}
                col_idx = 0
            elif tag in ("schema", "row-schema"):
                in_row_schema = True
            continue

        # event == "end"

        # ── 列定义阶段 ──
        if tag == "name" and in_row_schema:
            txt = (elem.text or "").strip()
            if txt:
                col_name_buf = txt
                if col_name_buf not in columns:
                    columns.append(col_name_buf)

        elif tag in ("schema", "row-schema"):
            in_row_schema = False

        elif tag == "col" and not in_row:
            cname = elem.get("name", "")
            if cname and cname not in columns:
                columns.append(cname)

        # ── 行数据阶段 ──
        elif in_row:
            if tag == "c":
                fmt = elem.get("fmt", "")
                text_val = (elem.text or "").strip()
                value = fmt if fmt else text_val
                if col_idx < len(columns):
                    row_values[columns[col_idx]] = value
                col_idx += 1

            elif tag not in ("row",):
                # mnemonic tag 直接出现在 row 内
                fmt = elem.get("fmt", "")
                text_val = (elem.text or "").strip()
                value = fmt if fmt else text_val
                if value:
                    # 用 tag 名作为列名
                    if tag not in columns:
                        columns.append(tag)
                    row_values[tag] = value

        # ── 行结束 ──
        if tag == "row":
            in_row = False
            if row_values:
                row_handler(row_values)
            elem.clear()


def _parse_mw_value(text: str) -> Optional[float]:
    """解析功耗值: '200.0', '200 mW', '1.5 W' → mW float."""
    if not text:
        return None
    text = text.strip()
    parts = text.split()
    if not parts:
        return None
    try:
        val = float(parts[0].replace(",", ""))
    except ValueError:
        # 尝试提取数字
        cleaned = ""
        for ch in parts[0]:
            if ch in "0123456789.-":
                cleaned += ch
        if not cleaned:
            return None
        try:
            val = float(cleaned)
        except ValueError:
            return None

    # 单位转换
    if len(parts) > 1:
        unit = parts[1].lower()
        if unit == "w":
            val *= 1000.0
        elif unit in ("µw", "uw", "μw"):
            val /= 1000.0
        elif unit == "kw":
            val *= 1000000.0
    return val


# ── 2. 解析进程 CPU% ──


def parse_process_cpu(
    xml_path_or_jsonl: Path,
) -> List[Dict[str, Any]]:
    """
    从 time-profile XML 或 process_metrics.jsonl 读取各进程 CPU% 采样。

    JSONL 格式 (process_metrics.jsonl):
      {"ts": 1234.5, "pid": 123, "name": "MyApp", "cpuUsage": 30.5, ...}

    time-profile XML:
      从 thread 采样计算各进程的 CPU% (采样权重占比 × 100)

    Returns:
        [{ts, pid, name, cpu_pct}, ...]
    """
    path = Path(xml_path_or_jsonl)
    if not path.exists():
        logger.warning("parse_process_cpu: file not found: %s", path)
        return []

    suffix = path.suffix.lower()

    if suffix == ".jsonl":
        return _parse_cpu_from_jsonl(path)
    elif suffix in (".xml", ".trace"):
        return _parse_cpu_from_timeprofile(path)
    else:
        # 尝试按内容检测
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:256]
            if head.lstrip().startswith("<"):
                return _parse_cpu_from_timeprofile(path)
            elif head.lstrip().startswith("{"):
                return _parse_cpu_from_jsonl(path)
        except Exception:
            pass
        return []


def _parse_cpu_from_jsonl(path: Path) -> List[Dict[str, Any]]:
    """从 JSONL 读取进程 CPU% 采样。"""
    samples: List[Dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []

    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue

        ts = d.get("ts", 0)
        pid = d.get("pid", 0)
        name = d.get("name", "")
        cpu = d.get("cpuUsage") or d.get("cpu_usage") or d.get("cpu_pct")

        if pid and name and cpu is not None:
            try:
                cpu_val = float(cpu)
            except (ValueError, TypeError):
                continue
            samples.append({
                "ts": float(ts),
                "pid": int(pid),
                "name": str(name),
                "cpu_pct": cpu_val,
            })

    logger.info(
        "parse_process_cpu (jsonl): %d samples from %s",
        len(samples), path,
    )
    return samples


def _parse_cpu_from_timeprofile(path: Path) -> List[Dict[str, Any]]:
    """
    从 time-profile XML 中提取各进程 CPU% (基于采样权重)。

    解析 thread 采样，统计各进程的采样次数占比作为 CPU%。
    """
    # 先尝试按 backtrace 格式解析
    process_samples: Dict[Tuple[int, str], int] = defaultdict(int)
    total_samples = 0

    # 快速格式检测
    try:
        with open(path, "r", errors="replace") as f:
            head = f.read(4096)
    except Exception:
        return []

    # 尝试提取 process/thread 信息
    # xctrace time-profile XML 格式:
    # <row><backtrace><frame name="..."/><thread fmt="tid"/><process fmt="pid"/>
    # 或 legacy: <process-name>AppName</process-name><sample-count>42</sample-count>

    if "<backtrace" in head or "<tagged-backtrace" in head:
        return _parse_cpu_from_timeprofile_backtrace(path)
    else:
        return _parse_cpu_from_timeprofile_legacy(path)


def _parse_cpu_from_timeprofile_backtrace(
    path: Path,
) -> List[Dict[str, Any]]:
    """从 backtrace 格式的 time-profile XML 中提取进程 CPU%。"""
    process_weights: Dict[Tuple[str, int], float] = defaultdict(float)
    current_process = ""
    current_pid = 0
    current_weight = 0.0
    total_weight = 0.0

    for event, elem in iterparse(str(path), events=("start", "end")):
        tag = elem.tag

        if event == "start":
            if tag == "row":
                current_process = ""
                current_pid = 0
                current_weight = 0.0
            continue

        # event == "end"
        if tag == "process":
            fmt = elem.get("fmt", "")
            text = (elem.text or "").strip()
            val = fmt or text
            if val:
                try:
                    current_pid = int(float(val))
                except (ValueError, TypeError):
                    pass
        elif tag == "process-name":
            text = (elem.text or "").strip()
            if text:
                current_process = text
        elif tag in ("sample-count", "weight", "running-time"):
            fmt = elem.get("fmt", "")
            text = (elem.text or "").strip()
            val = fmt or text
            if val:
                current_weight = _parse_mw_value(val) or 0.0
                if current_weight == 0.0:
                    try:
                        current_weight = float(val)
                    except (ValueError, TypeError):
                        pass
        elif tag == "row":
            if current_process and current_weight > 0:
                key = (current_process, current_pid)
                process_weights[key] += current_weight
                total_weight += current_weight
            elem.clear()

    if total_weight <= 0:
        return []

    samples: List[Dict[str, Any]] = []
    for (name, pid), weight in process_weights.items():
        pct = (weight / total_weight) * 100.0
        samples.append({
            "ts": 0.0,  # time-profile 无精确 ts
            "pid": pid,
            "name": name,
            "cpu_pct": pct,
        })

    return samples


def _parse_cpu_from_timeprofile_legacy(
    path: Path,
) -> List[Dict[str, Any]]:
    """从 legacy 格式的 time-profile XML 中提取进程 CPU%。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    process_weights: Dict[Tuple[str, int], float] = defaultdict(float)
    total_weight = 0.0

    # 匹配 <sample-count>N</sample-count> 和 <process-name>X</process-name>
    # 以及 <process fmt="pid">
    proc_pat = re.compile(r"<process-name>([^<]+)</process-name>", re.S)
    pid_pat = re.compile(r'<process[^>]*fmt="([^"]*)"', re.S)
    count_pat = re.compile(r"<sample-count>(\d+)</sample-count>", re.S)
    weight_pat = re.compile(r'<weight[^>]*fmt="([^"]*)"', re.S)

    # 按 row 切分
    rows = text.split("<row>")
    for row_text in rows[1:]:  # skip before first <row>
        end = row_text.find("</row>")
        if end >= 0:
            row_text = row_text[:end]

        m_name = proc_pat.search(row_text)
        m_pid = pid_pat.search(row_text)
        m_count = count_pat.search(row_text)
        m_weight = weight_pat.search(row_text)

        name = m_name.group(1).strip() if m_name else ""
        pid = 0
        if m_pid:
            try:
                pid = int(float(m_pid.group(1)))
            except (ValueError, TypeError):
                pass

        weight = 0.0
        if m_weight:
            weight = _parse_mw_value(m_weight.group(1)) or 0.0
        if weight <= 0 and m_count:
            try:
                weight = float(m_count.group(1))
            except (ValueError, TypeError):
                pass

        if name and weight > 0:
            key = (name, pid)
            process_weights[key] += weight
            total_weight += weight

    if total_weight <= 0:
        return []

    samples = []
    for (name, pid), weight in process_weights.items():
        pct = (weight / total_weight) * 100.0
        samples.append({
            "ts": 0.0,
            "pid": pid,
            "name": name,
            "cpu_pct": pct,
        })

    return samples


# ── 3. 功耗归因 ──


def attribute_power(
    power_samples: List[Dict[str, Any]],
    process_cpu_samples: List[Dict[str, Any]],
) -> List[ProcessPower]:
    """
    按 CPU% 比例将总功耗分摊到各进程。

    公式:
      process_power_mw = total_power_mw × (process_cpu_pct / sum_all_cpu_pct)

    处理流程:
    1. 计算平均总功耗 (avg_total_mw)
    2. 计算各进程的平均 CPU%
    3. 按比例分摊功耗
    4. 处理进程消失的情况 (CPU%=0 的进程仍保留但功耗为 0)

    Args:
        power_samples: parse_system_power() 的输出
        process_cpu_samples: parse_process_cpu() 的输出

    Returns:
        [ProcessPower, ...] 按功耗降序排列
    """
    if not power_samples or not process_cpu_samples:
        return []

    # 1. 计算平均总功耗
    total_values = [
        s.get("total_mw", 0) for s in power_samples if s.get("total_mw", 0) > 0
    ]
    if not total_values:
        # 尝试从分量求和
        total_values = []
        for s in power_samples:
            t = s.get("cpu_mw", 0) + s.get("gpu_mw", 0) + \
                s.get("display_mw", 0) + s.get("network_mw", 0)
            if t > 0:
                total_values.append(t)

    if not total_values:
        return []

    avg_total_mw = sum(total_values) / len(total_values)

    # 2. 计算各进程的平均 CPU%
    process_cpu_map: Dict[Tuple[str, int], List[float]] = defaultdict(list)
    for s in process_cpu_samples:
        pid = s.get("pid", 0)
        name = s.get("name", "")
        cpu = s.get("cpu_pct", 0)
        if name:
            process_cpu_map[(name, pid)].append(float(cpu))

    # 3. 按比例分摊
    process_avg: List[Tuple[str, int, float]] = []
    for (name, pid), cpu_list in process_cpu_map.items():
        avg_cpu = sum(cpu_list) / len(cpu_list) if cpu_list else 0.0
        process_avg.append((name, pid, avg_cpu))

    sum_cpu = sum(avg for _, _, avg in process_avg)

    results: List[ProcessPower] = []
    for name, pid, avg_cpu in process_avg:
        if sum_cpu > 0:
            power_mw = avg_total_mw * (avg_cpu / sum_cpu)
            pct_of_total = (avg_cpu / sum_cpu) * 100.0
        else:
            power_mw = 0.0
            pct_of_total = 0.0

        results.append(ProcessPower(
            name=name,
            pid=pid,
            avg_cpu_pct=avg_cpu,
            power_mw=power_mw,
            pct_of_total=pct_of_total,
        ))

    # 按功耗降序
    results.sort(key=lambda p: p.power_mw, reverse=True)
    return results


def attribute_power_multidim(
    power_samples: List[Dict[str, Any]],
    process_cpu_samples: List[Dict[str, Any]],
    network_metrics: Optional[List[Dict[str, Any]]] = None,
    gpu_metrics: Optional[List[Dict[str, Any]]] = None,
) -> List[ProcessPower]:
    """
    多维功耗归因: CPU + GPU + Network + Display 子系统级分摊。

    在 attribute_power() 的 CPU 线性分摊基础上，进一步利用:
    - power_samples 中已解析的 cpu_mw/gpu_mw/display_mw/network_mw 分项
    - network_metrics: DvtBridge network 数据 [{pid, name, rx_bytes, tx_bytes}, ...]
    - gpu_metrics: DvtBridge graphics 数据 [{pid, name, gpu_time_pct}, ...]

    归因策略:
    - CPU 子系统功耗: 按 CPU% 比例分摊 cpu_mw
    - GPU 子系统功耗: 按 gpu_time_pct 分摊 gpu_mw (无 gpu_metrics 则按 CPU% 回退)
    - Network 子系统功耗: 按 rx+tx bytes 比例分摊 network_mw (无数据则按 CPU% 回退)
    - Display 子系统功耗: 不分摊到进程 (屏幕功耗是共享资源)

    Returns:
        [ProcessPower, ...] 按总功耗降序，source="multidim"
    """
    if not power_samples:
        return []

    # ── 计算各子系统平均功耗 ──
    def _avg_field(samples: List[Dict], field: str) -> float:
        vals = [s.get(field, 0) for s in samples if s.get(field, 0) > 0]
        return sum(vals) / len(vals) if vals else 0.0

    avg_cpu_mw = _avg_field(power_samples, "cpu_mw")
    avg_gpu_mw = _avg_field(power_samples, "gpu_mw")
    avg_net_mw = _avg_field(power_samples, "network_mw")
    avg_disp_mw = _avg_field(power_samples, "display_mw")
    avg_total_mw = _avg_field(power_samples, "total_mw")

    if avg_total_mw <= 0:
        avg_total_mw = avg_cpu_mw + avg_gpu_mw + avg_net_mw + avg_disp_mw

    if avg_total_mw <= 0:
        return []

    # ── 1. 计算各进程平均 CPU% ──
    process_cpu_map: Dict[Tuple[str, int], List[float]] = defaultdict(list)
    for s in process_cpu_samples:
        pid = s.get("pid", 0)
        name = s.get("name", "")
        cpu = s.get("cpu_pct", 0)
        if name:
            process_cpu_map[(name, pid)].append(float(cpu))

    process_avg_cpu: Dict[Tuple[str, int], float] = {}
    for key, cpu_list in process_cpu_map.items():
        process_avg_cpu[key] = sum(cpu_list) / len(cpu_list) if cpu_list else 0.0

    sum_cpu = sum(process_avg_cpu.values())
    if sum_cpu <= 0:
        return []

    # ── 2. 构建 Network 比例 (按 rx+tx bytes) ──
    process_net_bytes: Dict[Tuple[str, int], float] = {}
    if network_metrics:
        for m in network_metrics:
            key = (m.get("name", ""), m.get("pid", 0))
            rx = float(m.get("rx_bytes", 0) or 0)
            tx = float(m.get("tx_bytes", 0) or 0)
            process_net_bytes[key] = process_net_bytes.get(key, 0) + rx + tx

    sum_net_bytes = sum(process_net_bytes.values())

    # ── 3. 构建 GPU 比例 ──
    process_gpu_pct: Dict[Tuple[str, int], float] = {}
    if gpu_metrics:
        for m in gpu_metrics:
            key = (m.get("name", ""), m.get("pid", 0))
            gpu_t = float(m.get("gpu_time_pct", 0) or 0)
            process_gpu_pct[key] = process_gpu_pct.get(key, 0) + gpu_t

    sum_gpu_pct = sum(process_gpu_pct.values())

    # ── 4. 多维归因 ──
    results: List[ProcessPower] = []
    for key, avg_cpu in process_avg_cpu.items():
        name, pid = key
        cpu_ratio = avg_cpu / sum_cpu if sum_cpu > 0 else 0.0

        # CPU 子系统: 按 CPU% 比例
        cpu_power = avg_cpu_mw * cpu_ratio

        # GPU 子系统: 按 gpu_time_pct 比例, 无数据则回退到 CPU% 比例
        if sum_gpu_pct > 0 and process_gpu_pct.get(key, 0) > 0:
            gpu_ratio = process_gpu_pct[key] / sum_gpu_pct
        else:
            gpu_ratio = cpu_ratio
        gpu_power = avg_gpu_mw * gpu_ratio

        # Network 子系统: 按 bytes 比例, 无数据则回退到 CPU% 比例
        if sum_net_bytes > 0 and process_net_bytes.get(key, 0) > 0:
            net_ratio = process_net_bytes[key] / sum_net_bytes
        else:
            net_ratio = cpu_ratio
        net_power = avg_net_mw * net_ratio

        # 总功耗 = cpu + gpu + net (display 不归因)
        total_proc_mw = cpu_power + gpu_power + net_power
        pct_of_total = (total_proc_mw / avg_total_mw) * 100.0 if avg_total_mw > 0 else 0.0

        results.append(ProcessPower(
            name=name,
            pid=pid,
            avg_cpu_pct=avg_cpu,
            power_mw=total_proc_mw,
            pct_of_total=pct_of_total,
            cpu_power_mw=cpu_power,
            gpu_power_mw=gpu_power,
            network_power_mw=net_power,
            display_power_mw=0.0,  # display 不归因到进程
            avg_rx_bytes=sum(
                float(m.get("rx_bytes", 0) or 0)
                for m in (network_metrics or [])
                if m.get("name") == name and m.get("pid") == pid
            ),
            avg_tx_bytes=sum(
                float(m.get("tx_bytes", 0) or 0)
                for m in (network_metrics or [])
                if m.get("name") == name and m.get("pid") == pid
            ),
            gpu_time_pct=process_gpu_pct.get(key, 0.0),
            source="multidim",
        ))

    results.sort(key=lambda p: p.power_mw, reverse=True)
    return results


# ── 4. 设备进程树发现 ──


def discover_process_tree(
    device_udid: str,
) -> List[Dict[str, Any]]:
    """
    用 xcrun devicectl device info processes 发现设备进程树。

    识别 WebKit 子进程:
    - com.apple.WebKit.WebContent (JS 执行)
    - com.apple.WebKit.GPU (GPU 渲染)
    - com.apple.WebKit.Networking (网络)

    Returns:
        [{pid, name, path, is_webkit, webkit_role}, ...]
    """
    try:
        proc = subprocess.run(
            [
                "xcrun", "devicectl", "device", "info", "processes",
                "--device", device_udid,
            ],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            logger.warning(
                "discover_process_tree: devicectl failed (rc=%d): %s",
                proc.returncode, proc.stderr[:200],
            )
            return []
    except FileNotFoundError:
        logger.warning("discover_process_tree: xcrun not found")
        return []
    except subprocess.TimeoutExpired:
        logger.warning("discover_process_tree: devicectl timeout")
        return []
    except Exception as exc:
        logger.warning("discover_process_tree: %s", exc)
        return []

    results: List[Dict[str, Any]] = []
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

        is_webkit = "WebKit" in path
        webkit_role = ""
        if "WebKit.WebContent" in path:
            webkit_role = "js"
        elif "WebKit.GPU" in path:
            webkit_role = "gpu"
        elif "WebKit.Networking" in path:
            webkit_role = "network"

        results.append({
            "pid": pid,
            "name": name,
            "path": path,
            "is_webkit": is_webkit,
            "webkit_role": webkit_role,
        })

    logger.info(
        "discover_process_tree: %d processes (%d WebKit) on %s",
        len(results),
        sum(1 for p in results if p["is_webkit"]),
        device_udid,
    )
    return results


# ── 5. 进程生命周期追踪 ──


def track_process_lifecycle(
    session_dir: Path,
    device_udid: str,
    interval_sec: int = 10,
    duration_sec: int = 0,
) -> List[ProcessLifecycleEvent]:
    """
    周期性轮询进程列表，记录进程启停事件。

    通过对比两次 poll 之间的进程列表差异，检测新启动和已退出的进程。
    适用于录制过程中 WebKit 进程动态启停的场景。

    Args:
        session_dir:  会话目录，输出 lifecycle.jsonl
        device_udid:  设备 UDID
        interval_sec: 轮询间隔 (秒)
        duration_sec: 总追踪时长 (秒)，0 表示直到手动停止

    Returns:
        [ProcessLifecycleEvent, ...] 所有检测到的事件
    """
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    events_file = session_dir / "lifecycle.jsonl"
    events: List[ProcessLifecycleEvent] = []
    prev_pids: Dict[int, str] = {}  # pid → name

    start_time = time.time()
    running = True

    def _stop_handler(*_):
        nonlocal running
        running = False

    try:
        signal.signal(signal.SIGTERM, _stop_handler)
    except (OSError, ValueError):
        pass  # 非主线程无法设置 signal

    logger.info(
        "track_process_lifecycle: start (interval=%ds, device=%s)",
        interval_sec, device_udid,
    )

    try:
        while running:
            now = time.time()

            # 检查超时
            if duration_sec > 0 and (now - start_time) >= duration_sec:
                break

            # 获取当前进程列表
            current_procs = discover_process_tree(device_udid)
            current_pids: Dict[int, str] = {
                p["pid"]: p["name"] for p in current_procs
            }

            # 检测新进程
            for pid, name in current_pids.items():
                if pid not in prev_pids:
                    evt = ProcessLifecycleEvent(
                        ts=now,
                        event_type="start",
                        pid=pid,
                        name=name,
                    )
                    events.append(evt)
                    _append_lifecycle_event(events_file, evt)

            # 检测退出进程
            for pid, name in prev_pids.items():
                if pid not in current_pids:
                    evt = ProcessLifecycleEvent(
                        ts=now,
                        event_type="exit",
                        pid=pid,
                        name=name,
                    )
                    events.append(evt)
                    _append_lifecycle_event(events_file, evt)

            prev_pids = current_pids

            # 可中断的 sleep
            deadline = time.time() + interval_sec
            while running and time.time() < deadline:
                time.sleep(min(1.0, deadline - time.time()))

    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.warning("track_process_lifecycle error: %s", exc)

    logger.info(
        "track_process_lifecycle: done, %d events", len(events),
    )
    return events


def _append_lifecycle_event(
    path: Path, event: ProcessLifecycleEvent,
) -> None:
    """追加生命周期事件到 JSONL。"""
    try:
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── 6. 异常检测 ──


def detect_anomalies(
    attribution_data: List[ProcessPower],
    cpu_history: Optional[List[Dict[str, Any]]] = None,
    memory_history: Optional[List[Dict[str, Any]]] = None,
) -> List[AnomalyEvent]:
    """
    检测功耗归因中的异常。

    检测类型:
    1. 僵尸进程: 同名进程数 > 3 (如多个 WebContent 僵尸)
    2. 功耗飙升: 某进程功耗 > 2× 所有进程平均功耗
    3. 内存持续增长: 从 memory_history 检测线性增长趋势

    Args:
        attribution_data: attribute_power() 的输出
        cpu_history: 历史进程 CPU% 采样 (可选)
        memory_history: 历史进程内存采样 (可选)

    Returns:
        [AnomalyEvent, ...]
    """
    anomalies: List[AnomalyEvent] = []
    now = time.time()

    if not attribution_data:
        return anomalies

    # ── 僵尸进程检测 ──
    name_counts: Dict[str, List[ProcessPower]] = defaultdict(list)
    for p in attribution_data:
        name_counts[p.name].append(p)

    ZOMBIE_THRESHOLD = 3
    for name, procs in name_counts.items():
        if len(procs) > ZOMBIE_THRESHOLD:
            pids = [str(p.pid) for p in procs]
            anomalies.append(AnomalyEvent(
                ts=now,
                anomaly_type="zombie",
                process_name=name,
                detail=(
                    f"{len(procs)} instances (PIDs: {', '.join(pids)}), "
                    f"threshold={ZOMBIE_THRESHOLD}"
                ),
            ))

    # ── 功耗飙升检测 ──
    if len(attribution_data) >= 2:
        powers = [p.power_mw for p in attribution_data if p.power_mw > 0]
        if powers:
            avg_power = sum(powers) / len(powers)
            max_reasonable = avg_power * 2.0

            for p in attribution_data:
                if p.power_mw > max_reasonable and avg_power > 0:
                    anomalies.append(AnomalyEvent(
                        ts=now,
                        anomaly_type="power_spike",
                        process_name=p.name,
                        detail=(
                            f"{p.power_mw:.1f} mW is "
                            f"{p.power_mw / avg_power:.1f}x average "
                            f"({avg_power:.1f} mW)"
                        ),
                    ))

    # ── 内存持续增长检测 ──
    if memory_history:
        _detect_memory_growth(anomalies, memory_history, now)

    return anomalies


def _detect_memory_growth(
    anomalies: List[AnomalyEvent],
    memory_history: List[Dict[str, Any]],
    now: float,
) -> None:
    """
    检测内存持续增长趋势。

    策略: 对每个进程，检查最近 N 条采样的内存值是否单调递增，
    且增长幅度超过阈值 (50MB)。
    """
    # 按进程分组
    process_mem: Dict[Tuple[str, int], List[Tuple[float, float]]] = (
        defaultdict(list)
    )
    for record in memory_history:
        name = record.get("name", "")
        pid = record.get("pid", 0)
        ts = record.get("ts", 0)
        mem = record.get("phys_footprint_mb") or record.get("mem_mb")
        if name and mem is not None:
            try:
                mem_val = float(mem)
            except (ValueError, TypeError):
                continue
            process_mem[(name, pid)].append((float(ts), mem_val))

    GROWTH_THRESHOLD_MB = 50.0
    MIN_SAMPLES = 3

    for (name, pid), entries in process_mem.items():
        if len(entries) < MIN_SAMPLES:
            continue

        # 按时间排序
        entries.sort(key=lambda e: e[0])

        # 检查最近 MIN_SAMPLES 条是否单调递增
        recent = entries[-MIN_SAMPLES:]
        mems = [m for _, m in recent]
        is_monotonic = all(mems[i] < mems[i + 1] for i in range(len(mems) - 1))

        if is_monotonic:
            growth = mems[-1] - mems[0]
            if growth >= GROWTH_THRESHOLD_MB:
                anomalies.append(AnomalyEvent(
                    ts=now,
                    anomaly_type="memory_growth",
                    process_name=name,
                    detail=(
                        f"grew {growth:.1f} MB over "
                        f"{MIN_SAMPLES} samples "
                        f"({mems[0]:.1f} → {mems[-1]:.1f} MB)"
                    ),
                ))


# ── 7. 格式化报告 ──


def format_attribution_report(
    attribution: List[ProcessPower],
    anomalies: Optional[List[AnomalyEvent]] = None,
    power_samples: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    格式化功耗归因报告。

    输出示例:
    ══════════ Power Attribution Report ══════════
    Total Power: 2345.6 mW (avg over 60 samples)

    Top Processes by Power Consumption:
    ─────────────────────────────────────────────────
    #   Process           PID   CPU%    Power(mW)  %Total
    ─────────────────────────────────────────────────────────
    1.  SoulApp           1234  35.2%   825.3      35.2%
    2.  WebContent        5678  22.1%   518.4      22.1%
    ...
    """
    if not attribution:
        return "  (无功耗归因数据)"

    lines: List[str] = []
    sep = "─" * 55

    # ── Header ──
    lines.append(f"{'═' * 12} Power Attribution Report {'═' * 12}")

    if power_samples:
        totals = [
            s.get("total_mw", 0) for s in power_samples
            if s.get("total_mw", 0) > 0
        ]
        if totals:
            avg = sum(totals) / len(totals)
            lines.append(
                f"Total Power: {avg:.1f} mW (avg over {len(totals)} samples)"
            )

            # 分项
            cpu_vals = [
                s.get("cpu_mw", 0) for s in power_samples
                if s.get("cpu_mw", 0) > 0
            ]
            gpu_vals = [
                s.get("gpu_mw", 0) for s in power_samples
                if s.get("gpu_mw", 0) > 0
            ]
            disp_vals = [
                s.get("display_mw", 0) for s in power_samples
                if s.get("display_mw", 0) > 0
            ]
            net_vals = [
                s.get("network_mw", 0) for s in power_samples
                if s.get("network_mw", 0) > 0
            ]
            breakdown = []
            if cpu_vals:
                breakdown.append(
                    f"CPU={sum(cpu_vals) / len(cpu_vals):.1f}"
                )
            if gpu_vals:
                breakdown.append(
                    f"GPU={sum(gpu_vals) / len(gpu_vals):.1f}"
                )
            if disp_vals:
                breakdown.append(
                    f"Display={sum(disp_vals) / len(disp_vals):.1f}"
                )
            if net_vals:
                breakdown.append(
                    f"Net={sum(net_vals) / len(net_vals):.1f}"
                )
            if breakdown:
                lines.append(f"Breakdown (mW): {' | '.join(breakdown)}")

    lines.append("")
    lines.append("Top Processes by Power Consumption:")
    lines.append(sep)
    lines.append(
        f"{'#':<4} {'Process':<18} {'PID':<7} "
        f"{'CPU%':>7} {'Power(mW)':>11} {'%Total':>8}"
    )
    lines.append(sep)

    for i, p in enumerate(attribution, 1):
        name = p.name[:16] if len(p.name) > 16 else p.name
        lines.append(
            f"{i:<4} {name:<18} {p.pid:<7} "
            f"{p.avg_cpu_pct:>6.1f}% {p.power_mw:>10.1f} {p.pct_of_total:>7.1f}%"
        )

        # P2: 多维归因时显示子系统拆分
        if p.source == "multidim" and (
            p.cpu_power_mw > 0 or p.gpu_power_mw > 0 or p.network_power_mw > 0
        ):
            parts = []
            if p.cpu_power_mw > 0:
                parts.append(f"CPU={p.cpu_power_mw:.1f}")
            if p.gpu_power_mw > 0:
                parts.append(f"GPU={p.gpu_power_mw:.1f}")
            if p.network_power_mw > 0:
                parts.append(f"Net={p.network_power_mw:.1f}")
            if p.gpu_time_pct > 0:
                parts.append(f"gpu%={p.gpu_time_pct:.1f}")
            if p.avg_rx_bytes > 0 or p.avg_tx_bytes > 0:
                parts.append(f"rx={p.avg_rx_bytes:.0f} tx={p.avg_tx_bytes:.0f}")
            if parts:
                lines.append(f"     ^-- {' | '.join(parts)} (mW)")

    lines.append(sep)

    total_attributed = sum(p.power_mw for p in attribution)
    total_cpu = sum(p.avg_cpu_pct for p in attribution)
    lines.append(
        f"Total: {len(attribution)} processes, "
        f"{total_attributed:.1f} mW attributed, "
        f"{total_cpu:.1f}% total CPU"
    )

    # ── Anomalies ──
    if anomalies:
        lines.append("")
        lines.append(f"{'═' * 12} Anomalies ({len(anomalies)}) {'═' * 12}")
        for a in anomalies:
            icon = {
                "zombie": "!!",
                "power_spike": "!!",
                "memory_growth": "! ",
            }.get(a.anomaly_type, "??")

            ts_str = time.strftime(
                "%H:%M:%S", time.localtime(a.ts)
            ) if a.ts else "?"
            lines.append(
                f"  [{icon}] {a.anomaly_type} | "
                f"{a.process_name} | {a.detail}"
            )

    return "\n".join(lines)


# ── 辅助: 读取 lifecycle JSONL ──


def read_lifecycle_events(
    path: Path, last_n: int = 0,
) -> List[ProcessLifecycleEvent]:
    """读取 lifecycle.jsonl 文件。"""
    path = Path(path)
    if not path.exists():
        return []

    events: List[ProcessLifecycleEvent] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []

    for line in text.strip().splitlines():
        try:
            d = json.loads(line.strip())
            events.append(ProcessLifecycleEvent(
                ts=d.get("ts", 0),
                event_type=d.get("event_type", ""),
                pid=d.get("pid", 0),
                name=d.get("name", ""),
            ))
        except (json.JSONDecodeError, KeyError):
            continue

    if last_n > 0:
        events = events[-last_n:]
    return events
