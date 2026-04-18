"""
report_html — 将 perf report 生成为自包含 HTML 报告。

特性:
- chart.js 时序图: CPU/内存/功耗趋势
- DvtBridge 进程指标可视化
- 采样热点排名
- Syslog 告警统计
- Timeline 事件甘特图
- Gate/回归检测结果
- 零外部依赖: chart.js 通过 CDN 加载，CSS 内联
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .dvt_bridge import read_dvt_process_jsonl, read_dvt_system_jsonl
from .device_metrics import read_battery_jsonl
from .sampling import read_hotspots_jsonl


def generate_html_report(
    report: Dict[str, Any],
    meta: Dict[str, Any],
    session_root: Path,
    output_path: Optional[Path] = None,
) -> Path:
    """
    从 perf report + meta + JSONL 数据生成自包含 HTML 报告。

    Args:
        report: report.json 内容
        meta: meta.json 内容
        session_root: perf session 根目录 (.claude-parallel/perf/<tag>/)
        output_path: 输出 HTML 路径（默认 session_root/report.html）

    Returns:
        生成的 HTML 文件路径
    """
    if output_path is None:
        output_path = session_root / "report.html"

    logs_dir = session_root / "logs"

    # 收集各数据源
    chart_configs = []

    # 1. DvtBridge 进程指标时序图
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

    # 2. 电池趋势图
    batt = meta.get("battery", {})
    if batt.get("jsonl"):
        batt_path = Path(batt["jsonl"])
        if batt_path.exists():
            batt_data = read_battery_jsonl(batt_path)
            if batt_data:
                chart_configs.append(_build_battery_chart(batt_data))

    # 3. 采样热点数据
    hotspots = []
    sampling_meta = meta.get("sampling", {})
    if sampling_meta.get("hotspots_file"):
        hs_path = Path(sampling_meta["hotspots_file"])
        if hs_path.exists():
            hotspots = read_hotspots_jsonl(hs_path)

    # 4. 构建页面各 Section
    sections = []

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

    # Charts
    if chart_configs:
        sections.append(_section_charts(chart_configs))

    # DvtBridge 进程指标表
    if dvt_metrics.get("process_stats"):
        sections.append(_section_dvt_process_table(dvt_metrics))

    # 采样热点
    if hotspots:
        sections.append(_section_hotspots(hotspots))

    # Timeline
    timeline = report.get("timeline", {})
    if timeline.get("events"):
        sections.append(_section_timeline(timeline))

    # Syslog
    syslog = report.get("syslog", {})
    if syslog.get("lines", 0) > 0:
        sections.append(_section_syslog(syslog))

    # Live Analysis
    live = report.get("live_analysis", {})
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


def _build_dvt_process_chart(records: List[Dict]) -> Dict:
    """构建 DvtBridge 进程 CPU/内存时序图配置。"""
    # 按进程名分组
    by_name: Dict[str, List[Dict]] = {}
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
        labels = [time.strftime("%H:%M:%S", time.localtime(r.get("ts", 0))) for r in recs]
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

    all_labels = [time.strftime("%H:%M:%S", time.localtime(r.get("ts", 0))) for r in records[:1]]

    return {
        "id": "dvt_process",
        "title": "DvtBridge 进程指标趋势",
        "charts": [
            {
                "subtitle": "CPU 使用率 (%)",
                "type": "line",
                "labels": labels if len(by_name) == 1 else labels,
                "datasets": datasets_cpu,
                "yLabel": "CPU %",
            },
            {
                "subtitle": "物理内存 (MB)",
                "type": "line",
                "labels": labels if len(by_name) == 1 else labels,
                "datasets": datasets_mem,
                "yLabel": "MB",
            },
        ],
    }


def _build_dvt_system_chart(records: List[Dict]) -> Dict:
    """构建系统级指标时序图。"""
    labels = [time.strftime("%H:%M:%S", time.localtime(r.get("ts", 0))) for r in records]
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
    """构建电池趋势图。"""
    labels = [time.strftime("%H:%M:%S", time.localtime(r.get("ts", 0))) for r in records]
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


def _section_charts(chart_configs):
    nav_items = []
    chart_divs = []

    for cfg in chart_configs:
        cid = cfg["id"]
        nav_items.append(f'<a href="#{cid}" class="chart-nav-item">{cfg["title"]}</a>')
        sub_charts = []
        for j, ch in enumerate(cfg.get("charts", [])):
            sub_id = f"{cid}_chart_{j}"
            sub_charts.append(f"""
            <div class="chart-sub">
                <h4>{ch["subtitle"]}</h4>
                <canvas id="{sub_id}"></canvas>
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
    rows = []
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
        rows.append(f"""
            <tr>
                <td>{name}</td>
                <td>{stats.get('pid', '?')}</td>
                <td>{stats.get('samples', 0)}</td>
                <td>{cpu_avg_s}</td>
                <td>{cpu_peak_s}</td>
                <td>{mem_avg_s}</td>
                <td>{mem_peak_s}</td>
            </tr>""")

    return f"""
    <div class="card">
        <h3>DvtBridge 进程指标统计</h3>
        <table>
            <thead><tr><th>Process</th><th>PID</th><th>Samples</th><th>CPU Avg%</th><th>CPU Peak%</th><th>MEM Avg MB</th><th>MEM Peak MB</th></tr></thead>
            <tbody>{"".join(rows)}</tbody>
        </table>
    </div>"""


def _section_hotspots(hotspots):
    # 聚合热点
    agg = hotspots[-1] if hotspots else {}
    top = agg.get("top", [])
    if not top:
        return ""

    rows = []
    for i, h in enumerate(top[:20]):
        sym = h.get("symbol", "?")
        weight = h.get("weight_pct", 0)
        bar_width = min(weight * 3, 100)
        rows.append(f"""
            <tr>
                <td>{i + 1}</td>
                <td class="symbol">{sym[:100]}</td>
                <td>{weight:.2f}%</td>
                <td><div class="bar" style="width: {bar_width}%"></div></td>
            </tr>""")

    return f"""
    <div class="card">
        <h3>Sampling Hotspots (Top 20)</h3>
        <table>
            <thead><tr><th>#</th><th>Symbol</th><th>Weight</th><th></th></tr></thead>
            <tbody>{"".join(rows)}</tbody>
        </table>
    </div>"""


def _section_timeline(timeline):
    levels = timeline.get("levels", [])
    if not levels:
        return ""

    rows = []
    for lvl in levels:
        dur = lvl.get("duration_sec")
        dur_s = f"{dur:.1f}s" if dur else "N/A"
        tasks = ", ".join(lvl.get("tasks", []))
        rows.append(f"""
            <tr>
                <td>Level {lvl['level_idx']}</td>
                <td>{dur_s}</td>
                <td>{tasks}</td>
            </tr>""")

    return f"""
    <div class="card">
        <h3>Timeline ({timeline.get('events', 0)} events)</h3>
        <table>
            <thead><tr><th>Level</th><th>Duration</th><th>Tasks</th></tr></thead>
            <tbody>{"".join(rows)}</tbody>
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


# ── Full Page Renderer ──


def _render_full_page(tag, sections, chart_configs):
    """渲染完整 HTML 页面。"""

    # 构建 chart.js 初始化脚本
    chart_scripts = ""
    for cfg in chart_configs:
        for j, ch in enumerate(cfg.get("charts", [])):
            sub_id = f"{cfg['id']}_chart_{j}"
            labels_json = json.dumps(ch.get("labels", []))
            datasets_json = json.dumps(ch.get("datasets", []))
            y_label = ch.get("yLabel", "")
            chart_type = ch.get("type", "line")

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
                    y: {{ title: {{ display: true, text: '{y_label}' }} }}
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
  @media (max-width: 600px) {{
    .chart-grid {{ grid-template-columns: 1fr; }}
    .meta-grid {{ grid-template-columns: 1fr 1fr; }}
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
