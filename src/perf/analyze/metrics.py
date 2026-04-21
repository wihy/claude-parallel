"""Power/Process 指标解析 + 基线对比 + 门禁判定。

从 session.py 搬出 — session 只负责编排, 本模块负责:
- 从 xctrace trace 导出 schema XML → 抽取 Display/CPU/Networking 样本
- 基线 vs 当前的 delta 计算
- 阈值 gate 判定
"""

import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..decode.timeprofiler import export_xctrace_schema


def _avg(arr: List[float]) -> Optional[float]:
    if not arr:
        return None
    return round(sum(arr) / len(arr), 4)


def _safe_export_schema(trace_file: Path, schema: str, output: Path) -> None:
    """export_xctrace_schema 的 try/except 包装, 失败时静默吞。"""
    try:
        export_xctrace_schema(trace_file, schema, output)
    except Exception:
        pass


def extract_column_values(xml_file: Path, column_name: str) -> List[float]:
    """从 xctrace export 出来的 XML 里抽指定列的数值。"""
    if not xml_file.exists():
        return []
    try:
        text = xml_file.read_text(errors="replace")
    except Exception:
        return []

    columns = []
    for m in re.finditer(r'<col[^>]*name="([^"]+)"', text):
        columns.append(m.group(1))
    if not columns:
        return []
    idx = None
    for i, name in enumerate(columns):
        if name.lower() == column_name.lower():
            idx = i
            break
    if idx is None:
        return []

    vals: List[float] = []
    row_pat = re.compile(r"<row>(.*?)</row>", re.S)
    cell_pat = re.compile(r"<c[^>]*>(.*?)</c>", re.S)
    for row_m in row_pat.finditer(text):
        row = row_m.group(1)
        cells = [c.strip() for c in cell_pat.findall(row)]
        if idx < len(cells):
            try:
                vals.append(float(cells[idx]))
            except Exception:
                continue
    return vals


def compute_trace_metrics(meta: Dict[str, Any], exports_dir: Path) -> Dict[str, Any]:
    """从主 xctrace trace 导出 Power 相关 schema, 返回 display/cpu/networking 平均值。"""
    trace_str = meta.get("xctrace", {}).get("trace", "")
    if not trace_str:
        return {"source": "none", "display_avg": None, "cpu_avg": None, "networking_avg": None}
    trace_file = Path(trace_str)
    if not trace_file.exists():
        return {"source": "none", "display_avg": None, "cpu_avg": None, "networking_avg": None}

    power_xml = exports_dir / "SystemPowerLevel.xml"
    proc_xml = exports_dir / "ProcessSubsystemPowerImpact.xml"

    # 并行导出两个 schema
    with ThreadPoolExecutor(max_workers=2) as pool:
        pool.submit(_safe_export_schema, trace_file, "SystemPowerLevel", power_xml)
        pool.submit(_safe_export_schema, trace_file, "ProcessSubsystemPowerImpact", proc_xml)

    display_vals = extract_column_values(power_xml, "Display")
    cpu_vals = extract_column_values(proc_xml, "CPU")
    net_vals = extract_column_values(proc_xml, "Networking")

    return {
        "source": str(trace_file),
        "display_avg": _avg(display_vals),
        "cpu_avg": _avg(cpu_vals),
        "networking_avg": _avg(net_vals),
        "display_samples": len(display_vals),
        "cpu_samples": len(cpu_vals),
        "networking_samples": len(net_vals),
    }


def calc_delta(base: Dict[str, Any], cur: Dict[str, Any]) -> Dict[str, Any]:
    """基线 vs 当前的百分比差值。"""
    def pct(a, b):
        if a is None or b is None or a == 0:
            return None
        return (b - a) / a * 100.0
    return {
        "display_avg_pct": pct(base.get("display_avg"), cur.get("display_avg")),
        "cpu_avg_pct": pct(base.get("cpu_avg"), cur.get("cpu_avg")),
        "networking_avg_pct": pct(base.get("networking_avg"), cur.get("networking_avg")),
    }


def gate_check(delta: Dict[str, Any], threshold_pct: float) -> Dict[str, Any]:
    """任一维度超过 threshold_pct 就 fail。"""
    reasons = []
    for key in ("display_avg_pct", "cpu_avg_pct", "networking_avg_pct"):
        v = delta.get(key)
        if v is not None and v > threshold_pct:
            reasons.append(f"{key}={v:.1f}% > {threshold_pct:.1f}%")
    return {
        "checked": True,
        "passed": len(reasons) == 0,
        "reason": "; ".join(reasons) if reasons else "ok",
    }
