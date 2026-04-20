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

    # 采样热点
    hotspots_path = logs_dir / "hotspots.jsonl"
    state["hotspots"] = _tail_jsonl(hotspots_path, 5)

    # DVT 进程指标 (network / graphics)
    dvt_proc_path = logs_dir / "dvt_process.jsonl"
    if dvt_proc_path.exists():
        state["dvt_process"] = _tail_jsonl(dvt_proc_path, tail_n)
    dvt_sys_path = logs_dir / "dvt_system.jsonl"
    if dvt_sys_path.exists():
        state["dvt_system"] = _tail_jsonl(dvt_sys_path, tail_n)

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
  <h1 id="title">__TITLE__</h1>
  <div class="sub" id="sub">连接中... · 自动刷新 1s</div>

  <div class="grid">
    <div class="card" id="card-orch"><h2>调度概况 (Orchestrator)</h2><div id="orch">—</div></div>
    <div class="card" id="card-perf"><h2>真机功耗 (Battery / Power)</h2><div id="battery">—</div></div>
    <div class="card" id="card-metrics"><h2>实时指标 (CPU / Network)</h2><div id="metrics">—</div></div>
    <div class="card" id="card-hot"><h2>热点函数 (Sampling)</h2><div id="hotspots">—</div></div>
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
  return `
    <div class="row">
      <div class="stat"><span class="v">${last.level_pct ?? '—'}%</span><span class="l">当前电量</span></div>
      <div class="stat"><span class="v ${drain && drain > 0 ? 'err' : 'ok'}">${drain != null ? (drain > 0 ? '-' : '+') + Math.abs(drain) : '—'}%</span><span class="l">变化 (${window}s)</span></div>
      <div class="stat"><span class="v">${last.is_charging ? '充电中' : '放电中'}</span><span class="l">状态</span></div>
      <div class="stat"><span class="v">${recs.length}</span><span class="l">采样数</span></div>
    </div>
    <div class="muted">最新: ${fmtTs(last.ts)} · session=${p.tag || '?'}</div>
  `;
}

function renderMetrics(p) {
  if (!p || !p.enabled) return '<span class="muted">未启用 perf 采集</span>';
  const recs = p.metrics || [];
  if (!recs.length) return '<span class="muted">无指标 (metrics.jsonl 缺失或为空)</span>';
  const last = recs[recs.length - 1];
  // 兼容多种字段
  const cpu = last.cpu_avg ?? last.cpu ?? last.cpu_pct ?? null;
  const net = last.networking_avg ?? last.net ?? last.networking ?? null;
  const display = last.display_avg ?? last.display ?? null;
  return `
    <div class="row">
      <div class="stat"><span class="v">${cpu != null ? fmt(cpu, 1) : '—'}</span><span class="l">CPU</span></div>
      <div class="stat"><span class="v">${net != null ? fmt(net, 1) : '—'}</span><span class="l">Network</span></div>
      <div class="stat"><span class="v">${display != null ? fmt(display, 1) : '—'}</span><span class="l">Display</span></div>
      <div class="stat"><span class="v">${recs.length}</span><span class="l">样本</span></div>
    </div>
    <div class="muted">最新更新: ${fmtTs(last.ts)}</div>
  `;
}

function renderHotspots(p) {
  if (!p || !p.enabled) return '<span class="muted">未启用 perf 采集</span>';
  const recs = p.hotspots || [];
  if (!recs.length) return '<span class="muted">无热点数据 (hotspots.jsonl 缺失)</span>';
  // 取最新一条快照中的 top
  const latest = recs[recs.length - 1];
  const top = (latest.top || latest.functions || []).slice(0, 8);
  if (!top.length) return '<span class="muted">最新快照无热点条目</span>';
  let html = '<table><thead><tr><th>占比</th><th>函数</th></tr></thead><tbody>';
  for (const h of top) {
    const pct = h.percentage ?? h.pct ?? h.weight ?? null;
    const sym = h.symbol ?? h.name ?? h.function ?? '?';
    html += `<tr><td>${pct != null ? fmt(pct, 1) + '%' : '—'}</td><td>${escapeHtml(sym)}</td></tr>`;
  }
  return html + '</tbody></table>';
}

function renderAlerts(p) {
  if (!p || !p.enabled) return '<span class="muted">未启用 perf 采集</span>';
  const lines = p.alerts || [];
  if (!lines.length) return '<span class="muted">无告警</span>';
  return '<pre>' + escapeHtml(lines.slice(-30).join('\n')) + '</pre>';
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function tick() {
  try {
    const r = await fetch('/api/state', { cache: 'no-store' });
    if (!r.ok) throw new Error('http ' + r.status);
    const s = await r.json();
    document.getElementById('title').textContent = s.title || 'cpar Dashboard';
    document.getElementById('sub').textContent = `更新: ${fmtTs(s.ts)} · 自动刷新 1s`;
    document.getElementById('orch').innerHTML = renderOrch(s.orchestrator);
    document.getElementById('tasks').innerHTML = renderTasks(s.orchestrator);
    document.getElementById('battery').innerHTML = renderBattery(s.perf);
    document.getElementById('metrics').innerHTML = renderMetrics(s.perf);
    document.getElementById('hotspots').innerHTML = renderHotspots(s.perf);
    document.getElementById('alerts').innerHTML = renderAlerts(s.perf);
  } catch (e) {
    document.getElementById('sub').innerHTML = '<span class="err">连接错误: ' + escapeHtml(e.message) + '</span>';
  }
}

tick();
setInterval(tick, 1000);
</script>
</body>
</html>
"""
