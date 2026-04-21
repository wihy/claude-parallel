"""DvtBridge 进程/系统指标聚合 + 文本格式化。

从 session.py 搬出 — session 只负责生命周期, 本模块负责 JSONL → stats dict 汇总
以及汇总结果的可读文本呈现。
"""

from pathlib import Path
from typing import Any, Dict, List, Optional


def build_dvt_metrics_report(meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """分析 DvtBridge 采集的进程/系统指标 JSONL, 返回统计摘要。

    meta 需要带 device_metrics.source == 'dvt_bridge' 且 process_jsonl/system_jsonl 路径。
    无数据返回 None。
    """
    dm = meta.get("device_metrics", {})
    if dm.get("source") != "dvt_bridge":
        return None

    from ..protocol.dvt import read_dvt_process_jsonl, read_dvt_system_jsonl

    process_jsonl = Path(dm.get("process_jsonl", ""))
    system_jsonl = Path(dm.get("system_jsonl", ""))

    result: Dict[str, Any] = {"source": "dvt_bridge"}

    # 进程指标统计
    proc_records = read_dvt_process_jsonl(process_jsonl) if process_jsonl.exists() else []
    if proc_records:
        by_name: Dict[str, List[Dict[str, Any]]] = {}
        for r in proc_records:
            name = r.get("name", "unknown")
            by_name.setdefault(name, []).append(r)

        proc_stats = {}
        for name, records in by_name.items():
            cpu_vals = [r["cpuUsage"] for r in records if isinstance(r.get("cpuUsage"), (int, float))]
            mem_vals = [r["physFootprintMB"] for r in records if isinstance(r.get("physFootprintMB"), (int, float))]
            thread_vals = [r["threadCount"] for r in records if isinstance(r.get("threadCount"), (int, float))]

            proc_stats[name] = {
                "samples": len(records),
                "pid": records[-1].get("pid", "?"),
                "cpu_pct": {
                    "avg": round(sum(cpu_vals) / len(cpu_vals), 2) if cpu_vals else None,
                    "peak": round(max(cpu_vals), 2) if cpu_vals else None,
                    "min": round(min(cpu_vals), 2) if cpu_vals else None,
                },
                "mem_mb": {
                    "avg": round(sum(mem_vals) / len(mem_vals), 1) if mem_vals else None,
                    "peak": round(max(mem_vals), 1) if mem_vals else None,
                    "min": round(min(mem_vals), 1) if mem_vals else None,
                },
                "threads": {
                    "avg": round(sum(thread_vals) / len(thread_vals), 1) if thread_vals else None,
                    "peak": max(thread_vals) if thread_vals else None,
                },
            }

        result["process_stats"] = proc_stats
        result["total_process_samples"] = len(proc_records)

    # 系统指标统计
    sys_records = read_dvt_system_jsonl(system_jsonl) if system_jsonl.exists() else []
    if sys_records:
        cpu_total = [r["cpuTotal"] for r in sys_records if isinstance(r.get("cpuTotal"), (int, float))]
        mem_free = [r["physMemoryFreeMB"] for r in sys_records if isinstance(r.get("physMemoryFreeMB"), (int, float))]
        mem_used = [r["physMemoryUsedMB"] for r in sys_records if isinstance(r.get("physMemoryUsedMB"), (int, float))]

        result["system_stats"] = {
            "samples": len(sys_records),
            "cpu_total_pct": {
                "avg": round(sum(cpu_total) / len(cpu_total), 2) if cpu_total else None,
                "peak": round(max(cpu_total), 2) if cpu_total else None,
            },
            "mem_free_mb": {
                "avg": round(sum(mem_free) / len(mem_free), 1) if mem_free else None,
                "min": round(min(mem_free), 1) if mem_free else None,
            },
            "mem_used_mb": {
                "avg": round(sum(mem_used) / len(mem_used), 1) if mem_used else None,
                "peak": round(max(mem_used), 1) if mem_used else None,
            },
        }

    return result if result.get("process_stats") or result.get("system_stats") else None


def format_dvt_metrics_text(dvt_data: Dict[str, Any]) -> str:
    """将 DvtBridge 指标分析结果格式化为可读文本。"""
    if not dvt_data:
        return "  (无 DvtBridge 数据)"

    lines = []
    lines.append("  ── DvtBridge 实时指标 ──")
    lines.append("")

    # 进程统计
    proc_stats = dvt_data.get("process_stats", {})
    for name, stats in sorted(proc_stats.items(), key=lambda x: -(x[1].get("cpu_pct", {}).get("avg") or 0)):
        cpu = stats.get("cpu_pct", {})
        mem = stats.get("mem_mb", {})
        samples = stats.get("samples", 0)

        cpu_avg = cpu.get("avg", "?")
        cpu_peak = cpu.get("peak", "?")
        mem_avg = mem.get("avg", "?")
        mem_peak = mem.get("peak", "?")

        cpu_avg_str = f"{cpu_avg:.1f}%" if isinstance(cpu_avg, (int, float)) else "?"
        cpu_peak_str = f"{cpu_peak:.1f}%" if isinstance(cpu_peak, (int, float)) else "?"
        mem_avg_str = f"{mem_avg:.0f}MB" if isinstance(mem_avg, (int, float)) else "?"
        mem_peak_str = f"{mem_peak:.0f}MB" if isinstance(mem_peak, (int, float)) else "?"

        lines.append(
            f"  {name}(pid={stats.get('pid', '?')})  "
            f"CPU: avg={cpu_avg_str} peak={cpu_peak_str}  "
            f"MEM: avg={mem_avg_str} peak={mem_peak_str}  "
            f"({samples} samples)"
        )

    # 系统统计
    sys_stats = dvt_data.get("system_stats", {})
    if sys_stats:
        lines.append("")
        cpu_total = sys_stats.get("cpu_total_pct", {})
        mem_free = sys_stats.get("mem_free_mb", {})
        cpu_avg = cpu_total.get("avg")
        mem_min = mem_free.get("min")
        if cpu_avg is not None:
            lines.append(f"  系统CPU: avg={cpu_avg:.1f}%")
        if mem_min is not None:
            lines.append(f"  可用内存: min={mem_min:.0f}MB")

    return "\n".join(lines)
