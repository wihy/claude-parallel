"""
Web Dashboard — 替代 Rich Live 终端面板的浏览器仪表盘。

零依赖（仅标准库 http.server + json），双模式:
  1. 附在 cpar run 内 — Orchestrator 启动时挂载，前端展示调度 + 实时 perf
  2. 独立模式 cpar dashboard — 不需要 orchestrator，纯 perf 监控
                            （定位最常见用法：跑着 perf 同时浏览电池/CPU/网络）

设计原则:
  - 状态来源全部通过 callable provider，避免与 orchestrator 强耦合
  - HTTP 服务跑在后台线程，不阻塞 cpar run
  - 前端 1s 轮询 /api/state，极简 HTML 单文件无外部 CDN
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


# ── 状态收集器：从 Orchestrator 抽取 JSON 友好快照 ────────────────────────

def collect_orchestrator_state(orch: Any) -> Dict[str, Any]:
    """从 Orchestrator 实例抽取实时调度状态。orch 可为 None（独立模式）。"""
    if orch is None:
        return {"enabled": False}

    stats = getattr(orch, "stats", None)
    tasks = getattr(orch, "tasks", []) or []
    workers = getattr(orch, "workers", {}) or {}
    results = getattr(orch, "results", {}) or {}

    elapsed = 0.0
    if stats and getattr(stats, "start_time", 0):
        elapsed = max(0.0, time.time() - stats.start_time)

    task_rows: List[Dict[str, Any]] = []
    for t in tasks:
        worker = workers.get(t.id)
        result = results.get(t.id)
        row: Dict[str, Any] = {
            "id": t.id,
            "status": t.status,
            "depends_on": list(getattr(t, "depends_on", []) or []),
            "description": (t.description or "")[:120],
        }
        if result:
            row["cost_usd"] = round(result.cost_usd, 4)
            row["duration_s"] = round(result.duration_s, 1)
            row["num_turns"] = result.num_turns
            row["model"] = result.model_used
            row["retries"] = result.retry_attempt
            if result.error:
                row["error"] = result.error[:200]
        elif worker and worker.is_running:
            row["running_for_s"] = round(worker.elapsed, 1)
        task_rows.append(row)

    return {
        "enabled": True,
        "summary": {
            "total": getattr(stats, "total_tasks", 0) if stats else 0,
            "completed": getattr(stats, "completed", 0) if stats else 0,
            "failed": getattr(stats, "failed", 0) if stats else 0,
            "skipped": getattr(stats, "skipped", 0) if stats else 0,
            "retried": getattr(stats, "retried", 0) if stats else 0,
            "cost_usd": round(getattr(stats, "total_cost_usd", 0.0), 4) if stats else 0.0,
            "elapsed_s": round(elapsed, 1),
            "active_workers": sum(1 for w in workers.values() if w.is_running),
            "current_level": getattr(orch, "_current_level_idx", -1),
            "total_levels": len(getattr(orch, "levels", []) or []),
        },
        "tasks": task_rows,
    }


def collect_perf_state(repo: Optional[Path], coord_dir: str, tag: str, tail_n: int = 60) -> Dict[str, Any]:
    """从 perf JSONL 文件 tail 最新 N 条快照。"""
    if not repo:
        return {"enabled": False}
    perf_root = Path(repo) / coord_dir / "perf" / tag
    if not perf_root.exists():
        return {"enabled": False, "tag": tag, "reason": f"perf 会话目录不存在: {perf_root}"}

    logs_dir = perf_root / "logs"
    state: Dict[str, Any] = {
        "enabled": True,
        "tag": tag,
        "session_dir": str(perf_root),
    }

    # 电池/功耗
    battery_path = logs_dir / "battery.jsonl"
    state["battery"] = _tail_jsonl(battery_path, tail_n)

    # xctrace 实时指标
    metrics_path = logs_dir / "metrics.jsonl"
    state["metrics"] = _tail_jsonl(metrics_path, tail_n)

    # 采样热点（多 cycle 趋势分析需要更长历史）
    hotspots_path = logs_dir / "hotspots.jsonl"
    state["hotspots"] = _tail_jsonl(hotspots_path, max(tail_n, 50))

    # DVT 进程指标 — 实际文件在 logs/dvt/ 子目录；兼容旧路径
    # network 事件流频率高 (每连接每秒一条 update)，单独给更大窗口
    dvt_specs = (
        ("dvt_process",  "dvt_process.jsonl",  tail_n),
        ("dvt_system",   "dvt_system.jsonl",   tail_n),
        ("dvt_network",  "dvt_network.jsonl",  max(tail_n * 10, 600)),
        ("dvt_graphics", "dvt_graphics.jsonl", tail_n),
    )
    for key, fname, n in dvt_specs:
        for cand in (logs_dir / "dvt" / fname, logs_dir / fname):
            if cand.exists():
                state[key] = _tail_jsonl(cand, n)
                break

    # 实时告警 (普通文本日志, 取最后 N 行)
    alerts_path = logs_dir / "alerts.log"
    state["alerts"] = _tail_text(alerts_path, 30)

    # session meta
    meta_path = perf_root / "session.json"
    if meta_path.exists():
        try:
            state["meta"] = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    return state


def _tail_jsonl(path: Path, n: int) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        # 简单 tail：对 < 10MB 的 JSONL 直接全读，避免 seek 复杂度
        # 性能监控 JSONL 通常 KB 级，10 分钟会话 < 1MB
        size = path.stat().st_size
        if size > 10 * 1024 * 1024:
            # 超大文件读最后 ~256KB
            with open(path, "rb") as f:
                f.seek(max(0, size - 256 * 1024))
                data = f.read().decode("utf-8", errors="replace")
                lines = data.splitlines()[1:]  # 丢弃可能不完整的首行
        else:
            lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _tail_text(path: Path, n: int) -> List[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


# ── HTTP Server ───────────────────────────────────────────────────────────

class DashboardServer:
    """后台 HTTP 服务，承载 Web Dashboard。

    用法:
        srv = DashboardServer(port=8765, orch_provider=lambda: orch,
                              perf_provider=lambda: collect_perf_state(...))
        srv.start()
        ...
        srv.stop()
    """

    def __init__(
        self,
        port: int = 8765,
        host: str = "127.0.0.1",
        orch_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        perf_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        title: str = "cpar Dashboard",
    ):
        self.port = port
        self.host = host
        self.orch_provider = orch_provider or (lambda: {"enabled": False})
        self.perf_provider = perf_provider or (lambda: {"enabled": False})
        self.title = title
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> str:
        """启动后台 HTTP 服务，返回访问 URL。"""
        handler_cls = self._make_handler()
        self._server = ThreadingHTTPServer((self.host, self.port), handler_cls)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"cpar-dashboard-{self.port}",
            daemon=True,
        )
        self._thread.start()
        return f"http://{self.host}:{self.port}/"

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _make_handler(self):
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                # 静默：避免污染 cpar 主输出
                return

            def do_GET(self):  # noqa: N802
                if self.path == "/" or self.path == "/index.html":
                    self._respond(200, "text/html; charset=utf-8", _HTML_PAGE.replace("__TITLE__", outer.title))
                elif self.path.startswith("/api/state"):
                    state = {
                        "ts": int(time.time()),
                        "title": outer.title,
                        "orchestrator": _safe_call(outer.orch_provider),
                        "perf": _safe_call(outer.perf_provider),
                    }
                    self._respond(200, "application/json", json.dumps(state, ensure_ascii=False))
                else:
                    self._respond(404, "text/plain", "not found")

            def _respond(self, code: int, content_type: str, body: str):
                payload = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

        return _Handler


def _safe_call(fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    try:
        return fn() or {"enabled": False}
    except Exception as e:
        return {"enabled": False, "error": f"{type(e).__name__}: {e}"}


# ── 内嵌 HTML（单文件、无外部依赖、1s 轮询）──────────────────────────────

_HTML_PAGE = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #0f1115; --card: #181b22; --line: #262a33; --txt: #e6e9ef;
    --muted: #8a92a4; --accent: #4cc2ff; --ok: #4ade80; --warn: #fbbf24;
    --err: #f87171; --info: #93c5fd;
  }
  * { box-sizing: border-box; }
  body { margin:0; font: 13px/1.45 -apple-system, "SF Mono", Menlo, monospace;
         background: var(--bg); color: var(--txt); padding: 16px; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: 12px; margin-bottom: 14px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
          gap: 12px; }
  .card { background: var(--card); border: 1px solid var(--line);
          border-radius: 8px; padding: 12px; min-height: 80px; overflow: hidden; }
  .card h2 { font-size: 13px; font-weight: 600; margin: 0 0 8px;
             color: var(--accent); letter-spacing: 0.04em; }
  .row { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 6px; }
  .stat { display: flex; flex-direction: column; min-width: 60px; }
  .stat .v { font-size: 18px; font-weight: 600; }
  .stat .l { color: var(--muted); font-size: 11px; }
  table { width:100%; border-collapse: collapse; font-size: 12px; }
  th, td { text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--line);
           white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 260px; }
  th { color: var(--muted); font-weight: 500; }
  tr.done   td:first-child::before { content: "✓ "; color: var(--ok); }
  tr.failed td:first-child::before { content: "✗ "; color: var(--err); }
  tr.running td:first-child::before { content: "▶ "; color: var(--info); }
  tr.cancelled td:first-child::before { content: "○ "; color: var(--muted); }
  tr.pending td:first-child::before { content: "· "; color: var(--muted); }
  tr.retrying td:first-child::before { content: "↻ "; color: var(--warn); }
  .pbar { height: 6px; background: var(--line); border-radius: 3px; overflow: hidden;
          margin-top: 6px; }
  .pbar > i { display: block; height: 100%; background: var(--accent); transition: width .4s; }
  pre { background: #0a0c10; padding: 8px; border-radius: 4px; overflow-x: auto;
        font-size: 11px; max-height: 200px; margin: 0; color: #c8cdd6; }
  .pill { display: inline-block; padding: 2px 6px; border-radius: 10px;
          font-size: 11px; background: var(--line); }
  .err { color: var(--err); }
  .ok { color: var(--ok); }
  .muted { color: var(--muted); }
  .spark { display: inline-block; }
  footer { color: var(--muted); font-size: 11px; margin-top: 14px; text-align: center; }
</style>
</head>
<body>
  <h1 id="title">__TITLE__ <span id="hb" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#4ade80;margin-left:8px;vertical-align:middle;transition:opacity .2s;"></span></h1>
  <div class="sub" id="sub">连接中... · 自动刷新 1s</div>

  <div class="grid">
    <div class="card" id="card-orch"><h2>调度概况 (Orchestrator)</h2><div id="orch">—</div></div>
    <div class="card" id="card-perf"><h2>真机功耗 (Battery / Power)</h2><div id="battery">—</div></div>
    <div class="card" id="card-system"><h2>系统指标 (System CPU / Mem)</h2><div id="system">—</div></div>
    <div class="card" id="card-process"><h2>进程指标 (Per-Process via DVT)</h2><div id="process">—</div></div>
    <div class="card" id="card-gpu"><h2>GPU / 帧率 (DVT Graphics)</h2><div id="gpu">—</div></div>
    <div class="card" id="card-net"><h2>网络流 (DVT Network)</h2><div id="net">—</div></div>
    <div class="card" id="card-hot" style="grid-column: 1 / -1;">
      <h2>热点函数 (Sampling Profiler — 多 cycle 趋势聚合)</h2>
      <div id="hotspots">—</div>
    </div>
    <div class="card" id="card-tasks" style="grid-column: 1 / -1;"><h2>任务列表</h2><div id="tasks">—</div></div>
    <div class="card" id="card-alerts" style="grid-column: 1 / -1;"><h2>实时告警 (alerts.log)</h2><div id="alerts">—</div></div>
  </div>

  <footer>cpar Web Dashboard · 数据来源: Orchestrator + perf JSONL</footer>

<script>
const fmt = (n, d=2) => (n == null ? "—" : Number(n).toFixed(d));
const fmtTs = (ts) => ts ? new Date(ts*1000).toLocaleTimeString() : "—";

function lastN(arr, n) { return arr.slice(-n); }

function renderOrch(o) {
  if (!o || !o.enabled) {
    return '<span class="muted">未附加 Orchestrator (独立模式)</span>';
  }
  const s = o.summary || {};
  const total = s.total || 0;
  const finished = (s.completed || 0) + (s.failed || 0) + (s.skipped || 0);
  const pct = total ? Math.round(finished * 100 / total) : 0;
  return `
    <div class="row">
      <div class="stat"><span class="v">${s.completed || 0}</span><span class="l">完成</span></div>
      <div class="stat"><span class="v err">${s.failed || 0}</span><span class="l">失败</span></div>
      <div class="stat"><span class="v muted">${s.skipped || 0}</span><span class="l">跳过</span></div>
      <div class="stat"><span class="v">${s.active_workers || 0}</span><span class="l">活跃</span></div>
      <div class="stat"><span class="v">$${fmt(s.cost_usd, 3)}</span><span class="l">总成本</span></div>
      <div class="stat"><span class="v">${fmt(s.elapsed_s, 0)}s</span><span class="l">已运行</span></div>
    </div>
    <div class="muted">层级 ${s.current_level + 1}/${s.total_levels} · 进度 ${finished}/${total}</div>
    <div class="pbar"><i style="width:${pct}%"></i></div>
  `;
}

function renderTasks(o) {
  if (!o || !o.enabled || !o.tasks || !o.tasks.length) {
    return '<span class="muted">无任务</span>';
  }
  let html = '<table><thead><tr><th>ID</th><th>状态</th><th>耗时</th><th>成本</th><th>轮次</th><th>模型</th><th>描述</th></tr></thead><tbody>';
  for (const t of o.tasks) {
    const dur = t.duration_s != null ? `${fmt(t.duration_s, 1)}s`
              : t.running_for_s != null ? `<span class="info">${fmt(t.running_for_s, 0)}s…</span>` : '—';
    const cost = t.cost_usd != null ? `$${fmt(t.cost_usd, 4)}` : '—';
    const turns = t.num_turns != null ? t.num_turns : '—';
    const model = t.model || '—';
    html += `<tr class="${t.status}"><td>${t.id}</td><td>${t.status}</td><td>${dur}</td><td>${cost}</td><td>${turns}</td><td>${model}</td><td>${t.description || ''}</td></tr>`;
  }
  return html + '</tbody></table>';
}

function renderBattery(p) {
  if (!p || !p.enabled) return '<span class="muted">未启用 perf 采集</span>';
  const recs = p.battery || [];
  if (!recs.length) return '<span class="muted">无电池数据 (battery.jsonl 缺失或为空)</span>';
  const last = recs[recs.length - 1];
  const first = recs[0];
  const drain = (first.level_pct != null && last.level_pct != null) ? (first.level_pct - last.level_pct) : null;
  const window = (first.ts && last.ts) ? Math.round(last.ts - first.ts) : 0;
  // 折算耗电速率 %/min
  const drainRate = (drain != null && window > 0) ? (drain * 60 / window) : null;

  // sparkline 数据
  const levels = recs.map(r => r.level_pct).filter(v => v != null);
  // 颜色: 充电=绿，放电=黄/红
  const sparkColor = last.is_charging ? '#4ade80' : (drainRate > 0.5 ? '#f87171' : '#fbbf24');
  const sparkSvg = sparkline(levels, {w: 260, h: 36, stroke: sparkColor});

  return `
    <div class="row">
      <div class="stat"><span class="v">${last.level_pct ?? '—'}%</span><span class="l">当前电量</span></div>
      <div class="stat"><span class="v ${drain && drain > 0 ? 'err' : 'ok'}">${drain != null ? (drain > 0 ? '-' : '+') + Math.abs(drain) : '—'}%</span><span class="l">总变化</span></div>
      <div class="stat"><span class="v ${drainRate != null && drainRate > 0.3 ? 'warn' : 'ok'}">${drainRate != null ? fmt(drainRate, 2) + '/min' : '—'}</span><span class="l">耗电速率</span></div>
      <div class="stat"><span class="v">${last.is_charging ? '充电中' : '放电中'}</span><span class="l">状态</span></div>
      <div class="stat"><span class="v">${recs.length}</span><span class="l">采样</span></div>
    </div>
    <div style="margin-top:10px;padding:6px;background:#0a0c10;border-radius:4px">
      <div class="muted" style="font-size:10px;margin-bottom:2px">电量曲线 · 窗口 ${fmt(window/60, 1)} 分钟</div>
      ${sparkSvg}
    </div>
    <div class="muted">最新: ${fmtTs(last.ts)} · ${last.fully_charged ? '已充满' : ''}</div>
  `;
}

function renderSystem(p) {
  if (!p || !p.enabled) return '<span class="muted">未启用 perf 采集</span>';
  // 优先使用 dvt_system (新版 sysmontap)，fallback 到 metrics (xctrace 模式)
  const dvtSys = p.dvt_system || [];
  const metrics = p.metrics || [];
  if (dvtSys.length) {
    const last = dvtSys[dvtSys.length - 1];
    const win = dvtSys.length >= 5 ? dvtSys.slice(-5) : dvtSys;
    const avgCpu = win.reduce((s, r) => s + (r.cpuTotal || 0), 0) / win.length;
    const peakCpu = Math.max(...win.map(r => r.cpuTotal || 0));
    const cpuPctOfCore = (last.cpuTotal || 0) / 6;  // 6 核 iPhone15,2 估算
    const memFree = last.physMemoryFreeMB;
    const memUsed = last.physMemoryUsedMB;
    return `
      <div class="row">
        <div class="stat"><span class="v">${fmt(last.cpuTotal, 1)}</span><span class="l">CPU 总负载</span></div>
        <div class="stat"><span class="v">${fmt(cpuPctOfCore, 1)}%</span><span class="l">单核均值</span></div>
        <div class="stat"><span class="v ${peakCpu > 400 ? 'err' : peakCpu > 200 ? 'warn' : 'ok'}">${fmt(peakCpu, 0)}</span><span class="l">5s 峰值</span></div>
        <div class="stat"><span class="v">${memUsed != null ? fmt(memUsed, 0) + 'MB' : '—'}</span><span class="l">已用内存</span></div>
        <div class="stat"><span class="v">${dvtSys.length}</span><span class="l">采样</span></div>
      </div>
      <div class="muted">最新: ${fmtTs(last.ts)} · 来源: dvt_bridge sysmontap</div>
    `;
  }
  if (metrics.length) {
    const last = metrics[metrics.length - 1];
    const cpu = last.cpu_avg ?? last.cpu ?? null;
    const net = last.networking_avg ?? last.net ?? null;
    return `
      <div class="row">
        <div class="stat"><span class="v">${cpu != null ? fmt(cpu, 1) : '—'}</span><span class="l">CPU</span></div>
        <div class="stat"><span class="v">${net != null ? fmt(net, 1) : '—'}</span><span class="l">Network</span></div>
        <div class="stat"><span class="v">${metrics.length}</span><span class="l">样本</span></div>
      </div>
      <div class="muted">最新: ${fmtTs(last.ts)} · 来源: xctrace metrics.jsonl</div>
    `;
  }
  return '<span class="muted">无系统指标 — 启动 dvt_bridge 后自动出现</span>';
}

function renderProcess(p) {
  if (!p || !p.enabled) return '<span class="muted">未启用 perf 采集</span>';
  const recs = p.dvt_process || [];
  if (!recs.length) return '<span class="muted">无进程数据 — 检查 dvt_bridge daemon 是否启动</span>';
  // 按进程名分组，取每个进程最新一条
  const byName = {};
  for (const r of recs) {
    if (!byName[r.name] || r.ts > byName[r.name].ts) byName[r.name] = r;
  }
  // 最近 30s 计算 CPU/MEM 趋势
  const recent = recs.slice(-30);
  const last = recs[recs.length - 1];
  const cpuAvg = recent.reduce((s, r) => s + (r.cpuUsage || 0), 0) / recent.length;
  const cpuPeak = Math.max(...recent.map(r => r.cpuUsage || 0));
  const memDelta = recent.length > 1
    ? (recent[recent.length - 1].physFootprintMB || 0) - (recent[0].physFootprintMB || 0)
    : 0;
  let html = `
    <div class="row">
      <div class="stat"><span class="v ${last.cpuUsage > 80 ? 'err' : last.cpuUsage > 50 ? 'warn' : 'ok'}">${fmt(last.cpuUsage, 1)}%</span><span class="l">${escapeHtml(last.name)} CPU</span></div>
      <div class="stat"><span class="v">${fmt(cpuAvg, 1)}%</span><span class="l">30s 均值</span></div>
      <div class="stat"><span class="v ${cpuPeak > 80 ? 'err' : 'warn'}">${fmt(cpuPeak, 1)}%</span><span class="l">30s 峰值</span></div>
      <div class="stat"><span class="v">${fmt(last.physFootprintMB, 0)}MB</span><span class="l">物理内存</span></div>
      <div class="stat"><span class="v ${memDelta > 50 ? 'err' : memDelta > 10 ? 'warn' : 'muted'}">${memDelta > 0 ? '+' : ''}${fmt(memDelta, 1)}MB</span><span class="l">30s 变化</span></div>
      <div class="stat"><span class="v">${last.threadCount}</span><span class="l">线程数</span></div>
    </div>
    <div class="muted">最新: ${fmtTs(last.ts)} · pid=${last.pid} · 监控进程 ${Object.keys(byName).length} 个</div>
  `;
  // 多进程时列表展示 (按 CPU 降序，含定位按钮)
  if (Object.keys(byName).length > 1) {
    html += '<table style="margin-top:8px"><thead><tr><th>进程</th><th>PID</th><th>CPU</th><th>内存</th><th>线程</th></tr></thead><tbody>';
    const sortedNames = Object.keys(byName).sort((a, b) => (byName[b].cpuUsage || 0) - (byName[a].cpuUsage || 0));
    for (const name of sortedNames) {
      const r = byName[name];
      const cpu = r.cpuUsage || 0;
      const cpuCls = cpu > 50 ? 'err' : (cpu > 20 ? 'warn' : '');
      const memCls = (r.physFootprintMB || 0) > 500 ? 'warn' : '';
      html += `<tr>
        <td>${escapeHtml(name)} <a class="loc-btn" title="复制进程名" onclick="copyText('${escapeAttr(name)}', this); return false">📋</a></td>
        <td>${r.pid}</td>
        <td class="${cpuCls}">${fmt(cpu, 1)}%</td>
        <td class="${memCls}">${fmt(r.physFootprintMB, 0)}MB</td>
        <td>${r.threadCount}</td>
      </tr>`;
    }
    html += '</tbody></table>';
  }
  return html;
}

function renderGpu(p) {
  if (!p || !p.enabled) return '<span class="muted">未启用 perf 采集</span>';
  const recs = p.dvt_graphics || [];

  // 关联：主进程及 WebKit.GPU 进程的 CPU% (作为 per-process GPU 间接指标)
  const procRecs = (p.dvt_process || []).slice(-60);
  const byName = {};
  for (const r of procRecs) {
    if (!byName[r.name] || r.ts > byName[r.name].ts) byName[r.name] = r;
  }
  const soulCpu = byName['Soul_New']?.cpuUsage;
  const wkGpuCpu = byName['com.apple.WebKit.GPU']?.cpuUsage;
  const wkContentCpu = byName['com.apple.WebKit.WebContent']?.cpuUsage;

  if (!recs.length) {
    let html = '<span class="muted">无设备级 GPU 数据 — 启动 dvt_bridge 时加 --collect-graphics</span>';
    if (soulCpu != null || wkGpuCpu != null) {
      html += '<div class="row" style="margin-top:8px">';
      if (soulCpu != null) html += `<div class="stat"><span class="v">${fmt(soulCpu,1)}%</span><span class="l">Soul_New CPU</span></div>`;
      if (wkGpuCpu != null) html += `<div class="stat"><span class="v">${fmt(wkGpuCpu,1)}%</span><span class="l">WebKit.GPU 进程 CPU</span></div>`;
      if (wkContentCpu != null) html += `<div class="stat"><span class="v">${fmt(wkContentCpu,1)}%</span><span class="l">WebKit.WebContent</span></div>`;
      html += '</div>';
    }
    return html;
  }

  const last = recs[recs.length - 1];
  const recent = recs.slice(-30);
  const fpsVals = recent.map(r => r.fps).filter(v => v != null);
  const gpuVals = recent.map(r => r.gpu_util).filter(v => v != null);
  const avgFps = fpsVals.length ? fpsVals.reduce((a,b)=>a+b,0)/fpsVals.length : null;
  const minFps = fpsVals.length ? Math.min(...fpsVals) : null;
  const avgGpu = gpuVals.length ? gpuVals.reduce((a,b)=>a+b,0)/gpuVals.length : null;
  const peakGpu = gpuVals.length ? Math.max(...gpuVals) : null;

  let html = `
    <div class="row">
      <div class="stat"><span class="v ${last.fps && last.fps < 30 ? 'err' : last.fps && last.fps < 50 ? 'warn' : 'ok'}">${last.fps != null ? fmt(last.fps, 0) : '—'}</span><span class="l">FPS (设备)</span></div>
      <div class="stat"><span class="v">${avgFps != null ? fmt(avgFps, 1) : '—'}</span><span class="l">30s 均 FPS</span></div>
      <div class="stat"><span class="v ${minFps && minFps < 24 ? 'err' : 'warn'}">${minFps != null ? fmt(minFps, 0) : '—'}</span><span class="l">最低</span></div>
      <div class="stat"><span class="v ${last.gpu_util > 80 ? 'err' : last.gpu_util > 50 ? 'warn' : 'ok'}">${last.gpu_util != null ? fmt(last.gpu_util, 1) + '%' : '—'}</span><span class="l">GPU 利用率 (设备)</span></div>
      <div class="stat"><span class="v">${avgGpu != null ? fmt(avgGpu, 1) + '%' : '—'}</span><span class="l">30s 均值</span></div>
      <div class="stat"><span class="v">${peakGpu != null ? fmt(peakGpu, 1) + '%' : '—'}</span><span class="l">峰值</span></div>
    </div>
  `;
  // 进程级 CPU 关联 (iOS DVT 不暴露 per-process GPU 拆分，
  // 用 WebKit.GPU 进程 CPU% 作为 H5 GPU 占用代理)
  if (soulCpu != null || wkGpuCpu != null || wkContentCpu != null) {
    html += '<div class="muted" style="margin-top:8px">主进程关联 (per-process CPU 作为 GPU 归因代理):</div>';
    html += '<div class="row">';
    if (soulCpu != null)
      html += `<div class="stat"><span class="v ${soulCpu > 60 ? 'warn' : 'ok'}">${fmt(soulCpu,1)}%</span><span class="l">Soul_New 主进程</span></div>`;
    if (wkGpuCpu != null)
      html += `<div class="stat"><span class="v ${wkGpuCpu > 20 ? 'warn' : 'ok'}">${fmt(wkGpuCpu,1)}%</span><span class="l">WebKit.GPU (H5)</span></div>`;
    if (wkContentCpu != null)
      html += `<div class="stat"><span class="v ${wkContentCpu > 20 ? 'warn' : 'ok'}">${fmt(wkContentCpu,1)}%</span><span class="l">WebKit.WebContent</span></div>`;
    html += '</div>';
  }
  html += `<div class="muted">最新: ${fmtTs(last.ts)} · ${recs.length} 条 GPU 采样</div>`;
  return html;
}

function renderNet(p) {
  if (!p || !p.enabled) return '<span class="muted">未启用 perf 采集</span>';
  const recs = p.dvt_network || [];
  if (!recs.length) return '<span class="muted">无网络数据 — 启动 dvt_bridge 时加 --collect-network</span>';

  // 分类
  const ifaces = recs.filter(r => r.type === 'InterfaceDetectionEvent');
  const conns = recs.filter(r => r.type === 'ConnectionDetectionEvent');
  const updates = recs.filter(r => r.type === 'ConnectionUpdateEvent');

  // 按 connection_serial 取每条连接的最新累计字节
  const lastByConn = {};
  for (const u of updates) {
    const s = u.connection_serial;
    if (s == null) continue;
    if (!lastByConn[s] || u.ts > lastByConn[s].ts) lastByConn[s] = u;
  }
  const activeConns = Object.values(lastByConn);

  // 累计 rx/tx
  const totalRx = activeConns.reduce((s,r)=> s + (r.rx_bytes||0), 0);
  const totalTx = activeConns.reduce((s,r)=> s + (r.tx_bytes||0), 0);

  // 30s 流量速率：取 30s 前 vs 最新的累计差
  const now = updates.length ? updates[updates.length-1].ts : 0;
  const oldUpdates = updates.filter(u => u.ts >= now - 30);
  let rateRx = 0, rateTx = 0, dt = 0;
  if (oldUpdates.length >= 2) {
    const oldByConn = {};
    for (const u of oldUpdates) {
      const s = u.connection_serial;
      if (!oldByConn[s] || u.ts < oldByConn[s].ts) oldByConn[s] = u;
    }
    let oldRx = 0, oldTx = 0;
    for (const s of Object.keys(lastByConn)) {
      if (oldByConn[s]) {
        oldRx += oldByConn[s].rx_bytes || 0;
        oldTx += oldByConn[s].tx_bytes || 0;
      }
    }
    dt = (oldUpdates[oldUpdates.length-1].ts - oldUpdates[0].ts) || 1;
    rateRx = (totalRx - oldRx) / dt;
    rateTx = (totalTx - oldTx) / dt;
  }

  const fmtBytes = (b) => {
    if (b < 1024) return `${b.toFixed(0)}B`;
    if (b < 1024*1024) return `${(b/1024).toFixed(1)}KB`;
    if (b < 1024*1024*1024) return `${(b/(1024*1024)).toFixed(1)}MB`;
    return `${(b/(1024*1024*1024)).toFixed(2)}GB`;
  };
  const fmtRate = (bps) => bps > 0 ? `${fmtBytes(bps)}/s` : '0';

  // Top 5 流量连接
  const topConns = activeConns
    .map(c => ({...c, total: (c.rx_bytes||0) + (c.tx_bytes||0)}))
    .sort((a,b) => b.total - a.total)
    .slice(0, 5);

  let html = `
    <div class="row">
      <div class="stat"><span class="v">${activeConns.length}</span><span class="l">活跃连接</span></div>
      <div class="stat"><span class="v">${ifaces.length}</span><span class="l">网卡事件</span></div>
      <div class="stat"><span class="v">${conns.length}</span><span class="l">新建连接</span></div>
      <div class="stat"><span class="v ${rateRx > 100*1024 ? 'warn' : ''}">${fmtRate(rateRx)}</span><span class="l">↓ 速率 (30s)</span></div>
      <div class="stat"><span class="v ${rateTx > 100*1024 ? 'warn' : ''}">${fmtRate(rateTx)}</span><span class="l">↑ 速率 (30s)</span></div>
      <div class="stat"><span class="v">${fmtBytes(totalRx + totalTx)}</span><span class="l">累计总流量</span></div>
    </div>
  `;
  if (topConns.length) {
    html += '<table style="margin-top:8px"><thead><tr><th>连接#</th><th>↓ Rx</th><th>↑ Tx</th><th>合计</th><th>包数 ↓/↑</th></tr></thead><tbody>';
    for (const c of topConns) {
      html += `<tr><td>${c.connection_serial}</td><td>${fmtBytes(c.rx_bytes||0)}</td><td>${fmtBytes(c.tx_bytes||0)}</td><td>${fmtBytes(c.total)}</td><td>${c.rx_packets||0} / ${c.tx_packets||0}</td></tr>`;
    }
    html += '</tbody></table>';
  }
  if (ifaces.length) {
    const ifList = ifaces.map(i => `${i.name}(${i.interface_index})`).join(' · ');
    html += `<div class="muted" style="margin-top:6px">网卡: ${escapeHtml(ifList)}</div>`;
  }
  return html;
}

function renderHotspots(p) {
  if (!p || !p.enabled) return '<span class="muted">未启用 perf 采集</span>';
  const recs = p.hotspots || [];
  if (!recs.length) return '<span class="muted">无热点数据 (hotspots.jsonl 缺失)</span>';

  // 多 cycle 聚合：每个函数收集所有 cycle 的占比
  // 函数缺席的 cycle 记 0（说明本 cycle 跌出 top）
  const cycleCount = recs.length;
  const funcMap = {};
  for (const cycle of recs) {
    const top = cycle.top || cycle.functions || [];
    const seenInCycle = new Set();
    for (const h of top) {
      const sym = (h.symbol ?? h.name ?? h.function ?? '?').slice(0, 90);
      const pct = h.percentage ?? h.pct ?? h.weight ?? 0;
      if (!funcMap[sym]) funcMap[sym] = { sym, vals: new Array(cycleCount).fill(0), cyclesIn: 0 };
      seenInCycle.add(sym);
      funcMap[sym].vals[recs.indexOf(cycle)] = pct;
    }
  }
  // 计算每个函数的平均、峰值、出现率
  const ranked = Object.values(funcMap).map(f => {
    const vals = f.vals;
    const inCount = vals.filter(v => v > 0).length;
    const sum = vals.reduce((a,b)=>a+b, 0);
    const avgAcrossAll = sum / cycleCount;     // 跨所有 cycle 的均值（0 算入）
    const avgWhenIn = inCount ? sum / inCount : 0;  // 仅出现的 cycle 内均值
    const peak = Math.max(...vals);
    const last = vals[vals.length - 1];
    return {
      sym: f.sym, vals, inCount, peak, last,
      avgAcrossAll, avgWhenIn,
      // 趋势：最后 1/3 cycle 均值 vs 最早 1/3
      trend: vals.length >= 3
        ? (vals.slice(-Math.ceil(vals.length/3)).reduce((a,b)=>a+b,0) / Math.ceil(vals.length/3))
          - (vals.slice(0, Math.ceil(vals.length/3)).reduce((a,b)=>a+b,0) / Math.ceil(vals.length/3))
        : 0,
    };
  }).sort((a, b) => b.avgAcrossAll - a.avgAcrossAll).slice(0, 12);

  if (!ranked.length) return '<span class="muted">所有 cycle 无热点条目</span>';

  // 计算总时间跨度
  const tFirst = recs[0].ts || 0;
  const tLast = recs[recs.length - 1].ts || 0;
  const span = tLast - tFirst;

  // 全局最大 pct（用于柱状条比例）
  const globalMax = Math.max(...ranked.map(r => r.peak));

  let html = `
    <div class="row" style="margin-bottom:8px">
      <div class="stat"><span class="v">${cycleCount}</span><span class="l">cycle 数</span></div>
      <div class="stat"><span class="v">${fmt(span/60, 1)}min</span><span class="l">采集跨度</span></div>
      <div class="stat"><span class="v">${ranked.length}</span><span class="l">展示函数</span></div>
      <div class="stat"><span class="v">${Object.keys(funcMap).length}</span><span class="l">unique 函数</span></div>
      <div class="stat"><span class="v">${fmt(globalMax, 1)}%</span><span class="l">最高峰值</span></div>
    </div>
    <style>
      #hotspots table { font-size: 12px; }
      #hotspots td.func {
        max-width: none; white-space: normal; word-break: break-word;
        font-family: "SF Mono", Menlo, monospace; font-size: 11px;
        line-height: 1.35; padding: 6px;
      }
      #hotspots td.num { text-align: right; font-variant-numeric: tabular-nums; }
      #hotspots tr:hover { background: #1e2230; }
      .locators { display: inline-flex; gap: 4px; margin-left: 6px; vertical-align: middle; }
      .loc-btn {
        display: inline-block; padding: 1px 5px;
        background: #262a33; border-radius: 3px;
        cursor: pointer; text-decoration: none; color: #e6e9ef;
        font-size: 11px; line-height: 1.4;
        transition: background 0.15s;
      }
      .loc-btn:hover { background: #4cc2ff; color: #0f1115; }
    </style>
  `;
  html += `<table>
    <thead><tr>
      <th style="width:60px">排名</th>
      <th style="width:60px" class="num">均 %</th>
      <th style="width:60px" class="num">峰 %</th>
      <th style="width:80px" class="num">出现率</th>
      <th>函数 (demangled)</th>
      <th style="width:160px">各 cycle 趋势</th>
      <th style="width:80px" class="num">变化</th>
    </tr></thead>
    <tbody>`;
  for (let i = 0; i < ranked.length; i++) {
    const f = ranked[i];
    const trendColor = f.trend > 1 ? '#f87171' : (f.trend < -1 ? '#4ade80' : '#4cc2ff');
    const spark = sparkline(f.vals, {w: 150, h: 22, stroke: trendColor, min: 0, max: globalMax});
    const trendIcon = f.trend > 1 ? '↑' : (f.trend < -1 ? '↓' : '→');
    const trendCls = f.trend > 1 ? 'err' : (f.trend < -1 ? 'ok' : 'muted');
    const presentPct = (f.inCount / cycleCount * 100).toFixed(0);
    // 简单分类: 业务 SDK / Apple 系统 / pthread/runtime
    const sym = f.sym;
    let badge = '';
    if (/^realx::/.test(sym)) badge = '<span class="pill" style="background:#7c3aed">RealX SDK</span>';
    else if (/^WebCore::|^WebKit::|^JSC::/.test(sym)) badge = '<span class="pill" style="background:#2563eb">WebKit</span>';
    else if (/^CA::|^CG/.test(sym)) badge = '<span class="pill" style="background:#0891b2">CoreAnimation</span>';
    else if (/^ausdk::|AudioUnit/.test(sym)) badge = '<span class="pill" style="background:#dc2626">Audio</span>';
    else if (/^_pthread|^objc_|^_platform_|^mach_msg|^__/.test(sym)) badge = '<span class="pill" style="background:#525252">Runtime</span>';
    else if (/^0x[0-9a-f]+/.test(sym)) badge = '<span class="pill" style="background:#ca8a04">Unsymbolicated</span>';
    else badge = '<span class="pill" style="background:#16a34a">业务/其他</span>';

    html += `<tr>
      <td>#${i + 1} ${badge}</td>
      <td class="num"><b>${fmt(f.avgAcrossAll, 1)}</b></td>
      <td class="num">${fmt(f.peak, 1)}</td>
      <td class="num">${f.inCount}/${cycleCount}<br><span class="muted" style="font-size:10px">${presentPct}%</span></td>
      <td class="func">${escapeHtml(f.sym)}${renderLocators(f.sym)}</td>
      <td>${spark}</td>
      <td class="num"><span class="${trendCls}">${trendIcon} ${f.trend >= 0 ? '+' : ''}${fmt(f.trend, 1)}</span></td>
    </tr>`;
  }
  html += '</tbody></table>';
  return html;
}

function renderAlerts(p) {
  if (!p || !p.enabled) return '<span class="muted">未启用 perf 采集</span>';
  const lines = p.alerts || [];
  if (!lines.length) return '<span class="muted">无告警</span>';
  return '<pre>' + escapeHtml(lines.slice(-30).join('\n')) + '</pre>';
}

// ── 代码定位工具 ──

// 识别符号类型并返回最佳搜索 URL 集合
function symbolLocators(sym) {
  const links = [];
  // 清理：去掉模板参数和函数签名后括号，方便搜索
  const cleanSym = sym.replace(/\(.*$/, '').replace(/<.*>/g, '');
  const enc = encodeURIComponent(cleanSym);
  const encFull = encodeURIComponent(sym);

  if (/^realx::|^Realx::|^RX[A-Z]/.test(sym)) {
    // 第三方 SDK — GitHub 全局搜
    links.push({ icon: '🔍', label: 'GitHub', url: `https://github.com/search?q=${enc}&type=code` });
    links.push({ icon: '📚', label: 'CocoaPods', url: `https://cocoapods.org/?q=${encodeURIComponent('realx')}` });
  } else if (/^WebCore::|^WebKit::|^JSC::|^bmalloc::/.test(sym)) {
    // WebKit 开源
    links.push({ icon: '🌐', label: 'WebKit GitHub', url: `https://github.com/WebKit/WebKit/search?q=${enc}` });
    links.push({ icon: '📚', label: 'Apple Dev', url: `https://developer.apple.com/search/?q=${enc}` });
  } else if (/^CA::|^CG[A-Z]|^CF[A-Z]|^NS[A-Z]|^UI[A-Z]/.test(sym)) {
    // Apple 系统符号
    links.push({ icon: '🍎', label: 'Apple Dev', url: `https://developer.apple.com/search/?q=${enc}` });
    links.push({ icon: '🔍', label: 'Google', url: `https://www.google.com/search?q=${enc}+site:developer.apple.com` });
  } else if (/^ausdk::|AudioUnit|CoreAudio/.test(sym)) {
    links.push({ icon: '🍎', label: 'Audio Docs', url: `https://developer.apple.com/search/?q=${enc}` });
  } else if (/^_pthread|^pthread_/.test(sym)) {
    links.push({ icon: '📖', label: 'man', url: `https://www.man7.org/linux/man-pages/man3/${enc.replace('_pthread_', 'pthread_')}.3.html` });
  } else if (/^objc_|^_objc_/.test(sym)) {
    links.push({ icon: '🍎', label: 'objc4', url: `https://github.com/apple-oss-distributions/objc4/search?q=${enc}` });
  } else if (/^mach_|^_mach_/.test(sym)) {
    links.push({ icon: '🍎', label: 'XNU', url: `https://github.com/apple-oss-distributions/xnu/search?q=${enc}` });
  } else if (/^0x[0-9a-fA-F]+/.test(sym)) {
    // 未符号化的地址 — 提示需要 dSYM
    links.push({ icon: '⚠️', label: '需 dSYM', url: '#' });
  } else {
    // 业务/未分类 — 多种搜索
    links.push({ icon: '🔍', label: 'GitHub', url: `https://github.com/search?q=${enc}&type=code` });
    links.push({ icon: '🌐', label: 'Google', url: `https://www.google.com/search?q=${encFull}` });
  }
  return links;
}

// 渲染函数名旁边的定位按钮组
function renderLocators(sym) {
  const links = symbolLocators(sym);
  let html = ` <span class="locators">`;
  // 复制按钮
  html += `<a class="loc-btn" title="复制函数名" onclick="copyText('${escapeAttr(sym)}', this); return false">📋</a>`;
  // 跳转按钮
  for (const l of links) {
    if (l.url === '#') {
      html += `<span class="loc-btn" style="opacity:0.5" title="${l.label}">${l.icon}</span>`;
    } else {
      html += `<a class="loc-btn" target="_blank" rel="noopener" href="${l.url}" title="${l.label}: ${escapeAttr(sym)}">${l.icon}</a>`;
    }
  }
  return html + '</span>';
}

function escapeAttr(s) {
  return String(s).replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

function copyText(text, el) {
  navigator.clipboard.writeText(text).then(() => {
    if (el) {
      const orig = el.innerHTML;
      el.innerHTML = '✓';
      el.style.color = '#4ade80';
      setTimeout(() => { el.innerHTML = orig; el.style.color = ''; }, 800);
    }
  }).catch(err => {
    console.error('copy failed:', err);
    // fallback: 弹 prompt
    prompt('Ctrl+C 复制:', text);
  });
}

// SVG sparkline — 极简折线图
function sparkline(values, opts) {
  opts = opts || {};
  const w = opts.w || 120, h = opts.h || 28;
  const stroke = opts.stroke || '#4cc2ff';
  const fill = opts.fill || 'none';
  const showDot = opts.showDot !== false;
  const baseline = opts.baseline;  // 可选基线值，画虚线
  if (!values || values.length === 0) return '';
  const vs = values.filter(v => v != null && !isNaN(v));
  if (!vs.length) return '';
  const min = opts.min != null ? opts.min : Math.min(...vs);
  const max = opts.max != null ? opts.max : Math.max(...vs);
  const range = (max - min) || 1;
  const step = vs.length > 1 ? w / (vs.length - 1) : 0;
  const pts = vs.map((v, i) => `${(i*step).toFixed(1)},${(h - ((v - min) / range) * (h - 4) - 2).toFixed(1)}`);
  const polyline = `<polyline fill="${fill}" stroke="${stroke}" stroke-width="1.5" points="${pts.join(' ')}"/>`;
  let dot = '';
  if (showDot && pts.length) {
    const last = pts[pts.length - 1].split(',');
    dot = `<circle cx="${last[0]}" cy="${last[1]}" r="2.5" fill="${stroke}"/>`;
  }
  let baseLine = '';
  if (baseline != null && baseline >= min && baseline <= max) {
    const y = (h - ((baseline - min) / range) * (h - 4) - 2).toFixed(1);
    baseLine = `<line x1="0" y1="${y}" x2="${w}" y2="${y}" stroke="#444" stroke-dasharray="2,2"/>`;
  }
  return `<svg width="${w}" height="${h}" style="vertical-align:middle;display:inline-block">
    ${baseLine}${polyline}${dot}
  </svg>`;
}

// SVG 横向柱状条 (单值)
function bar(value, max, w, color) {
  w = w || 80;
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  const c = color || '#4cc2ff';
  return `<span style="display:inline-block;width:${w}px;height:6px;background:#262a33;border-radius:3px;vertical-align:middle">
    <span style="display:block;width:${pct.toFixed(1)}%;height:100%;background:${c};border-radius:3px"></span>
  </span>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

let _tickCount = 0;
async function tick() {
  const hb = document.getElementById('hb');
  try {
    const r = await fetch('/api/state', { cache: 'no-store' });
    if (!r.ok) throw new Error('http ' + r.status);
    const s = await r.json();
    _tickCount++;
    // 心跳: 闪一下 (绿)
    hb.style.opacity = '0.2';
    setTimeout(() => { hb.style.opacity = '1'; }, 150);

    const p = s.perf || {};
    const counts = `battery:${(p.battery||[]).length} · metrics:${(p.metrics||[]).length} · dvt_proc:${(p.dvt_process||[]).length} · dvt_net:${(p.dvt_network||[]).length} · dvt_gfx:${(p.dvt_graphics||[]).length} · hotspots:${(p.hotspots||[]).length} · alerts:${(p.alerts||[]).length}`;

    document.getElementById('sub').innerHTML =
      `更新 #${_tickCount} · ${fmtTs(s.ts)} · 自动刷新 1s · 数据量: <span class="muted">${counts}</span>`;

    document.getElementById('orch').innerHTML = renderOrch(s.orchestrator);
    document.getElementById('tasks').innerHTML = renderTasks(s.orchestrator);
    document.getElementById('battery').innerHTML = renderBattery(s.perf);
    document.getElementById('system').innerHTML = renderSystem(s.perf);
    document.getElementById('process').innerHTML = renderProcess(s.perf);
    document.getElementById('hotspots').innerHTML = renderHotspots(s.perf);
    document.getElementById('gpu').innerHTML = renderGpu(s.perf);
    document.getElementById('net').innerHTML = renderNet(s.perf);
    document.getElementById('alerts').innerHTML = renderAlerts(s.perf);
  } catch (e) {
    hb.style.background = '#f87171';
    document.getElementById('sub').innerHTML = '<span class="err">连接错误 #' + _tickCount + ': ' + escapeHtml(e.message) + '</span>';
  }
}

tick();
setInterval(tick, 1000);
</script>
</body>
</html>
"""
