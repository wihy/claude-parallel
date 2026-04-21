"""Time Profiler 调用栈分析 + 文本格式化。

从 session.py 搬出 — session 只负责生命周期, 本模块负责:
- 从 meta 找 Time Profiler / systrace trace 文件
- 并行 export schema → parse XML → aggregate Top-N 热点函数 & 调用路径
- 结果的可读文本呈现
"""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..decode.timeprofiler import export_xctrace_schema, parse_timeprofiler_xml


_TIME_TEMPLATES = ("time", "time profiler", "systrace", "systemtrace", "system trace")


def main_has_timeprofiler(meta: Dict[str, Any]) -> bool:
    """检查主链路 xctrace 是否已包含 Time Profiler 模板。"""
    tpl = meta.get("xctrace", {}).get("template", "")
    if tpl.lower() in ("time", "time profiler"):
        return True
    for entry in meta.get("xctrace_multi", []):
        if entry.get("template", "").lower() in ("time", "time profiler"):
            return True
    return False


def find_timeprofiler_traces(meta: Dict[str, Any], traces_dir: Optional[Path] = None) -> List[Path]:
    """查找含 Time Profiler 数据的 trace 文件 (包括 systemtrace)。"""
    traces: List[Path] = []

    # 单模板场景
    tpl_name = meta.get("xctrace", {}).get("template", "")
    trace_str = meta.get("xctrace", {}).get("trace", "")
    if tpl_name.lower() in _TIME_TEMPLATES and trace_str:
        p = Path(trace_str)
        if p.exists():
            traces.append(p)

    # 多模板场景
    for entry in meta.get("xctrace_multi", []):
        tpl = entry.get("template", "")
        trace_str = entry.get("trace", "")
        if tpl.lower() in _TIME_TEMPLATES and trace_str:
            p = Path(trace_str)
            if p.exists():
                traces.append(p)

    # 兜底: 在 traces_dir 中搜索含 time 或 systrace 的文件
    if not traces and traces_dir is not None and traces_dir.exists():
        for pattern in ("*time*.trace", "*systrace*.trace"):
            for f in traces_dir.glob(pattern):
                if f not in traces:
                    traces.append(f)

    return traces


def _safe_export_schema(trace_file: Path, schema: str, output: Path) -> None:
    try:
        export_xctrace_schema(trace_file, schema, output)
    except Exception:
        pass


def analyze_callstack(
    meta: Dict[str, Any],
    exports_dir: Path,
    traces_dir: Optional[Path] = None,
    top_n: int = 20,
    min_weight: float = 0.5,
    flatten: bool = True,
    full_stack: bool = False,
    time_from: float = 0,
    time_to: float = 0,
) -> Dict[str, Any]:
    """
    解析 Time Profiler 调用栈, 返回热点函数排名 + 调用路径。

    Args:
        meta:         session meta.json 字典 (含 xctrace / xctrace_multi)
        exports_dir:  schema 导出落地目录
        traces_dir:   trace 文件目录 (用于兜底搜索, 可选)
        top_n:        返回前 N 个热点
        min_weight:   最小权重百分比
        flatten:      True=按函数聚合, False=不聚合只看 path
        full_stack:   True=保留完整调用链, False=只取叶子函数
        time_from:    时间切片起点 (秒, 0=不限)
        time_to:      时间切片终点 (秒, 0=不限)
    """
    if meta.get("status") not in ("stopped", "running"):
        return {"error": "没有已完成的 perf 采集会话", "hot_functions": [], "call_paths": []}

    trace_files = find_timeprofiler_traces(meta, traces_dir)
    if not trace_files:
        return {
            "error": "未找到 Time Profiler trace (录制时需要 --templates time)",
            "hot_functions": [],
            "call_paths": [],
        }

    t_range = None
    if time_from > 0 or time_to > 0:
        t_range = (time_from, time_to if time_to > 0 else float("inf"))

    def _export_and_parse(trace_file: Path) -> List[Tuple[str, float]]:
        xml_path = exports_dir / f"time_profile_{trace_file.stem}.xml"
        _safe_export_schema(trace_file, "time-profile", xml_path)
        if not xml_path.exists() or xml_path.stat().st_size < 100:
            xml_path = exports_dir / f"TimeProfiler_{trace_file.stem}.xml"
            _safe_export_schema(trace_file, "TimeProfiler", xml_path)
        if not xml_path.exists():
            return []
        return parse_timeprofiler_xml(
            xml_path,
            keep_full_stack=full_stack,
            time_range=t_range,
        )

    all_samples: List[Tuple[str, float]] = []
    with ThreadPoolExecutor(max_workers=min(len(trace_files), 4)) as pool:
        futures = [pool.submit(_export_and_parse, tf) for tf in trace_files]
        for f in futures:
            try:
                all_samples.extend(f.result())
            except Exception:
                pass

    if not all_samples:
        return {
            "error": "TimeProfiler XML 中无采样数据",
            "hot_functions": [],
            "call_paths": [],
            "total_samples": 0,
        }

    total_samples = len(all_samples)

    if flatten:
        # 按函数聚合权重
        func_weight: Dict[str, float] = defaultdict(float)
        for frame, weight in all_samples:
            func_weight[frame] += weight
        hot = sorted(func_weight.items(), key=lambda x: x[1], reverse=True)
        hot_functions = []
        for func, w in hot[:top_n]:
            pct = w / total_samples * 100.0
            if pct < min_weight:
                break
            hot_functions.append({
                "symbol": func,
                "samples": int(round(w)),
                "weight_pct": round(pct, 2),
            })
    else:
        hot_functions = []

    # 提取 Top N 完整调用路径
    path_weight: Dict[str, float] = defaultdict(float)
    for frame, weight in all_samples:
        path_weight[frame] += weight
    top_paths = sorted(path_weight.items(), key=lambda x: x[1], reverse=True)[:top_n]
    call_paths = []
    for path, w in top_paths:
        pct = w / total_samples * 100.0
        if pct < min_weight:
            break
        frames = [f.strip() for f in path.split(" → ") if f.strip()]
        call_paths.append({
            "frames": frames,
            "depth": len(frames),
            "samples": int(round(w)),
            "weight_pct": round(pct, 2),
            "leaf": frames[-1] if frames else "",
        })

    return {
        "source": str(trace_files[0]),
        "total_samples": total_samples,
        "hot_functions": hot_functions,
        "call_paths": call_paths,
        "summary": {
            "unique_symbols": len(set(f for f, _ in all_samples)),
            "top_symbol": hot_functions[0]["symbol"] if hot_functions else "",
            "top_weight_pct": hot_functions[0]["weight_pct"] if hot_functions else 0.0,
        },
    }


def format_callstack_text(data: Dict[str, Any], max_depth: int = 8) -> str:
    """将调用栈分析结果格式化为可读文本。"""
    if "error" in data:
        return f"  [错误] {data['error']}"

    lines = []
    total = data.get("total_samples", 0)
    lines.append(f"  总采样数: {total}")
    summary = data.get("summary", {})
    lines.append(f"  唯一函数: {summary.get('unique_symbols', 0)}")
    lines.append("")

    # 热点函数 Top N
    hot = data.get("hot_functions", [])
    if hot:
        lines.append("  ── 热点函数 (按采样权重排序) ──")
        lines.append("")
        max_sym_len = max(len(h["symbol"]) for h in hot[:10])
        for i, h in enumerate(hot):
            bar_len = int(h["weight_pct"] / 2)
            bar = "█" * bar_len
            sym = h["symbol"][:80]
            lines.append(
                f"  {i+1:2d}. {sym:<{max_sym_len}}  {h['weight_pct']:5.1f}%  "
                f"({h['samples']} samples)  {bar}"
            )
        lines.append("")

    # 调用路径 Top N
    paths = data.get("call_paths", [])
    if paths:
        lines.append("  ── 调用路径 (从调用者到被调用者) ──")
        lines.append("")
        for i, p in enumerate(paths[:10]):
            leaf = p["leaf"]
            lines.append(f"  {i+1:2d}. {leaf}  ({p['weight_pct']}%, depth={p['depth']})")
            frames = p.get("frames", [])
            display_frames = frames[:max_depth]
            for j, frame in enumerate(display_frames):
                indent = "      " + "  " * j
                arrow = "→ " if j > 0 else "  "
                lines.append(f"{indent}{arrow}{frame}")
            if len(frames) > max_depth:
                lines.append(f"      ... ({len(frames) - max_depth} more frames)")
            lines.append("")

    return "\n".join(lines)
