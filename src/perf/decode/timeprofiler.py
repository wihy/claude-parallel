"""Time Profiler XML 解析 — xctrace export → samples → Top-N 聚合。

从 capture/sampling.py 抽出的纯解析逻辑,与采集循环解耦。

公共函数:
- export_xctrace_schema: 调 xcrun xctrace export 导出指定 schema 的 XML
- parse_timeprofiler_xml: 解析 Time Profiler XML (自动识别 Xcode 16+ / legacy)
- aggregate_top_n: 按叶子符号聚合并取 Top-N

capture/sampling.py 继续以 re-export 方式暴露这些函数,保持外部 API 兼容。
"""

import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


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
    thread_map: Dict[str, str] = {}             # thread id → "name#tid" (per-thread 聚合关键)
    process_map: Dict[str, str] = {}            # process id → name

    samples: List[Any] = []
    # 当前 row 的临时状态
    in_row = False
    row_weight: float = 1.0
    row_frames: List[Tuple[str, str]] = []   # (name, addr)
    row_ts: float = 0.0
    row_thread: str = ""                     # 本 row 的线程标签
    row_process: str = ""                    # 本 row 的进程名
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
                    row_thread = ""
                    row_process = ""
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

            elif tag == "thread":
                # xctrace XML 中线程元素: <thread id="..." name="..." tid="..."/>
                # 或引用形式: <thread ref="..."/>
                tid_id = elem.get("id")
                tid_ref = elem.get("ref")
                tname = elem.get("name", "") or elem.get("fmt", "")
                tid_attr = elem.get("tid", "") or elem.get("thread-id", "")
                if tid_id:
                    label = tname if tname else (f"tid#{tid_attr}" if tid_attr else f"thread#{tid_id}")
                    thread_map[tid_id] = label
                if in_row:
                    if tid_id:
                        row_thread = thread_map.get(tid_id, "")
                    elif tid_ref:
                        row_thread = thread_map.get(tid_ref, "")

            elif tag == "process":
                # 进程元素: <process id="..." name="..." pid="..."/>
                pid_id = elem.get("id")
                pid_ref = elem.get("ref")
                pname = elem.get("name", "") or elem.get("fmt", "")
                if pid_id:
                    process_map[pid_id] = pname
                if in_row:
                    if pid_id:
                        row_process = process_map.get(pid_id, "")
                    elif pid_ref:
                        row_process = process_map.get(pid_ref, "")

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
                                "thread": row_thread,
                                "process": row_process,
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
                            # (name, weight, addr, thread)
                            samples.append((leaf_name, row_weight, leaf_addr, row_thread))

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
      [(name, weight)]                       最古老格式
      [(name, weight, addr)]                 含地址 (LinkMap 反查)
      [(name, weight, addr, thread)]         含 thread (per-thread 聚合)
    """
    if not samples:
        return []

    # (leaf_name, addr) → weight
    # 保留 addr 让 SymbolResolver 能用 LinkMap/atos 反查
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
