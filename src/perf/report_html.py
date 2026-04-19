"""
report_html — 将 perf report 生成为自包含 HTML 报告。

特性:
- chart.js 时序图: CPU/内存/功耗趋势
- xctrace 功耗子系统堆叠图 (Display/CPU/GPU/Networking mW)
- Before/After 对比双线图
- Syslog 告警时序散点图
- WebKit 进程树可视化
- DvtBridge 进程指标可视化
- 采样热点排名
- Timeline 事件甘特图
- Gate/回归检测结果
- PDF 导出按钮 (浏览器打印)
- chartjs-plugin-zoom 交互式缩放
- 零外部依赖: chart.js + zoom plugin 通过 CDN 加载，CSS 内联
"""

import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .dvt_bridge import read_dvt_process_jsonl, read_dvt_system_jsonl
from .device_metrics import read_battery_jsonl
from .sampling import read_hotspots_jsonl


# ── xctrace XML Parsing ──


def _parse_xctrace_table(xml_path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    """
    解析 xctrace export 的 XML 表格。
    返回 (列名列表, 行字典列表)。
    xctrace 格式: <row><c>val1</c><c>val2</c></row>，列名在 <col name="..."> 中。
    """
    if not xml_path.exists():
        return [], []
    try:
        text = xml_path.read_text(errors="replace")
    except Exception:
        return [], []

    # 提取列名
    columns = [m.group(1) for m in re.finditer(r'<col[^>]*name="([^"]+)"', text)]
    if not columns:
        return [], []

    # 提取行数据
    rows = []
    row_pat = re.compile(r"<row>(.*?)</row>", re.S)
    cell_pat = re.compile(r"<c[^>]*>(.*?)</c>", re.S)
    for row_m in row_pat.finditer(text):
        cells = [c.strip() for c in cell_pat.findall(row_m.group(1))]
        row_dict = {}
        for i, col in enumerate(columns):
            if i < len(cells):
                row_dict[col] = cells[i]
        rows.append(row_dict)

    return columns, rows


def _parse_mw_value(text: str) -> Optional[float]:
    """从 '123 mW' 或纯数字中提取 mW 值。"""
    if not text:
        return None
    m = re.search(r"([\d.]+)", text)
    if m:
        try:
            return float(m.group(1))
        except (TypeError, ValueError):
            return None
    return None


def _safe_float(text: str) -> Optional[float]:
    """安全地从字符串中提取数字。"""
    if not text:
        return None
    try:
        cleaned = re.sub(r"[^\d.\-eE]", "", text)
        return float(cleaned) if cleaned else None
    except (TypeError, ValueError):
        return None


def _fmt_time(ts: float) -> str:
    """时间戳转 HH:MM:SS"""
    if not ts:
        return "?"
    try:
        return time.strftime("%H:%M:%S", time.localtime(ts))
    except Exception:
        return "?"


# ── Main Entry ──


def generate_html_report(
    report: Dict[str, Any],
    meta: Dict[str, Any],
    session_root: Path,
    output_path: Optional[Path] = None,
) -> Path:
    """
    从 perf report + meta + JSONL 数据生成自包含 HTML 报告。
    """
    if output_path is None:
        output_path = session_root / "report.html"

    logs_dir = session_root / "logs"
    exports_dir = session_root / "exports"

    chart_configs = []
    sections = []

    # 1. xctrace 功耗时序图 (P0 — 最重要)
    power_chart = _build_power_chart(exports_dir)
    if power_chart:
        chart_configs.append(power_chart)

    # 2. Before/After 对比图 (P1)
    baseline_chart = _build_baseline_chart(report, session_root)
    if baseline_chart:
        chart_configs.append(baseline_chart)

    # 3. Syslog 告警时序散点图 (P2)
    live = report.get("live_analysis", {})
    alerts = live.get("alerts", [])
    if alerts:
        alert_chart = _build_alert_timeline_chart(alerts)
        if alert_chart:
            chart_configs.append(alert_chart)

    # 4. DvtBridge 进程指标时序图
    dvt_metrics = report.get("dvt_metrics", {})
    dm = meta.get("device_metrics", {})
    if dm.get("source") == "dvt_bridge":
        proc_jsonl = Path(dm.get("process_jsonl", ""))
        if proc_jsonl.exists():
            dvt_proc_data = read_dvt_process_jsonl(proc_jsonl)
            if dvt_proc_data:
                chart_configs.append(_build_dvt_process_chart(dvt_proc_data))

        sys_jsonl = Path(dm.get("system_jsonl", ""))
        if sys_jsonl.exists():
            dvt_sys_data = read_dvt_system_jsonl(sys_jsonl)
            if dvt_sys_data:
                chart_configs.append(_build_dvt_system_chart(dvt_sys_data))

    # 5. 电池趋势图
    batt = meta.get("battery", {})
    if batt.get("jsonl"):
        batt_path = Path(batt["jsonl"])
        if batt_path.exists():
            batt_data = read_battery_jsonl(batt_path)
            if batt_data:
                chart_configs.append(_build_battery_chart(batt_data))

    # 6. 采样热点数据
    hotspots = []
    sampling_meta = meta.get("sampling", {})
    if sampling_meta.get("hotspots_file"):
        hs_path = Path(sampling_meta["hotspots_file"])
        if hs_path.exists():
            hotspots = read_hotspots_jsonl(hs_path)

    # ── 构建 Sections ──

    # Header
    tag = report.get("tag", meta.get("tag", "unknown"))
    status = report.get("status", meta.get("status", "unknown"))
    started = meta.get("started_at", 0)
    ended = meta.get("ended_at", 0)
    duration = round(ended - started, 1) if started and ended else 0
    started_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)) if started else "N/A"
    ended_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ended)) if ended else "N/A"

    sections.append(_section_header(tag, status, started_str, ended_str, duration, meta))

    # Gate
    gate = report.get("gate", {})
    if gate.get("checked"):
        sections.append(_section_gate(gate))

    # Power Summary (数字摘要)
    metrics = report.get("metrics", {})
    if metrics.get("display_avg") is not None:
        sections.append(_section_power_summary(metrics))

    # Charts
    if chart_configs:
        sections.append(_section_charts(chart_configs))

    # DvtBridge 进程指标表
    if dvt_metrics.get("process_stats"):
        sections.append(_section_dvt_process_table(dvt_metrics))

    # 采样热点
    if hotspots:
        sections.append(_section_hotspots(hotspots))

    # WebKit 进程树 + 调用栈 (P2)
    sections.append(_section_webkit_tree(report, session_root))

    # Timeline
    timeline = report.get("timeline", {})
    if timeline.get("events"):
        sections.append(_section_timeline(timeline))

    # Syslog
    syslog = report.get("syslog", {})
    if syslog.get("lines", 0) > 0:
        sections.append(_section_syslog(syslog))

    # Live Analysis
    if live.get("status") == "completed":
        sections.append(_section_live_analysis(live))

    # Baseline
    baseline = report.get("baseline", {})
    if baseline.get("metrics"):
        sections.append(_section_baseline(baseline))

    # 生成完整 HTML
    html = _render_full_page(tag, sections, chart_configs)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")

    return output_path


# ── Chart 构建器 ──


def _build_power_chart(exports_dir: Path) -> Optional[Dict]:
    """P0: xctrace 功耗子系统堆叠图 — Display/CPU/GPU/Networking mW 时序。"""
    power_xml = exports_dir / "SystemPowerLevel.xml"
    if not power_xml.exists():
        return None

    columns, rows = _parse_xctrace_table(power_xml)
    if not rows:
        return None

    # 提取各子系统的 mW 时序
    labels = []
    display_vals = []
    cpu_vals = []
    gpu_vals = []
    net_vals = []
    total_vals = []

    for row in rows:
        # 时间列可能是 sample-time 或 start-time 等
        time_val = None
        for col in columns:
            if "time" in col.lower():
                time_val = _safe_float(row.get(col, ""))
                break
        labels.append(time_val or len(labels))

        d = _parse_mw_value(row.get("Display", ""))
        c = _parse_mw_value(row.get("CPU", ""))
        g = _parse_mw_value(row.get("GPU", ""))
        n = _parse_mw_value(row.get("Networking", ""))

        display_vals.append(d)
        cpu_vals.append(c)
        gpu_vals.append(g)
        net_vals.append(n)
        total_vals.append(sum(v for v in [d, c, g, n] if v is not None) if any(v is not None for v in [d, c, g, n]) else None)

    # 时间标签格式化
    time_labels = [f"{i}" for i in range(len(labels))]

    # 检查 thermal state 数据
    thermal_labels = []
    thermal_levels = []
    thermal_xml = exports_dir / "device-thermal-state-intervals.xml"
    if thermal_xml.exists():
        _, thermal_rows = _parse_xctrace_table(thermal_xml)
        for tr in thermal_rows:
            state = tr.get("thermal-state", "")
            thermal_labels.append(state[:10])
            level_map = {"Nominal": 0, "Fair": 1, "Serious": 2, "Critical": 3}
            thermal_levels.append(level_map.get(state, 0))

    datasets = [
        {
            "label": "Display",
            "data": display_vals,
            "backgroundColor": "#3b82f680",
            "borderColor": "#3b82f6",
            "fill": True,
            "tension": 0.3,
            "pointRadius": 0,
            "order": 1,
        },
        {
            "label": "CPU",
            "data": cpu_vals,
            "backgroundColor": "#ef444480",
            "borderColor": "#ef4444",
            "fill": True,
            "tension": 0.3,
            "pointRadius": 0,
            "order": 2,
        },
        {
            "label": "GPU",
            "data": gpu_vals,
            "backgroundColor": "#f59e0b80",
            "borderColor": "#f59e0b",
            "fill": True,
            "tension": 0.3,
            "pointRadius": 0,
            "order": 3,
        },
        {
            "label": "Networking",
            "data": net_vals,
            "backgroundColor": "#10b98180",
            "borderColor": "#10b981",
            "fill": True,
            "tension": 0.3,
            "pointRadius": 0,
            "order": 4,
        },
        {
            "label": "Total",
            "data": total_vals,
            "borderColor": "#e2e8f0",
            "backgroundColor": "transparent",
            "fill": False,
            "tension": 0.3,
            "pointRadius": 0,
            "borderWidth": 2,
            "borderDash": [5, 5],
            "order": 0,
        },
    ]

    # 如果有 thermal 数据，加一条线
    if thermal_levels:
        # 将 thermal 归一化到功耗范围内
        max_power = max((v for v in total_vals if v is not None), default=1000)
        scaled_thermal = [l / 3.0 * max_power * 0.1 for l in thermal_levels]
        datasets.append({
            "label": "Thermal State",
            "data": scaled_thermal,
            "type": "line",
            "borderColor": "#ff00ff",
            "backgroundColor": "transparent",
            "fill": False,
            "pointRadius": 2,
            "borderDash": [2, 2],
            "yAxisID": "y1",
            "order": 0,
        })

    charts = [
        {
            "subtitle": "功耗子系统 (mW) — 堆叠面积图",
            "type": "line",
            "labels": time_labels,
            "datasets": datasets,
            "yLabel": "mW",
            "stacked": True,
            "hasThermal": bool(thermal_levels),
        },
    ]

    return {
        "id": "xctrace_power",
        "title": "xctrace 功耗时序图",
        "charts": charts,
    }


def _build_baseline_chart(report: Dict, session_root: Path) -> Optional[Dict]:
    """P1: Before/After 对比双线图。尝试读两个 session 的 SystemPowerLevel.xml。"""
    baseline = report.get("baseline", {})
    if not baseline.get("metrics"):
        return None

    current_exports = session_root / "exports"
    current_xml = current_exports / "SystemPowerLevel.xml"
    if not current_xml.exists():
        return None

    # 当前数据
    _, cur_rows = _parse_xctrace_table(current_xml)
    if not cur_rows:
        return None

    cur_total = []
    for row in cur_rows:
        d = _parse_mw_value(row.get("Display", ""))
        c = _parse_mw_value(row.get("CPU", ""))
        g = _parse_mw_value(row.get("GPU", ""))
        n = _parse_mw_value(row.get("Networking", ""))
        cur_total.append(sum(v for v in [d, c, g, n] if v is not None) if any(v is not None for v in [d, c, g, n]) else 0)

    # 尝试读 baseline 数据
    baseline_tag = baseline.get("tag", "")
    baseline_exports = session_root.parent / baseline_tag / "exports"
    baseline_xml = baseline_exports / "SystemPowerLevel.xml"

    base_total = []
    if baseline_xml.exists():
        _, base_rows = _parse_xctrace_table(baseline_xml)
        for row in base_rows:
            d = _parse_mw_value(row.get("Display", ""))
            c = _parse_mw_value(row.get("CPU", ""))
            g = _parse_mw_value(row.get("GPU", ""))
            n = _parse_mw_value(row.get("Networking", ""))
            base_total.append(sum(v for v in [d, c, g, n] if v is not None) if any(v is not None for v in [d, c, g, n]) else 0)

    labels = [str(i) for i in range(max(len(cur_total), len(base_total)))]

    if base_total:
        # 双线对比模式
        datasets = [
            {
                "label": f"Before ({baseline_tag})",
                "data": base_total,
                "borderColor": "#94a3b8",
                "backgroundColor": "#94a3b830",
                "fill": False,
                "tension": 0.3,
                "pointRadius": 1,
                "borderDash": [5, 5],
            },
            {
                "label": "After",
                "data": cur_total,
                "borderColor": "#3b82f6",
                "backgroundColor": "#3b82f630",
                "fill": True,
                "tension": 0.3,
                "pointRadius": 1,
            },
        ]
    else:
        # Fallback: 柱状对比 (用 metrics 数值)
        bm = baseline.get("metrics", {})
        cm = report.get("metrics", {})
        labels_bar = ["Display", "CPU", "Networking"]
        before_vals = [bm.get("display_avg"), bm.get("cpu_avg"), bm.get("networking_avg")]
        after_vals = [cm.get("display_avg"), cm.get("cpu_avg"), cm.get("networking_avg")]

        datasets = [
            {
                "label": f"Before ({baseline_tag})",
                "data": before_vals,
                "borderColor": "#94a3b8",
                "backgroundColor": "#94a3b860",
            },
            {
                "label": "After",
                "data": after_vals,
                "borderColor": "#3b82f6",
                "backgroundColor": "#3b82f660",
            },
        ]
        return {
            "id": "baseline_compare",
            "title": "Before/After 功耗对比",
            "charts": [
                {
                    "subtitle": "平均功耗对比 (mW)",
                    "type": "bar",
                    "labels": labels_bar,
                    "datasets": datasets,
                    "yLabel": "mW (avg)",
                },
            ],
        }

    return {
        "id": "baseline_compare",
        "title": "Before/After 功耗时序对比",
        "charts": [
            {
                "subtitle": "总功耗对比 (mW)",
                "type": "line",
                "labels": labels,
                "datasets": datasets,
                "yLabel": "mW",
            },
        ],
    }


def _build_alert_timeline_chart(alerts: List[Dict]) -> Optional[Dict]:
    """P2: Syslog 告警时序散点图 — X时间 Y级别，彩色圆点。"""
    if not alerts:
        return None

    # 提取时间和级别
    scatter_data = []
    level_map = {"critical": 3, "error": 2.5, "warn": 2, "warning": 2, "info": 1}
    color_map = {"critical": "#ef4444", "error": "#ef4444", "warn": "#f59e0b", "warning": "#f59e0b", "info": "#3b82f6"}

    for a in alerts:
        ts = a.get("ts", 0)
        level = a.get("level", "info").lower()
        y_val = level_map.get(level, 1)
        color = color_map.get(level, "#64748b")
        msg = a.get("message", a.get("rule", ""))
        rule = a.get("rule", "")
        scatter_data.append({
            "x": ts,
            "y": y_val,
            "msg": msg[:80],
            "rule": rule,
            "color": color,
            "level": level,
        })

    # 按 time 分组为 datasets（按颜色）
    by_color = {}  # type: Dict[str, List[Dict]]
    for d in scatter_data:
        by_color.setdefault(d["color"], []).append(d)

    datasets = []
    for color, items in by_color.items():
        level_name = items[0]["level"]
        datasets.append({
            "label": level_name,
            "data": [{"x": i["x"], "y": i["y"]} for i in items],
            "backgroundColor": color,
            "pointRadius": 5,
            "pointHoverRadius": 8,
            "msgs": [i["msg"] for i in items],  # for tooltip
        })

    return {
        "id": "alert_timeline",
        "title": "Syslog 告警时序分布",
        "charts": [
            {
                "subtitle": "告警时间线 (hover 查看详情)",
                "type": "scatter",
                "labels": [],
                "datasets": datasets,
                "yLabel": "",
                "yLabels": {1: "Info", 2: "Warn", 3: "Critical"},
                "isScatter": True,
            },
        ],
    }


def _build_dvt_process_chart(records: List[Dict]) -> Dict:
    """DvtBridge 进程 CPU/内存时序图。"""
    by_name = {}  # type: Dict[str, List[Dict]]
    for r in records:
        name = r.get("name", "unknown")
        by_name.setdefault(name, []).append(r)

    datasets_cpu = []
    datasets_mem = []

    colors = [
        "#3b82f6", "#ef4444", "#10b981", "#f59e0b", "#8b5cf6",
        "#ec4899", "#06b6d4", "#f97316", "#6366f1", "#84cc16",
    ]

    for i, (name, recs) in enumerate(by_name.items()):
        color = colors[i % len(colors)]
        labels = [_fmt_time(r.get("ts", 0)) for r in recs]
        cpu_vals = [r.get("cpuUsage") for r in recs]
        mem_vals = [r.get("physFootprintMB") for r in recs]

        datasets_cpu.append({
            "label": name,
            "data": cpu_vals,
            "borderColor": color,
            "backgroundColor": color + "20",
            "fill": False,
            "tension": 0.3,
            "pointRadius": 1,
        })
        datasets_mem.append({
            "label": name,
            "data": mem_vals,
            "borderColor": color,
            "backgroundColor": color + "20",
            "fill": False,
            "tension": 0.3,
            "pointRadius": 1,
        })

    all_labels = [_fmt_time(records[0].get("ts", 0))] if records else []

    return {
        "id": "dvt_process",
        "title": "DvtBridge 进程指标趋势",
        "charts": [
            {
                "subtitle": "CPU 使用率 (%)",
                "type": "line",
                "labels": all_labels if len(by_name) == 1 else labels,
                "datasets": datasets_cpu,
                "yLabel": "CPU %",
            },
            {
                "subtitle": "物理内存 (MB)",
                "type": "line",
                "labels": all_labels if len(by_name) == 1 else labels,
                "datasets": datasets_mem,
                "yLabel": "MB",
            },
        ],
    }


def _build_dvt_system_chart(records: List[Dict]) -> Dict:
    """系统级指标时序图。"""
    labels = [_fmt_time(r.get("ts", 0)) for r in records]
    cpu_vals = [r.get("cpuTotal") for r in records]
    mem_free = [r.get("physMemoryFreeMB") for r in records]
    mem_used = [r.get("physMemoryUsedMB") for r in records]

    return {
        "id": "dvt_system",
        "title": "系统级指标趋势",
        "charts": [
            {
                "subtitle": "系统 CPU 总使用率 (%)",
                "type": "line",
                "labels": labels,
                "datasets": [{
                    "label": "CPU Total",
                    "data": cpu_vals,
                    "borderColor": "#3b82f6",
                    "backgroundColor": "#3b82f620",
                    "fill": True,
                    "tension": 0.3,
                    "pointRadius": 1,
                }],
                "yLabel": "CPU %",
            },
            {
                "subtitle": "内存使用 (MB)",
                "type": "line",
                "labels": labels,
                "datasets": [
                    {
                        "label": "Used",
                        "data": mem_used,
                        "borderColor": "#ef4444",
                        "backgroundColor": "#ef444420",
                        "fill": True,
                        "tension": 0.3,
                        "pointRadius": 1,
                    },
                    {
                        "label": "Free",
                        "data": mem_free,
                        "borderColor": "#10b981",
                        "backgroundColor": "#10b98120",
                        "fill": True,
                        "tension": 0.3,
                        "pointRadius": 1,
                    },
                ],
                "yLabel": "MB",
            },
        ],
    }


def _build_battery_chart(records: List[Dict]) -> Dict:
    """电池趋势图。"""
    labels = [_fmt_time(r.get("ts", 0)) for r in records]
    levels = [r.get("level_pct") for r in records]

    return {
        "id": "battery",
        "title": "电池电量趋势",
        "charts": [
            {
                "subtitle": "电池电量 (%)",
                "type": "line",
                "labels": labels,
                "datasets": [{
                    "label": "Battery Level",
                    "data": levels,
                    "borderColor": "#10b981",
                    "backgroundColor": "#10b98120",
                    "fill": True,
                    "tension": 0.3,
                    "pointRadius": 1,
                }],
                "yLabel": "%",
            },
        ],
    }


# ── Section 构建器 ──


def _section_header(tag, status, started, ended, duration, meta):
    device = meta.get("device", "N/A")
    attach = meta.get("attach", "N/A")
    templates = meta.get("templates", "N/A")
    status_class = "status-pass" if status == "stopped" else "status-running"

    return f"""
    <div class="card">
        <h2>Perf Report: {tag}</h2>
        <div class="meta-grid">
            <div><span class="label">Status</span> <span class="{status_class}">{status}</span></div>
            <div><span class="label">Device</span> {device}</div>
            <div><span class="label">Attach</span> {attach}</div>
            <div><span class="label">Templates</span> {templates}</div>
            <div><span class="label">Started</span> {started}</div>
            <div><span class="label">Ended</span> {ended}</div>
            <div><span class="label">Duration</span> {duration}s</div>
        </div>
        <button onclick="window.print()" class="btn-pdf">导出 PDF</button>
    </div>"""


def _section_gate(gate):
    passed = gate.get("passed", False)
    reason = gate.get("reason", "")
    cls = "gate-pass" if passed else "gate-fail"
    icon = "PASS" if passed else "FAIL"

    return f"""
    <div class="card {cls}">
        <h3>Regression Gate: {icon}</h3>
        <p>{reason}</p>
    </div>"""


def _section_power_summary(metrics):
    """P0: 功耗数字摘要卡片 — 一目了然。"""
    display_avg = metrics.get("display_avg")
    cpu_avg = metrics.get("cpu_avg")
    gpu_avg = metrics.get("gpu_avg")
    networking_avg = metrics.get("networking_avg")

    def fmt(v):
        return f"{v:.0f} mW" if isinstance(v, (int, float)) else "N/A"

    rows = ""
    items = [
        ("Display", display_avg, "#3b82f6"),
        ("CPU", cpu_avg, "#ef4444"),
        ("GPU", gpu_avg, "#f59e0b"),
        ("Networking", networking_avg, "#10b981"),
    ]
    for name, val, color in items:
        pct = ""
        if isinstance(val, (int, float)):
            bar_w = min(val / 1500 * 100, 100)
            pct = f'<div class="power-bar" style="width:{bar_w:.0f}%;background:{color}"></div>'
        rows += f'<div class="power-item"><span class="label">{name}</span> {fmt(val)} {pct}</div>'

    return f"""
    <div class="card">
        <h3>功耗摘要 (xctrace Power Profiler)</h3>
        <div class="power-grid">{rows}</div>
    </div>"""


def _section_charts(chart_configs):
    nav_items = []
    chart_divs = []

    for cfg in chart_configs:
        cid = cfg["id"]
        nav_items.append(f'<a href="#{cid}" class="chart-nav-item">{cfg["title"]}</a>')
        sub_charts = []
        for j, ch in enumerate(cfg.get("charts", [])):
            sub_id = f"{cid}_chart_{j}"
            chart_type = ch.get("type", "line")
            extra_attrs = f'data-chart-type="{chart_type}"'
            if ch.get("isScatter"):
                extra_attrs += ' data-scatter="true"'
            sub_charts.append(f"""
            <div class="chart-sub">
                <h4>{ch["subtitle"]}</h4>
                <canvas id="{sub_id}" {extra_attrs}></canvas>
            </div>""")
        chart_divs.append(f"""
        <div id="{cid}" class="card">
            <h3>{cfg["title"]}</h3>
            <div class="chart-grid">
                {"".join(sub_charts)}
            </div>
        </div>""")

    return f"""
    <div class="card">
        <h3>Charts</h3>
        <nav class="chart-nav">{''.join(nav_items)}</nav>
    </div>
    {''.join(chart_divs)}"""


def _section_dvt_process_table(dvt_metrics):
    proc_stats = dvt_metrics.get("process_stats", {})
    rows = ""
    for name, stats in sorted(proc_stats.items(), key=lambda x: -(x[1].get("cpu_pct", {}).get("avg") or 0)):
        cpu = stats.get("cpu_pct", {})
        mem = stats.get("mem_mb", {})
        cpu_avg = cpu.get("avg")
        cpu_peak = cpu.get("peak")
        mem_avg = mem.get("avg")
        mem_peak = mem.get("peak")
        cpu_avg_s = f"{cpu_avg:.1f}" if isinstance(cpu_avg, (int, float)) else "N/A"
        cpu_peak_s = f"{cpu_peak:.1f}" if isinstance(cpu_peak, (int, float)) else "N/A"
        mem_avg_s = f"{mem_avg:.0f}" if isinstance(mem_avg, (int, float)) else "N/A"
        mem_peak_s = f"{mem_peak:.0f}" if isinstance(mem_peak, (int, float)) else "N/A"
        rows += f"""
            <tr>
                <td>{name}</td>
                <td>{stats.get('pid', '?')}</td>
                <td>{stats.get('samples', 0)}</td>
                <td>{cpu_avg_s}</td>
                <td>{cpu_peak_s}</td>
                <td>{mem_avg_s}</td>
                <td>{mem_peak_s}</td>
            </tr>"""

    return f"""
    <div class="card">
        <h3>DvtBridge 进程指标统计</h3>
        <table>
            <thead><tr><th>Process</th><th>PID</th><th>Samples</th><th>CPU Avg%</th><th>CPU Peak%</th><th>MEM Avg MB</th><th>MEM Peak MB</th></tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>"""


def _section_hotspots(hotspots):
    agg = hotspots[-1] if hotspots else {}
    top = agg.get("top", [])
    if not top:
        return ""

    rows = ""
    for i, h in enumerate(top[:20]):
        sym = h.get("symbol", "?")
        weight = h.get("weight_pct", 0)
        bar_width = min(weight * 3, 100)
        rows += f"""
            <tr>
                <td>{i + 1}</td>
                <td class="symbol">{sym[:100]}</td>
                <td>{weight:.2f}%</td>
                <td><div class="bar" style="width: {bar_width}%"></div></td>
            </tr>"""

    return f"""
    <div class="card">
        <h3>Sampling Hotspots (Top 20)</h3>
        <table>
            <thead><tr><th>#</th><th>Symbol</th><th>Weight</th><th></th></tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>"""


def _section_timeline(timeline):
    levels = timeline.get("levels", [])
    if not levels:
        return ""

    rows = ""
    for lvl in levels:
        dur = lvl.get("duration_sec")
        dur_s = f"{dur:.1f}s" if dur else "N/A"
        tasks = ", ".join(lvl.get("tasks", []))
        rows += f"""
            <tr>
                <td>Level {lvl['level_idx']}</td>
                <td>{dur_s}</td>
                <td>{tasks}</td>
            </tr>"""

    return f"""
    <div class="card">
        <h3>Timeline ({timeline.get('events', 0)} events)</h3>
        <table>
            <thead><tr><th>Level</th><th>Duration</th><th>Tasks</th></tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>"""


def _section_syslog(syslog):
    return f"""
    <div class="card">
        <h3>Syslog</h3>
        <div class="meta-grid">
            <div><span class="label">Lines</span> {syslog.get('lines', 0)}</div>
            <div><span class="label">Reliable</span> {syslog.get('reliable', False)}</div>
            <div><span class="label">Source</span> {syslog.get('source', 'N/A')}</div>
        </div>
    </div>"""


def _section_live_analysis(live):
    counts = live.get("alert_counts", {})
    count_rows = ""
    for rule, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        count_rows += f'<div class="alert-item"><span class="label">{rule}</span> {cnt}</div>'

    return f"""
    <div class="card">
        <h3>Live Analysis</h3>
        <div class="meta-grid">
            <div><span class="label">Lines Processed</span> {live.get('lines_processed', 0)}</div>
            <div><span class="label">Total Alerts</span> {live.get('total_alerts', 0)}</div>
            <div><span class="label">Duration</span> {live.get('duration_sec', 0)}s</div>
        </div>
        {f'<div class="alert-counts">{count_rows}</div>' if count_rows else ''}
    </div>"""


def _section_baseline(baseline):
    metrics = baseline.get("metrics", {})
    delta = baseline.get("delta", {})

    delta_rows = ""
    for key, val in delta.items():
        if val is not None:
            color = "#ef4444" if val > 0 else "#10b981"
            delta_rows += f'<div><span class="label">{key}</span> <span style="color: {color}">{val:+.1f}%</span></div>'

    return f"""
    <div class="card">
        <h3>Baseline Comparison ({baseline.get('tag', 'N/A')})</h3>
        <div class="meta-grid">
            {delta_rows}
        </div>
    </div>"""


def _section_webkit_tree(report: Dict, session_root: Path):
    """P2: WebKit 进程树 + 调用栈热点可视化。"""
    dvt_metrics = report.get("dvt_metrics", {})
    proc_stats = dvt_metrics.get("process_stats", {})
    callstack = report.get("callstack", {})

    # 收集 WebKit 相关进程
    webkit_procs = {}
    for name, stats in proc_stats.items():
        if any(kw in name.lower() for kw in ["webkit", "webcontent", "gpu", "networking", "soul"]):
            webkit_procs[name] = stats

    if not webkit_procs and not callstack:
        return ""

    # 进程树 HTML
    tree_html = ""

    if webkit_procs:
        # 主进程
        main_procs = {k: v for k, v in webkit_procs.items() if "soul" in k.lower()}
        child_procs = {k: v for k, v in webkit_procs.items() if "soul" not in k.lower()}

        tree_items = ""
        for name, stats in main_procs.items():
            cpu = stats.get("cpu_pct", {})
            mem = stats.get("mem_mb", {})
            cpu_avg = cpu.get("avg", 0) or 0
            mem_avg = mem.get("avg", 0) or 0
            tree_items += f"""
                <div class="tree-node tree-main">
                    <span class="tree-icon">📱</span>
                    <span class="tree-name">{name}</span>
                    <span class="tree-badge">{cpu_avg:.1f}% CPU</span>
                    <span class="tree-badge">{mem_avg:.0f}MB</span>
                </div>"""

        for name, stats in child_procs.items():
            cpu = stats.get("cpu_pct", {})
            mem = stats.get("mem_mb", {})
            cpu_avg = cpu.get("avg", 0) or 0
            mem_avg = mem.get("avg", 0) or 0
            icon = "🌐" if "webcontent" in name.lower() else ("🎮" if "gpu" in name.lower() else ("📡" if "network" in name.lower() else "⚙️"))
            tree_items += f"""
                <div class="tree-node tree-child">
                    <span class="tree-line">├─</span>
                    <span class="tree-icon">{icon}</span>
                    <span class="tree-name">{name}</span>
                    <span class="tree-badge">{cpu_avg:.1f}% CPU</span>
                    <span class="tree-badge">{mem_avg:.0f}MB</span>
                </div>"""

        tree_html = f'<div class="tree-container">{tree_items}</div>'

    # 调用栈热点（按进程分组）
    hotspots_html = ""
    if callstack:
        top_frames = callstack.get("top_frames", [])
        if top_frames:
            rows = ""
            for i, f in enumerate(top_frames[:15]):
                sym = f.get("symbol", f.get("name", "?"))
                weight = f.get("weight", f.get("weight_pct", 0))
                proc = f.get("process", "")
                bar_w = min(weight * 3, 100)
                rows += f"""
                    <tr>
                        <td>{i + 1}</td>
                        <td class="symbol">{sym[:80]}</td>
                        <td>{proc[:30]}</td>
                        <td>{weight:.2f}%</td>
                        <td><div class="bar" style="width: {bar_w}%"></div></td>
                    </tr>"""
            hotspots_html = f"""
            <h4>调用栈热点 (Top 15)</h4>
            <table>
                <thead><tr><th>#</th><th>Symbol</th><th>Process</th><th>Weight</th><th></th></tr></thead>
                <tbody>{rows}</tbody>
            </table>"""

    if not tree_html and not hotspots_html:
        return ""

    return f"""
    <div class="card">
        <h3>WebKit 进程分析</h3>
        {tree_html}
        {hotspots_html}
    </div>"""


# ── Full Page Renderer ──


def _render_full_page(tag, sections, chart_configs):
    """渲染完整 HTML 页面 — 含 zoom 插件 + PDF 按钮 + print 样式。"""

    # 构建 chart.js 初始化脚本
    chart_scripts = ""
    for cfg in chart_configs:
        for j, ch in enumerate(cfg.get("charts", [])):
            sub_id = f"{cfg['id']}_chart_{j}"
            labels_json = json.dumps(ch.get("labels", []))
            datasets_json = json.dumps(ch.get("datasets", []))
            y_label = ch.get("yLabel", "")
            chart_type = ch.get("type", "line")
            stacked = ch.get("stacked", False)
            is_scatter = ch.get("isScatter", False)
            y_labels = ch.get("yLabels", {})
            has_thermal = ch.get("hasThermal", False)

            if is_scatter:
                # Scatter chart: 自定义 tooltip 显示 msg
                chart_scripts += f"""
        (function() {{
            var ctx = document.getElementById('{sub_id}');
            if (!ctx) return;
            var datasets = {datasets_json};
            // 添加 tooltip 回调
            datasets.forEach(function(ds) {{
                ds.pointHitRadius = 10;
            }});
            new Chart(ctx, {{
                type: 'scatter',
                data: {{ datasets: datasets }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {{
                        legend: {{ position: 'bottom' }},
                        tooltip: {{
                            callbacks: {{
                                label: function(ctx) {{
                                    var ds = ctx.dataset;
                                    var idx = ctx.dataIndex;
                                    var msgs = ds.msgs || [];
                                    return ds.label + ': ' + (msgs[idx] || '');
                                }}
                            }}
                        }}
                    }},
                    scales: {{
                        x: {{
                            type: 'linear',
                            title: {{ display: true, text: 'Timestamp' }},
                            ticks: {{
                                callback: function(val) {{
                                    var d = new Date(val * 1000);
                                    return d.getHours() + ':' + String(d.getMinutes()).padStart(2,'0');
                                }}
                            }}
                        }},
                        y: {{
                            min: 0.5,
                            max: 3.5,
                            ticks: {{
                                callback: function(val) {{
                                    var map = {json.dumps(y_labels)};
                                    return map[val] || val;
                                }},
                                stepSize: 1
                            }}
                        }}
                    }},
                    plugins: [{{
                        id: 'zoom',
                        afterInit: function(chart) {{ /* zoom registered globally */ }}
                    }}]
                }}
            }});
        }})();"""

            else:
                # Standard line/bar chart
                stacked_opts = ""
                if stacked:
                    stacked_opts = """
                    x: { stacked: true },
                    y: { stacked: true, title: { display: true, text: '""" + y_label + """' } }"""

                thermal_opts = ""
                if has_thermal:
                    thermal_opts = """,
                    y1: {
                        position: 'right',
                        title: { display: true, text: 'Thermal' },
                        grid: { drawOnChartArea: false },
                        min: 0,
                        max: 1,
                        ticks: {
                            callback: function(val) {
                                var map = ['Nominal','Fair','Serious','Critical'];
                                return map[Math.round(val * 3)] || '';
                            }
                        }
                    }"""

                y_opts = f"title: {{ display: true, text: '{y_label}' }}" if not stacked else ""

                scales = stacked_opts if stacked else f"x: {{}}, y: {{ {y_opts} }}{thermal_opts}"

                chart_scripts += f"""
        new Chart(document.getElementById('{sub_id}'), {{
            type: '{chart_type}',
            data: {{
                labels: {labels_json},
                datasets: {datasets_json}
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                interaction: {{ mode: 'index', intersect: false }},
                plugins: {{ legend: {{ position: 'bottom' }} }},
                scales: {{
                    {scales}
                }}
            }}
        }});"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>cpar Perf Report: {tag}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/hammerjs@2"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0f172a;
    color: #e2e8f0;
    padding: 24px;
    max-width: 1200px;
    margin: 0 auto;
  }}
  h1 {{ font-size: 1.5rem; margin-bottom: 20px; color: #f8fafc; }}
  h2 {{ font-size: 1.2rem; color: #94a3b8; margin-bottom: 12px; }}
  h3 {{ font-size: 1rem; color: #cbd5e1; margin-bottom: 10px; }}
  h4 {{ font-size: 0.9rem; color: #94a3b8; margin-bottom: 8px; }}
  .card {{
    background: #1e293b;
    border-radius: 8px;
    padding: 16px 20px;
    margin-bottom: 16px;
    border: 1px solid #334155;
  }}
  .meta-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 8px 16px;
  }}
  .label {{ color: #64748b; font-size: 0.85rem; margin-right: 6px; }}
  .status-pass {{ color: #10b981; font-weight: bold; }}
  .status-running {{ color: #f59e0b; font-weight: bold; }}
  .gate-pass {{ border-color: #10b981; }}
  .gate-fail {{ border-color: #ef4444; }}
  table {{
    width: 100%;
    border-collapse: collapse;
    margin-top: 8px;
  }}
  th, td {{
    text-align: left;
    padding: 6px 10px;
    border-bottom: 1px solid #334155;
    font-size: 0.85rem;
  }}
  th {{ color: #94a3b8; font-weight: 600; }}
  td.symbol {{ font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.8rem; }}
  .bar {{
    height: 16px;
    background: linear-gradient(90deg, #3b82f6, #8b5cf6);
    border-radius: 3px;
    min-width: 2px;
  }}
  .chart-nav {{
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
  }}
  .chart-nav-item {{
    color: #3b82f6;
    text-decoration: none;
    font-size: 0.85rem;
  }}
  .chart-nav-item:hover {{ text-decoration: underline; }}
  .chart-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
    gap: 16px;
  }}
  .chart-sub {{ position: relative; height: 250px; }}
  .chart-sub canvas {{ width: 100% !important; height: 100% !important; }}
  .alert-counts {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px 16px;
    margin-top: 10px;
  }}
  .alert-item {{ font-size: 0.85rem; }}

  /* P0: Power summary */
  .power-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
    gap: 8px 16px;
  }}
  .power-item {{
    padding: 6px 0;
  }}
  .power-bar {{
    height: 6px;
    border-radius: 3px;
    margin-top: 2px;
    min-width: 2px;
  }}

  /* P2: WebKit tree */
  .tree-container {{
    margin: 8px 0;
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 0.85rem;
  }}
  .tree-node {{
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 3px 0;
  }}
  .tree-main {{ font-weight: 600; }}
  .tree-child {{ padding-left: 16px; color: #cbd5e1; }}
  .tree-line {{ color: #64748b; }}
  .tree-icon {{ font-size: 1rem; }}
  .tree-name {{ color: #e2e8f0; min-width: 200px; }}
  .tree-badge {{
    background: #334155;
    color: #94a3b8;
    padding: 1px 8px;
    border-radius: 10px;
    font-size: 0.75rem;
    font-family: -apple-system, sans-serif;
  }}

  /* P3: PDF export button */
  .btn-pdf {{
    margin-top: 12px;
    padding: 6px 16px;
    background: #3b82f6;
    color: white;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.85rem;
    float: right;
  }}
  .btn-pdf:hover {{ background: #2563eb; }}

  /* P3: Print styles */
  @media print {{
    body {{ background: white; color: black; padding: 0; }}
    .card {{ background: white; border: 1px solid #ccc; break-inside: avoid; }}
    .btn-pdf {{ display: none; }}
    .chart-nav {{ display: none; }}
    h1, h2, h3 {{ color: black; }}
    th {{ color: #333; }}
    .tree-badge {{ background: #eee; color: #333; }}
    .power-bar {{ print-color-adjust: exact; -webkit-print-color-adjust: exact; }}
    .bar {{ print-color-adjust: exact; -webkit-print-color-adjust: exact; }}
    canvas {{ max-height: 300px; }}
  }}

  @media (max-width: 600px) {{
    .chart-grid {{ grid-template-columns: 1fr; }}
    .meta-grid {{ grid-template-columns: 1fr 1fr; }}
    .power-grid {{ grid-template-columns: 1fr 1fr; }}
  }}
</style>
</head>
<body>
<h1>cpar Perf Report</h1>
{''.join(sections)}
<script>
  document.addEventListener('DOMContentLoaded', function() {{
    {chart_scripts}
  }});
</script>
</body>
</html>"""
