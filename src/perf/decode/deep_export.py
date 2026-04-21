"""
DeepExport — 深度 Schema 采集模块。

从 xctrace trace 文件中导出并解析 GPU 帧耗时、网络连接统计、
虚拟内存分配热点、Metal shader 耗时等深度诊断 schema。

工作流:
1. 探测 trace 有哪些可用 schema
2. 按需调用 xcrun xctrace export 导出 XML
3. 用 iterparse 流式解析（内存恒定）
4. 统计分析 + 格式化报告
"""

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree.ElementTree import iterparse

logger = logging.getLogger(__name__)


# ── 深度 Schema 配置 ──

DEEP_SCHEMAS: Dict[str, Dict[str, str]] = {
    "gpu-frame-time": {
        "xpath": '/trace-toc/run/data/table[@schema="gpu-frame-time"]',
        "desc": "GPU 帧耗时/掉帧",
    },
    "network-connection-stat": {
        "xpath": '/trace-toc/run/data/table[@schema="network-connection-stat"]',
        "desc": "网络连接/流量/延迟",
    },
    "vm-tracking": {
        "xpath": '/trace-toc/run/data/table[@schema="virtual-memory"]',
        "desc": "虚拟内存分配热点",
    },
    "metal-performance": {
        "xpath": '/trace-toc/run/data/table[@schema="metal-performance"]',
        "desc": "Metal GPU shader 耗时",
    },
}


# ── 数据结构 ──

@dataclass
class FrameTimeStats:
    """GPU 帧耗时统计摘要。"""
    avg_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    total_frames: int
    dropped_frames: int


# ── 数值解析辅助 ──

def _safe_float(text: str, default: float = 0.0) -> float:
    """安全解析字符串为 float，去除单位等非数字字符。"""
    if not text:
        return default
    parts = text.strip().split()
    if not parts:
        return default
    token = parts[0]
    cleaned = ""
    for j, ch in enumerate(token):
        if ch in "0123456789.":
            cleaned += ch
        elif ch == "-" and j == 0:
            cleaned += ch
    if not cleaned:
        return default
    try:
        val = float(cleaned)
        # 单位转换
        if len(parts) > 1:
            unit = parts[1].lower()
            if "µ" in unit or unit == "us" or unit == "μs":
                val /= 1000.0      # µs → ms
            elif unit == "s":
                val *= 1000.0      # s → ms
            elif unit == "kb":
                val /= 1024.0      # KB → MB
            elif unit == "gb":
                val *= 1024.0      # GB → MB
        return val
    except ValueError:
        return default


def _safe_int(text: str, default: int = 0) -> int:
    """安全解析字符串为 int。"""
    try:
        return int(float(str(text).strip()))
    except (ValueError, TypeError):
        return default


def _percentile(sorted_vals: List[float], pct: float) -> float:
    """计算百分位数（输入须已排序）。"""
    if not sorted_vals:
        return 0.0
    idx = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


# ── Schema 探测 ──

def probe_trace_schemas(trace_file: Path) -> List[str]:
    """
    探测 trace 文件包含的所有 schema 名称。

    调用 xcrun xctrace export --input <trace>（不带 --xpath），
    解析输出中的可用 schema 列表。
    """
    cmd = [
        "xcrun", "xctrace", "export",
        "--input", str(trace_file),
    ]
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, text=True, timeout=60,
        )
        output = result.stdout + result.stderr
    except Exception as exc:
        logger.warning("probe_trace_schemas failed: %s", exc)
        return []

    schemas: List[str] = []
    seen = set()
    for line in output.splitlines():
        line = line.strip()
        if "schema=" in line:
            # 提取 schema="xxx" 中的 xxx
            start = line.find('schema="')
            if start >= 0:
                start += len('schema="')
                end = line.find('"', start)
                if end > start:
                    name = line[start:end]
                    if name not in seen:
                        seen.add(name)
                        schemas.append(name)
    return schemas


# ── 单 schema 导出 ──

def export_deep_schema(
    trace_file: Path,
    schema_name: str,
    output_path: Path,
) -> Optional[Path]:
    """
    调用 xcrun xctrace export 导出单个 schema 的 XML 文件。

    Args:
        trace_file:  .trace 文件路径
        schema_name: DEEP_SCHEMAS 中的 key（如 'gpu-frame-time'）
        output_path: 导出 XML 的目标路径

    Returns:
        成功返回 output_path，失败返回 None
    """
    cfg = DEEP_SCHEMAS.get(schema_name)
    if not cfg:
        logger.error("Unknown deep schema: %s", schema_name)
        return None

    xpath = cfg["xpath"]
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "xcrun", "xctrace", "export",
        "--input", str(trace_file),
        "--xpath", xpath,
        "--output", str(output_path),
    ]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, text=True, timeout=120,
        )
        if output_path.exists() and output_path.stat().st_size > 0:
            logger.info(
                "export_deep_schema: %s → %s (%d bytes)",
                schema_name, output_path, output_path.stat().st_size,
            )
            return output_path
        # xctrace 可能把错误信息写到 stdout
        if proc.returncode != 0:
            logger.warning(
                "xctrace export failed for %s: rc=%d stderr=%s",
                schema_name, proc.returncode, proc.stderr[:200],
            )
        return None
    except subprocess.TimeoutExpired:
        logger.warning("xctrace export timeout for %s", schema_name)
        return None
    except Exception as exc:
        logger.warning("export_deep_schema error for %s: %s", schema_name, exc)
        return None


# ── 通用 iterparse 行解析器 ──

def _iterparse_rows(
    xml_path: Path,
    row_handler,  # Callable[[Dict[str, str]], None]
) -> None:
    """
    通用 iterparse 行遍历器。对每个 <row> 提取 {列名: 值} 字典，
    交给 row_handler 处理后释放元素（内存恒定）。

    支持两种 XML 列定义格式:
    - <col name="ColumnName"/>
    - <col><name>ColumnName</name></col>

    支持两种行值格式:
    - <c fmt="1.00 ms"/>
    - <c>42</c>
    """
    columns: List[str] = []
    in_row = False
    in_col_name = False
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
                in_col_name = True
            continue

        # event == "end"

        # ── 列定义阶段 ──
        if tag == "name" and in_col_name:
            col_name_buf = (elem.text or "").strip()
            if col_name_buf and col_name_buf not in columns:
                columns.append(col_name_buf)

        elif tag in ("schema", "row-schema"):
            in_col_name = False

        elif tag == "col" and not columns:
            # <col name="X"/> 格式
            cname = elem.get("name", "")
            if cname and cname not in columns:
                columns.append(cname)

        # ── 行数据阶段 ──
        elif in_row:
            # 尝试提取 <c> 的值
            if tag == "c" or (columns and tag in columns):
                fmt = elem.get("fmt", "")
                text_val = (elem.text or "").strip()
                value = fmt if fmt else text_val

                if col_idx < len(columns):
                    cname = columns[col_idx]
                    row_values[cname] = value
                col_idx += 1

            # 有些 XML 用 mnemonic 标签直接在 row 内
            elif tag not in ("row",) and tag in (elem.tag,):
                fmt = elem.get("fmt", "")
                text_val = (elem.text or "").strip()
                value = fmt if fmt else text_val
                if value and tag not in columns:
                    columns.append(tag)
                    row_values[tag] = value
                elif tag in columns:
                    row_values[tag] = value

        # ── 行结束 ──
        if tag == "row":
            in_row = False
            if row_values:
                row_handler(row_values)
            elem.clear()


# ── GPU 帧耗时解析 ──

def parse_gpu_frame_time(xml_path: Path) -> Dict[str, Any]:
    """
    解析 GPU 帧耗时 XML，返回帧记录和统计。

    Returns:
        {
            "frames": [{"frame_time_ms": float, "gpu_pid": int, "dropped": bool}, ...],
            "stats": FrameTimeStats as dict,
        }
    """
    if not xml_path.exists():
        return {"frames": [], "stats": None}

    frames: List[Dict[str, Any]] = []

    def _handle_row(vals: Dict[str, str]) -> None:
        frame_time_ms = 0.0
        gpu_pid = 0
        dropped = False

        for key, val in vals.items():
            kl = key.lower()
            if "frame" in kl and ("time" in kl or "duration" in kl or "ms" in kl):
                frame_time_ms = _safe_float(val)
            elif "gpu" in kl and "pid" in kl:
                gpu_pid = _safe_int(val)
            elif "pid" in kl:
                gpu_pid = _safe_int(val)
            elif "drop" in kl:
                dropped = val.lower() in ("true", "1", "yes")
            elif "frame" in kl and "ms" not in kl:
                # 可能直接就是帧时间数值
                try:
                    frame_time_ms = _safe_float(val)
                except (ValueError, TypeError):
                    pass

        # 如果只有一列数值，当作 frame_time
        if frame_time_ms == 0.0 and len(vals) == 1:
            frame_time_ms = _safe_float(list(vals.values())[0])

        frames.append({
            "frame_time_ms": frame_time_ms,
            "gpu_pid": gpu_pid,
            "dropped": dropped,
        })

    try:
        _iterparse_rows(xml_path, _handle_row)
    except Exception as exc:
        logger.warning("parse_gpu_frame_time error: %s", exc)

    stats = _compute_frame_stats(frames)
    return {"frames": frames, "stats": stats}


def _compute_frame_stats(frames: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """计算帧耗时统计：平均/P50/P95/P99。"""
    if not frames:
        return None

    times = [f["frame_time_ms"] for f in frames if f["frame_time_ms"] > 0]
    if not times:
        return None

    sorted_times = sorted(times)
    total = len(frames)
    dropped = sum(1 for f in frames if f.get("dropped"))

    return {
        "avg_ms": round(sum(times) / len(times), 2),
        "p50_ms": round(_percentile(sorted_times, 50), 2),
        "p95_ms": round(_percentile(sorted_times, 95), 2),
        "p99_ms": round(_percentile(sorted_times, 99), 2),
        "total_frames": total,
        "dropped_frames": dropped,
    }


# ── 网络连接统计解析 ──

def parse_network_stat(xml_path: Path) -> Dict[str, Any]:
    """
    解析网络连接统计 XML。

    Returns:
        {
            "connections": [{"conn_id": str, "bytes_in": int, "bytes_out": int,
                             "latency_ms": float, "protocol": str}, ...],
            "total_bytes_in": int,
            "total_bytes_out": int,
            "connection_count": int,
        }
    """
    if not xml_path.exists():
        return {"connections": [], "total_bytes_in": 0, "total_bytes_out": 0,
                "connection_count": 0}

    connections: List[Dict[str, Any]] = []
    total_in = 0
    total_out = 0

    def _handle_row(vals: Dict[str, str]) -> None:
        nonlocal total_in, total_out

        conn_id = ""
        bytes_in = 0
        bytes_out = 0
        latency_ms = 0.0
        protocol = ""

        for key, val in vals.items():
            kl = key.lower()
            if "conn" in kl and "id" in kl:
                conn_id = val.strip()
            elif "id" in kl and not conn_id:
                conn_id = val.strip()
            elif ("byte" in kl and "in" in kl) or "rx" in kl or "recv" in kl:
                bytes_in = _safe_int(val)
            elif ("byte" in kl and "out" in kl) or "tx" in kl or "sent" in kl or "transmit" in kl:
                bytes_out = _safe_int(val)
            elif "latency" in kl or "rtt" in kl or "delay" in kl:
                latency_ms = _safe_float(val)
            elif "protocol" in kl or "proto" in kl:
                protocol = val.strip()

        total_in += bytes_in
        total_out += bytes_out

        connections.append({
            "conn_id": conn_id,
            "bytes_in": bytes_in,
            "bytes_out": bytes_out,
            "latency_ms": latency_ms,
            "protocol": protocol,
        })

    try:
        _iterparse_rows(xml_path, _handle_row)
    except Exception as exc:
        logger.warning("parse_network_stat error: %s", exc)

    return {
        "connections": connections,
        "total_bytes_in": total_in,
        "total_bytes_out": total_out,
        "connection_count": len(connections),
    }


# ── 虚拟内存分配解析 ──

def parse_vm_tracking(xml_path: Path, top_n: int = 20) -> Dict[str, Any]:
    """
    解析虚拟内存分配 XML，返回 Top-N 分配热点。

    Returns:
        {
            "regions": [{"region_type": str, "size_mb": float, "process": str}, ...],
            "top_n": [...],   # 按 size_mb 降序排列的 Top-N
            "total_size_mb": float,
            "region_count": int,
        }
    """
    if not xml_path.exists():
        return {"regions": [], "top_n": [], "total_size_mb": 0.0, "region_count": 0}

    regions: List[Dict[str, Any]] = []

    def _handle_row(vals: Dict[str, str]) -> None:
        region_type = ""
        size_mb = 0.0
        process = ""

        for key, val in vals.items():
            kl = key.lower()
            if "region" in kl and "type" in kl:
                region_type = val.strip()
            elif "type" in kl and not region_type:
                region_type = val.strip()
            elif "size" in kl or "virtual" in kl or "alloc" in kl:
                size_mb = _safe_float(val)
            elif "process" in kl or "pid" in kl or "task" in kl:
                process = val.strip()

        # 如果只有一列数值，当作 size
        if size_mb == 0.0 and len(vals) == 1:
            size_mb = _safe_float(list(vals.values())[0])

        if region_type or size_mb > 0:
            regions.append({
                "region_type": region_type,
                "size_mb": size_mb,
                "process": process,
            })

    try:
        _iterparse_rows(xml_path, _handle_row)
    except Exception as exc:
        logger.warning("parse_vm_tracking error: %s", exc)

    # 按 size_mb 降序
    regions.sort(key=lambda r: r["size_mb"], reverse=True)
    total = sum(r["size_mb"] for r in regions)

    return {
        "regions": regions,
        "top_n": regions[:top_n],
        "total_size_mb": round(total, 2),
        "region_count": len(regions),
    }


# ── Metal Performance 解析 ──

def parse_metal_performance(xml_path: Path) -> Dict[str, Any]:
    """
    解析 Metal shader 耗时 XML。

    Returns:
        {
            "shaders": [{"shader_name": str, "gpu_time_ms": float, "calls": int}, ...],
            "total_gpu_time_ms": float,
            "shader_count": int,
        }
    """
    if not xml_path.exists():
        return {"shaders": [], "total_gpu_time_ms": 0.0, "shader_count": 0}

    shaders: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}

    def _handle_row(vals: Dict[str, str]) -> None:
        shader_name = ""
        gpu_time_ms = 0.0
        calls = 1

        for key, val in vals.items():
            kl = key.lower()
            if "shader" in kl or "name" in kl or "kernel" in kl or "program" in kl:
                shader_name = val.strip()
            elif "gpu" in kl and ("time" in kl or "duration" in kl):
                gpu_time_ms = _safe_float(val)
            elif "time" in kl or "duration" in kl or "ms" in kl:
                gpu_time_ms = _safe_float(val)
            elif "call" in kl or "count" in kl or "invoc" in kl or "dispatch" in kl:
                calls = _safe_int(val, default=1)

        if not shader_name and gpu_time_ms > 0:
            # 尝试用第一个非数值字段作为 shader name
            for key, val in vals.items():
                v = val.strip()
                if v and not _is_numeric(v):
                    shader_name = v
                    break

        if not shader_name:
            shader_name = f"<unknown_{len(seen)}>"

        # 聚合同名 shader
        if shader_name in seen:
            seen[shader_name]["gpu_time_ms"] += gpu_time_ms
            seen[shader_name]["calls"] += calls
        else:
            seen[shader_name] = {
                "shader_name": shader_name,
                "gpu_time_ms": gpu_time_ms,
                "calls": calls,
            }

    try:
        _iterparse_rows(xml_path, _handle_row)
    except Exception as exc:
        logger.warning("parse_metal_performance error: %s", exc)

    # 按 gpu_time_ms 降序
    shaders = sorted(seen.values(), key=lambda s: s["gpu_time_ms"], reverse=True)
    total_time = sum(s["gpu_time_ms"] for s in shaders)

    return {
        "shaders": shaders,
        "total_gpu_time_ms": round(total_time, 2),
        "shader_count": len(shaders),
    }


def _is_numeric(s: str) -> bool:
    """快速判断字符串是否看起来像数字。"""
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False


# ── 批量导出 + 解析 ──

# schema_name → parse function 映射
_SCHEMA_PARSERS = {
    "gpu-frame-time": parse_gpu_frame_time,
    "network-connection-stat": parse_network_stat,
    "vm-tracking": parse_vm_tracking,
    "metal-performance": parse_metal_performance,
}


def deep_export_all(
    trace_file: Path,
    output_dir: Path,
    schemas: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    批量导出并解析所有指定的深度 schema。

    Args:
        trace_file: .trace 文件路径
        output_dir: XML 导出目录
        schemas:    要导出的 schema 列表（默认全部 DEEP_SCHEMAS key）

    Returns:
        {schema_name: {"raw_xml": Path, "parsed": parsed_data, "error": str|None}, ...}
    """
    if schemas is None:
        schemas = list(DEEP_SCHEMAS.keys())

    # 先探测 trace 有哪些 schema
    available = probe_trace_schemas(trace_file)
    # 构建 xpath 中包含的 schema 名称 → DEEP_SCHEMAS key 的映射
    available_set = set(available)
    # DEEP_SCHEMAS 的 xpath 中的实际 schema 名
    xpath_schema_names = {
        key: cfg["xpath"].split('schema="')[1].split('"')[0]
        for key, cfg in DEEP_SCHEMAS.items()
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    results: Dict[str, Any] = {}

    for schema_name in schemas:
        if schema_name not in DEEP_SCHEMAS:
            results[schema_name] = {
                "raw_xml": None,
                "parsed": None,
                "error": f"Unknown schema: {schema_name}",
            }
            continue

        # 检查 trace 是否有此 schema
        actual_name = xpath_schema_names.get(schema_name, schema_name)
        if available and actual_name not in available_set and schema_name not in available_set:
            results[schema_name] = {
                "raw_xml": None,
                "parsed": None,
                "error": f"Schema not found in trace: {actual_name}",
            }
            logger.info("Skipping %s: not in trace", schema_name)
            continue

        xml_path = output_dir / f"{schema_name}.xml"

        # 导出
        exported = export_deep_schema(trace_file, schema_name, xml_path)

        if not exported:
            results[schema_name] = {
                "raw_xml": None,
                "parsed": None,
                "error": "xctrace export failed or empty output",
            }
            continue

        # 解析
        parser = _SCHEMA_PARSERS.get(schema_name)
        if parser:
            try:
                parsed = parser(xml_path)
                results[schema_name] = {
                    "raw_xml": str(xml_path),
                    "parsed": parsed,
                    "error": None,
                }
            except Exception as exc:
                results[schema_name] = {
                    "raw_xml": str(xml_path),
                    "parsed": None,
                    "error": f"Parse error: {exc}",
                }
                logger.warning("Parse %s failed: %s", schema_name, exc)
        else:
            results[schema_name] = {
                "raw_xml": str(xml_path),
                "parsed": None,
                "error": f"No parser for schema: {schema_name}",
            }

    return results


# ── 格式化报告 ──

def format_deep_report(data: Dict[str, Any], schema_name: str) -> str:
    """
    将解析后的数据格式化为可读文本报告。

    Args:
        data:       parse_xxx() 返回的解析数据
        schema_name: schema key

    Returns:
        格式化的文本字符串
    """
    cfg = DEEP_SCHEMAS.get(schema_name, {})
    desc = cfg.get("desc", schema_name)

    lines: List[str] = []
    lines.append(f"{'─' * 60}")
    lines.append(f"  {schema_name} — {desc}")
    lines.append(f"{'─' * 60}")

    if schema_name == "gpu-frame-time":
        _format_gpu_frame_time(data, lines)
    elif schema_name == "network-connection-stat":
        _format_network_stat(data, lines)
    elif schema_name == "vm-tracking":
        _format_vm_tracking(data, lines)
    elif schema_name == "metal-performance":
        _format_metal_performance(data, lines)
    else:
        lines.append("  (无专用格式化器)")

    return "\n".join(lines)


def _format_gpu_frame_time(data: Dict[str, Any], lines: List[str]) -> None:
    """格式化 GPU 帧耗时报告。"""
    stats = data.get("stats")
    if not stats:
        lines.append("  (无帧耗时数据)")
        return

    lines.append(f"  总帧数:     {stats['total_frames']}")
    lines.append(f"  掉帧数:     {stats['dropped_frames']}")
    drop_pct = (
        round(stats["dropped_frames"] / stats["total_frames"] * 100, 2)
        if stats["total_frames"] > 0 else 0.0
    )
    lines.append(f"  掉帧率:     {drop_pct}%")
    lines.append("")
    lines.append(f"  平均帧耗时: {stats['avg_ms']:.2f} ms")
    lines.append(f"  P50:        {stats['p50_ms']:.2f} ms")
    lines.append(f"  P95:        {stats['p95_ms']:.2f} ms")
    lines.append(f"  P99:        {stats['p99_ms']:.2f} ms")

    # 掉帧详情（最多 10 条）
    frames = data.get("frames", [])
    dropped = [f for f in frames if f.get("dropped")]
    if dropped:
        lines.append("")
        lines.append(f"  掉帧记录 (共 {len(dropped)} 帧，显示前 10):")
        for f in dropped[:10]:
            lines.append(
                f"    PID={f.get('gpu_pid', '?'):>6d}  "
                f"耗时={f.get('frame_time_ms', 0):.2f} ms"
            )


def _format_network_stat(data: Dict[str, Any], lines: List[str]) -> None:
    """格式化网络连接统计报告。"""
    conns = data.get("connections", [])
    if not conns:
        lines.append("  (无网络连接数据)")
        return

    lines.append(f"  连接总数:   {data.get('connection_count', 0)}")
    lines.append(f"  总入流量:   {_fmt_bytes(data.get('total_bytes_in', 0))}")
    lines.append(f"  总出流量:   {_fmt_bytes(data.get('total_bytes_out', 0))}")
    lines.append("")
    lines.append("  Top 连接 (按入流量降序):")

    sorted_conns = sorted(conns, key=lambda c: c.get("bytes_in", 0), reverse=True)
    for i, c in enumerate(sorted_conns[:15]):
        latency = f"{c.get('latency_ms', 0):.1f} ms" if c.get("latency_ms", 0) > 0 else "-"
        proto = c.get("protocol", "") or "-"
        lines.append(
            f"  {i + 1:2d}. {c.get('conn_id', '?')[:40]:<40s}  "
            f"IN={_fmt_bytes(c['bytes_in']):>10s}  "
            f"OUT={_fmt_bytes(c['bytes_out']):>10s}  "
            f"RTT={latency:>10s}  {proto}"
        )


def _format_vm_tracking(data: Dict[str, Any], lines: List[str]) -> None:
    """格式化虚拟内存分配热点报告。"""
    top_n = data.get("top_n", [])
    if not top_n:
        lines.append("  (无虚拟内存数据)")
        return

    lines.append(f"  总分配:     {data.get('total_size_mb', 0):.1f} MB")
    lines.append(f"  区域数:     {data.get('region_count', 0)}")
    lines.append("")
    lines.append(f"  Top 分配热点:")

    max_type = max(len(r.get("region_type", "")) for r in top_n) if top_n else 0
    max_type = min(max_type, 40)

    for i, r in enumerate(top_n):
        rtype = r.get("region_type", "?")[:40]
        proc = r.get("process", "") or "-"
        bar_len = min(int(r["size_mb"] / max(top_n[0]["size_mb"], 1) * 30), 30)
        bar = "█" * bar_len
        lines.append(
            f"  {i + 1:2d}. {rtype:<{max_type}}  "
            f"{r['size_mb']:>10.1f} MB  {proc:<20s}  {bar}"
        )


def _format_metal_performance(data: Dict[str, Any], lines: List[str]) -> None:
    """格式化 Metal shader 耗时报告。"""
    shaders = data.get("shaders", [])
    if not shaders:
        lines.append("  (无 Metal shader 数据)")
        return

    lines.append(f"  Shader 数:  {data.get('shader_count', 0)}")
    lines.append(f"  总 GPU 耗时: {data.get('total_gpu_time_ms', 0):.2f} ms")
    lines.append("")
    lines.append("  Top shader (按 GPU 耗时降序):")

    max_name = max(len(s.get("shader_name", "")) for s in shaders[:20]) if shaders else 0
    max_name = min(max_name, 50)

    for i, s in enumerate(shaders[:20]):
        name = s.get("shader_name", "?")[:50]
        bar_len = min(
            int(s["gpu_time_ms"] / max(shaders[0]["gpu_time_ms"], 0.001) * 30), 30
        )
        bar = "█" * bar_len
        lines.append(
            f"  {i + 1:2d}. {name:<{max_name}}  "
            f"{s['gpu_time_ms']:>10.2f} ms  "
            f"calls={s.get('calls', 1):>6d}  {bar}"
        )


def _fmt_bytes(n: int) -> str:
    """格式化字节数为可读字符串。"""
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    elif n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    else:
        return f"{n / (1024 * 1024 * 1024):.1f} GB"
