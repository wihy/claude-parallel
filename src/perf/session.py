"""
PerfSessionManager — 性能采集会话的完整生命周期管理。

提供:
- 真机 syslog 采集 (idevicesyslog)
- Instruments Power Profiler 长录制 (xcrun xctrace record)
- timeline 事件打点
- 采集会话的 start/stop/tail/report
- 基线对比 + 回归门禁
"""

import json
import os
import signal
import threading
import time
from pathlib import Path
from typing import Optional, Dict, Any

from .config import PerfConfig
from .protocol.device import BatteryPoller, ProcessMetricsStreamer
from .capture.webcontent import WebContentProfiler
from .protocol.dvt import DvtBridgeThread
from .locate.resolver import SymbolResolver
from .capture.sampling import SamplingProfilerSidecar
from ..infrastructure.storage.atomic import atomic_write_json, safe_read_json


class PerfSessionManager:
    def __init__(self, repo: str, coordination_dir: str, config: PerfConfig):
        self.repo = Path(repo).expanduser().resolve()
        self.config = config
        self.coordination_dir = coordination_dir
        self.root = self.repo / coordination_dir / "perf" / config.tag
        self.logs_dir = self.root / "logs"
        self.traces_dir = self.root / "traces"
        self.exports_dir = self.root / "exports"
        self.meta_file = self.root / "meta.json"
        self.timeline_file = self.root / "timeline.json"
        self.report_file = self.root / "report.json"
        self.sampling_sidecar: Optional[SamplingProfilerSidecar] = None
        self.battery_poller: Optional[BatteryPoller] = None
        self.process_streamer: Optional[ProcessMetricsStreamer] = None
        self.dvt_bridge_thread: Optional[DvtBridgeThread] = None
        self.webcontent_profiler: Optional[WebContentProfiler] = None
        self._resolver: Optional[SymbolResolver] = None

    # ---------- lifecycle ----------
    def start(self) -> Dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.traces_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)

        existing = self._load_meta()
        if existing.get("status") == "running":
            return existing

        # 启动符号解析器 (后台 warmup,sampling 起来时已可用)
        self._wire_resolver(self.config)

        meta = self._init_meta()
        self._start_syslog(meta)
        self._start_battery(meta)
        self._start_dvt(meta)
        self._start_xctrace(meta)
        self._start_sampling(meta)
        self._start_webcontent(meta)

        self._save_meta(meta)
        if not self.timeline_file.exists():
            atomic_write_json(self.timeline_file, {"events": []})
        self.mark_event("perf_session_started", detail="collector booted")
        return meta

    # ---------- start() 子步骤 (Task 35: 拆分原 370 行 start) ----------
    # 实现委派到 capture/starters.py,类上保留薄包装以便测试/子类覆盖。
    def _init_meta(self) -> Dict[str, Any]:
        """构建 meta dict 骨架 — 只填静态字段,子系统字段由各 _start_* 添加."""
        return {
            "tag": self.config.tag,
            "repo": str(self.repo),
            "started_at": time.time(),
            "ended_at": 0,
            "status": "running",
            "device": self.config.device,
            "attach": self.config.attach,
            "templates": self.config.templates,
            "duration_sec": self.config.duration_sec,
            "baseline_tag": self.config.baseline_tag,
            "threshold_pct": self.config.threshold_pct,
            "syslog": {
                "enabled": False,
                "pid": 0,
                "log": str(self.logs_dir / "syslog_full.log"),
                "reliable": None,
            },
            "xctrace": {
                "enabled": False,
                "pid": 0,
                "trace": str(self.traces_dir / "power.trace"),
                "stderr": str(self.logs_dir / "xctrace.stderr.log"),
            },
            "errors": [],
        }

    def _start_syslog(self, meta: Dict[str, Any]) -> None:
        from .capture.starters import start_syslog
        start_syslog(self, meta)

    def _start_battery(self, meta: Dict[str, Any]) -> None:
        from .capture.starters import start_battery
        start_battery(self, meta)

    def _start_dvt(self, meta: Dict[str, Any]) -> None:
        from .capture.starters import start_dvt
        start_dvt(self, meta)

    def _start_xctrace(self, meta: Dict[str, Any]) -> None:
        from .capture.starters import start_xctrace
        start_xctrace(self, meta)

    def _start_sampling(self, meta: Dict[str, Any]) -> None:
        from .capture.starters import start_sampling
        start_sampling(self, meta)

    def _start_webcontent(self, meta: Dict[str, Any]) -> None:
        from .capture.starters import start_webcontent
        start_webcontent(self, meta)

    def stop(self) -> Dict[str, Any]:
        meta = self._load_meta()
        if not meta:
            return {}

        # 先停旁路 (in-process sidecar 或从 meta 恢复的 daemon PID)
        sampling_pid = meta.get("sampling", {}).get("pid", 0)
        if self.sampling_sidecar:
            sampling_result = self.sampling_sidecar.stop()
            meta["sampling_result"] = sampling_result
        elif sampling_pid:
            self._kill_pid(sampling_pid)
            meta["sampling_result"] = {"stopped_pid": sampling_pid}

        # 停 battery
        batt = meta.get("battery", {})
        if self.battery_poller:
            self.battery_poller.stop()
        elif batt.get("pid"):
            self._kill_pid(batt["pid"])

        # 停 device metrics (DvtBridge 优先)
        if self.dvt_bridge_thread and self.dvt_bridge_thread.is_alive():
            dvt_result = self.dvt_bridge_thread.stop()
            meta["dvt_bridge_result"] = dvt_result
        dm = meta.get("device_metrics", {})
        if self.process_streamer:
            self.process_streamer.stop()
        elif dm.get("process_pid"):
            self._kill_pid(dm["process_pid"])

        # 停 webcontent profiler
        wc = meta.get("webcontent", {})
        if self.webcontent_profiler:
            self.webcontent_profiler.stop()
        elif wc.get("daemon_pid"):
            self._kill_pid(wc["daemon_pid"])

        self._kill_pid(meta.get("syslog", {}).get("pid", 0))
        self._kill_pid(meta.get("xctrace", {}).get("pid", 0))
        # 多模板进程
        for entry in meta.get("xctrace_multi", []):
            self._kill_pid(entry.get("pid", 0))

        # 所有 sidecar 停完后再关 resolver — 让 in-flight lookup 有机会落 cache
        self._teardown_resolver()

        meta["ended_at"] = time.time()
        meta["status"] = "stopped"
        self._save_meta(meta)

        self._check_syslog_reliability(meta)
        self.mark_event("perf_session_stopped", detail="collector stopped")
        return meta

    def tail_syslog(self, lines: int = 80) -> str:
        meta = self._load_meta()
        log_path = Path(meta.get("syslog", {}).get("log", ""))
        if not log_path.exists():
            return "[perf] syslog file not found"
        data = log_path.read_text(errors="replace").splitlines()
        return "\n".join(data[-lines:])

    def mark_event(self, name: str, detail: str = "", level_idx: Optional[int] = None, tasks: Optional[list] = None):
        payload = safe_read_json(self.timeline_file, {"events": []}) or {"events": []}
        if "events" not in payload or not isinstance(payload["events"], list):
            payload["events"] = []
        payload["events"].append({
            "ts": time.time(),
            "event": name,
            "detail": detail,
            "level_idx": level_idx,
            "tasks": tasks or [],
        })
        atomic_write_json(self.timeline_file, payload)

    # ---------- analysis ----------
    def report(self, with_callstack: bool = False, callstack_top_n: int = 20) -> Dict[str, Any]:
        from concurrent.futures import ThreadPoolExecutor

        meta = self._load_meta()

        # 并行执行独立的分析任务
        with ThreadPoolExecutor(max_workers=4) as pool:
            f_syslog = pool.submit(self._syslog_stats, meta)
            f_timeline = pool.submit(self._timeline_stats)
            f_metrics = pool.submit(self._trace_metrics, meta)
            f_callstack = (
                pool.submit(self.callstack, top_n=callstack_top_n)
                if with_callstack else None
            )

        report = {
            "tag": self.config.tag,
            "status": meta.get("status", "unknown"),
            "syslog": f_syslog.result(),
            "timeline": f_timeline.result(),
            "metrics": f_metrics.result(),
            "baseline": {},
            "gate": {"checked": False, "passed": True, "reason": ""},
        }

        if f_callstack:
            report["callstack"] = f_callstack.result()

        # DvtBridge 进程指标分析
        dvt_data = self._dvt_metrics_report(meta)
        if dvt_data:
            report["dvt_metrics"] = dvt_data

        if self.config.baseline_tag:
            baseline = PerfSessionManager(str(self.repo), self.coordination_dir, PerfConfig(tag=self.config.baseline_tag))
            base_meta = baseline._load_meta()
            base_metrics = baseline._trace_metrics(base_meta)
            report["baseline"] = {
                "tag": self.config.baseline_tag,
                "metrics": base_metrics,
                "delta": self._calc_delta(base_metrics, report["metrics"]),
            }
            if self.config.threshold_pct > 0:
                report["gate"] = self._gate_check(report["baseline"]["delta"], self.config.threshold_pct)

        atomic_write_json(self.report_file, report)
        return report

    # ---------- internals (metrics 委派到 analyze/metrics) ----------
    def _trace_metrics(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        from .analyze.metrics import compute_trace_metrics
        return compute_trace_metrics(meta, self.exports_dir)

    def _calc_delta(self, base: Dict[str, Any], cur: Dict[str, Any]) -> Dict[str, Any]:
        from .analyze.metrics import calc_delta
        return calc_delta(base, cur)

    def _gate_check(self, delta: Dict[str, Any], threshold_pct: float) -> Dict[str, Any]:
        from .analyze.metrics import gate_check
        return gate_check(delta, threshold_pct)

    def _timeline_stats(self) -> Dict[str, Any]:
        from .analyze.syslog_stats import compute_timeline_stats
        return compute_timeline_stats(self.timeline_file)

    def _syslog_stats(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        from .analyze.syslog_stats import compute_syslog_stats
        return compute_syslog_stats(meta)

    def _check_syslog_reliability(self, meta: Dict[str, Any]):
        from .analyze.syslog_stats import check_syslog_reliability
        check_syslog_reliability(meta, save_meta=self._save_meta)

    def _kill_pid(self, pid: int, grace_seconds: float = 5.0):
        """SIGTERM → 每 0.2s 探测 → grace 耗尽则 SIGKILL."""
        if not pid:
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            return

        deadline = time.time() + grace_seconds
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            except Exception:
                return
            time.sleep(0.2)

        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            return

    def _load_meta(self) -> Dict[str, Any]:
        if not self.meta_file.exists():
            return {}
        try:
            return json.loads(self.meta_file.read_text())
        except Exception:
            return {}

    def _save_meta(self, meta: Dict[str, Any]):
        atomic_write_json(self.meta_file, meta)

    # ---------- resolver wiring (Task 5) ----------
    def _wire_resolver(self, cfg) -> None:
        """session.start() 调用 — 构建 resolver + 后台 warmup。

        若 cfg 里没配 binary/linkmap/dsym,from_config 返回 None —
        此时 self._resolver 保持 None,sampling 会走原 hex 路径 (零破坏)。
        """
        self._resolver = SymbolResolver.from_config(cfg, self.repo)
        if self._resolver is not None:
            threading.Thread(
                target=self._resolver.warmup,
                name="resolver-warmup",
                daemon=True,
            ).start()

    def _teardown_resolver(self) -> None:
        """session.stop() 调用 — 关 atos daemon + flush cache。

        shutdown 异常被吞,不影响其它 session 收尾工作。_resolver 为 None 时直接返回。
        """
        if self._resolver is not None:
            try:
                self._resolver.shutdown()
            except Exception:
                pass
            self._resolver = None

    def _main_has_timeprofiler(self, meta: Dict[str, Any]) -> bool:
        from .analyze.callstack import main_has_timeprofiler
        return main_has_timeprofiler(meta)

    # ── 调用栈分析 (委派到 analyze/callstack) ──

    def callstack(self, **kwargs) -> Dict[str, Any]:
        """解析 Time Profiler 调用栈 — 委派到 analyze/callstack.analyze_callstack。"""
        from .analyze.callstack import analyze_callstack
        meta = self._load_meta()
        return analyze_callstack(
            meta,
            self.exports_dir,
            traces_dir=self.traces_dir,
            **kwargs,
        )

    def format_callstack_text(self, data: Dict[str, Any], max_depth: int = 8) -> str:
        from .analyze.callstack import format_callstack_text
        return format_callstack_text(data, max_depth=max_depth)

    # ── DvtBridge 指标分析 (delegate to present/dvt_metrics) ──

    def _dvt_metrics_report(self, meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        from .present.dvt_metrics import build_dvt_metrics_report
        return build_dvt_metrics_report(meta)

    def format_dvt_metrics_text(self, dvt_data: Dict[str, Any]) -> str:
        from .present.dvt_metrics import format_dvt_metrics_text
        return format_dvt_metrics_text(dvt_data)
