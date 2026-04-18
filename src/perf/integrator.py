"""
PerfIntegrator — 将 perf 采集深度嵌入 cpar run 生命周期。

职责:
- 在 Orchestrator 的关键节点自动触发 perf 操作
- 管理 LiveLogAnalyzer + LiveMetricsStreamer 的启停
- 将 perf 报告摘要注入 cpar run 的最终输出
- 终端实时输出告警摘要 + 指标趋势
"""

import json
import time
from pathlib import Path
from typing import Optional, Dict, Any

from .config import PerfConfig
from .session import PerfSessionManager
from .live_log import LiveLogAnalyzer, LogRule, DEFAULT_RULES
from .live_metrics import LiveMetricsStreamer, MetricSnapshot, DEFAULT_THRESHOLDS
from .sampling import read_hotspots_jsonl, format_hotspots_text
from .templates import TemplateLibrary


class PerfIntegrator:
    """
    perf 与 Orchestrator 的集成胶水。

    使用方式 (在 Orchestrator 中):
        self.perf_integrator = PerfIntegrator(perf_config, repo, coordination_dir)
        self.perf_integrator.on_run_start()
        ...
        self.perf_integrator.on_level_start(level_idx, task_ids)
        ...
        self.perf_integrator.on_task_done(task_id, success, duration)
        ...
        self.perf_integrator.on_level_end(level_idx, task_ids)
        ...
        self.perf_integrator.on_run_end()
    """

    def __init__(
        self,
        config: PerfConfig,
        repo: str,
        coordination_dir: str = ".claude-parallel",
    ):
        self.config = config
        self.repo = repo
        self.coordination_dir = coordination_dir

        self.session = PerfSessionManager(repo, coordination_dir, config)
        self.live_analyzer: Optional[LiveLogAnalyzer] = None
        self.metrics_streamer: Optional[LiveMetricsStreamer] = None
        self._run_start_ts: float = 0.0
        self._task_timings: Dict[str, Dict[str, Any]] = {}
        self._alert_summary_printed = False

    # ── 生命周期钩子 ──

    def on_run_start(self) -> Dict[str, Any]:
        """
        cpar run 开始时调用。
        启动 perf session + syslog 分析 + 指标流。
        """
        self._run_start_ts = time.time()

        # 解析模板
        tpl_lib = TemplateLibrary()
        tpls = tpl_lib.resolve_multi(self.config.templates)
        tpl_names = [t.alias or t.name for t in tpls] if tpls else [self.config.templates]

        # 启动 perf session (syslog 采集 + xctrace 多模板录制)
        meta = self.session.start()
        self.session.mark_event("run_start", detail="cpar run started")

        print(f"  [perf] 模板: {', '.join(tpl_names)}")

        # 启动实时 syslog 分析器
        if self.config.device:
            alert_log = str(
                Path(self.repo) / self.coordination_dir / "perf" / self.config.tag / "logs" / "alerts.log"
            )

            # 加载自定义规则 (如果有)
            rules = list(DEFAULT_RULES)
            if self.config.live_rules_file:
                custom = LiveLogAnalyzer.load_rules_from_file(self.config.live_rules_file)
                if custom:
                    rules = custom

            self.live_analyzer = LiveLogAnalyzer(
                device=self.config.device,
                rules=rules,
                alert_log_path=alert_log,
                perf_manager=self.session,
                buffer_lines=self.config.live_buffer_lines,
            )
            live_status = self.live_analyzer.start()
            self.session.mark_event(
                "live_analyzer_started",
                detail=f"rules={len(rules)}, pid={live_status.get('pid', 'N/A')}",
            )
            print(f"  [perf] 实时 syslog 分析已启动 ({len(rules)} 条规则)")

        # 启动实时指标流 (如果有 xctrace trace)
        trace_file = meta.get("xctrace", {}).get("trace", "")
        if trace_file:
            exports_dir = str(
                Path(self.repo) / self.coordination_dir / "perf" / self.config.tag / "exports"
            )
            jsonl_path = self.config.stream_jsonl or str(
                Path(self.repo) / self.coordination_dir / "perf" / self.config.tag / "logs" / "metrics.jsonl"
            )

            self.metrics_streamer = LiveMetricsStreamer(
                trace_file=trace_file,
                exports_dir=exports_dir,
                interval_sec=self.config.stream_interval,
                window_size=self.config.stream_window,
                thresholds=list(DEFAULT_THRESHOLDS),
                jsonl_path=jsonl_path,
            )
            stream_status = self.metrics_streamer.start()
            if stream_status.get("status") in ("running", "waiting"):
                self.session.mark_event(
                    "metrics_streamer_started",
                    detail=f"interval={self.config.stream_interval}s, window={self.config.stream_window}",
                )
                print(f"  [perf] 实时指标流已启动 (间隔 {self.config.stream_interval}s)")

        if self.config.device and self.config.attach:
            print(f"  [perf] xctrace 设备={self.config.device}, 附加={self.config.attach}")

        # Sampling sidecar 状态
        sampling = meta.get("sampling", {})
        if sampling.get("enabled"):
            print(
                f"  [perf] Sampling Profiler 旁路已启动 "
                f"(interval={sampling.get('interval_sec', '?')}s)"
            )
        elif sampling.get("reason"):
            print(f"  [perf] Sampling 未启动: {sampling['reason']}")

        # DvtBridge 状态
        dvt_bridge = meta.get("dvt_bridge", {})
        if dvt_bridge.get("status") == "running":
            dm = meta.get("device_metrics", {})
            src = dm.get("source", "?")
            procs = dm.get("process_names", [])
            print(
                f"  [perf] DvtBridge 已启动 (source={src}, "
                f"processes={procs}, tunneld={dvt_bridge.get('tunneld', False)})"
            )
        elif meta.get("device_metrics", {}).get("source") == "cli_subprocess":
            print(f"  [perf] 指标采集: CLI 子进程模式 (DvtBridge 不可用)")

        return meta

    def on_level_start(self, level_idx: int, task_ids: list) -> None:
        """并行层级开始"""
        self.session.mark_event(
            "level_start",
            detail=f"level {level_idx} start",
            level_idx=level_idx,
            tasks=task_ids,
        )
        # 打印实时告警摘要 + 指标快照
        parts = []
        if self.live_analyzer and self.live_analyzer.is_running():
            counts = self.live_analyzer.get_alert_counts_by_level()
            crit = counts.get("critical", 0)
            err = counts.get("error", 0)
            warn = counts.get("warn", 0)
            if crit or err:
                parts.append(f"CRITICAL={crit} ERROR={err} WARN={warn}")

        if self.metrics_streamer and self.metrics_streamer.is_running():
            latest = self.metrics_streamer.get_latest()
            if latest:
                m_parts = []
                if latest.display_mw is not None:
                    m_parts.append(f"Display={latest.display_mw:.0f}mW")
                if latest.cpu_mw is not None:
                    m_parts.append(f"CPU={latest.cpu_mw:.0f}mW")
                if latest.cpu_pct is not None:
                    m_parts.append(f"CPU%={latest.cpu_pct:.0f}%")
                if m_parts:
                    parts.append(" | ".join(m_parts))

        if parts:
            print(f"  [perf] Level {level_idx}: {' | '.join(parts)}")

    def on_task_start(self, task_id: str) -> None:
        """单个任务开始"""
        self._task_timings[task_id] = {"start": time.time()}

    def on_task_done(self, task_id: str, success: bool, duration_s: float = 0) -> None:
        """单个任务完成"""
        self._task_timings[task_id] = {
            "start": self._task_timings.get(task_id, {}).get("start", 0),
            "end": time.time(),
            "duration_s": duration_s,
            "success": success,
        }
        self.session.mark_event(
            "task_done",
            detail=f"task={task_id} success={success} duration={duration_s:.1f}s",
            tasks=[task_id],
        )

    def on_level_end(self, level_idx: int, task_ids: list) -> None:
        """并行层级结束"""
        self.session.mark_event(
            "level_end",
            detail=f"level {level_idx} end",
            level_idx=level_idx,
            tasks=task_ids,
        )

        # 显示最新热点快照
        if self.session.sampling_sidecar and self.session.sampling_sidecar.is_alive():
            hotspots_file = self.session.sampling_sidecar.hotspots_file
            snaps = read_hotspots_jsonl(hotspots_file, last_n=1)
            if snaps:
                text = format_hotspots_text(snaps, top_n=5)
                print(f"  [perf] Level {level_idx} 热点:\n{text}")

    def on_run_end(self) -> Dict[str, Any]:
        """
        cpar run 结束时调用。
        停止所有采集, 生成最终报告。
        """
        # 停止实时指标流
        stream_summary = {}
        if self.metrics_streamer:
            stream_summary = self.metrics_streamer.stop()
            self.session.mark_event(
                "metrics_streamer_stopped",
                detail=f"snapshots={stream_summary.get('snapshots', 0)}, alerts={stream_summary.get('alerts', 0)}",
            )

        # 停止实时 syslog 分析
        live_summary = {}
        if self.live_analyzer:
            live_summary = self.live_analyzer.stop()
            self.session.mark_event(
                "live_analyzer_stopped",
                detail=f"lines={live_summary.get('lines_processed', 0)}, alerts={live_summary.get('total_alerts', 0)}",
            )

        self.session.mark_event("run_end", detail="cpar run finished")

        # 停止 perf session
        self.session.stop()

        # 生成报告
        report = self.session.report()

        # 补充 live analyzer 摘要
        report["live_analysis"] = {
            "status": "completed" if live_summary else "not_used",
            "lines_processed": live_summary.get("lines_processed", 0),
            "total_alerts": live_summary.get("total_alerts", 0),
            "alert_counts": live_summary.get("alert_counts", {}),
            "duration_sec": round(time.time() - self._run_start_ts, 1) if self._run_start_ts else 0,
        }

        # 补充 sampling 摘要
        if self.session.sampling_sidecar:
            hotspots_file = self.session.sampling_sidecar.hotspots_file
            agg = read_hotspots_jsonl(hotspots_file, aggregate=True)
            report["sampling"] = {
                "status": "completed",
                "cycles": self.session.sampling_sidecar._cycle_count,
                "hotspots_file": str(hotspots_file),
                "aggregate": agg[0] if agg else {},
            }

        # 补充 metrics streamer 摘要
        report["metrics_stream"] = {
            "status": "completed" if stream_summary else "not_used",
            "iterations": stream_summary.get("iterations", 0),
            "snapshots": stream_summary.get("snapshots", 0),
            "alerts": stream_summary.get("alerts", 0),
            "stats": stream_summary.get("stats", {}),
            "latest": stream_summary.get("latest"),
        }

        # 打印摘要
        self._print_summary(report)

        return report

    # ── 查询 ──

    def get_live_status(self) -> Dict[str, Any]:
        """获取实时分析状态"""
        result = {"log": "not_enabled", "metrics": "not_enabled"}
        if self.live_analyzer:
            result["log"] = self.live_analyzer.get_summary()
        if self.metrics_streamer:
            result["metrics"] = self.metrics_streamer.get_summary()
        return result

    def get_live_alerts(self, level: str = "", limit: int = 20) -> list:
        """获取实时告警"""
        if self.live_analyzer:
            return self.live_analyzer.get_alerts(level=level, limit=limit)
        return []

    def has_critical_alerts(self) -> bool:
        """是否有 critical 级别告警"""
        if self.live_analyzer and self.live_analyzer.has_critical_alerts():
            return True
        return False

    def get_latest_metrics(self) -> Optional[MetricSnapshot]:
        """获取最新指标快照"""
        if self.metrics_streamer:
            return self.metrics_streamer.get_latest()
        return None

    def get_metrics_stats(self) -> Dict[str, Any]:
        """获取指标滚动窗口统计"""
        if self.metrics_streamer:
            return self.metrics_streamer.get_stats()
        return {}

    def get_perf_report(self) -> Dict[str, Any]:
        """获取 perf 报告"""
        return self.session.report()

    # ── 内部 ──

    def _print_summary(self, report: Dict[str, Any]):
        """打印 perf 摘要到终端"""
        print(f"\n  {'='*50}")
        print(f"  Perf 报告摘要")
        print(f"  {'='*50}")

        # Syslog
        syslog = report.get("syslog", {})
        if syslog.get("lines", 0) > 0:
            print(f"  Syslog: {syslog['lines']} 行, reliable={syslog.get('reliable', False)}")

        # Timeline
        timeline = report.get("timeline", {})
        print(f"  Timeline: {timeline.get('events', 0)} 个事件")
        for lvl in timeline.get("levels", []):
            dur = lvl.get("duration_sec")
            dur_str = f"{dur:.1f}s" if dur else "N/A"
            print(f"    Level {lvl['level_idx']}: {dur_str} ({', '.join(lvl.get('tasks', []))})")

        # Metrics
        metrics = report.get("metrics", {})
        if metrics.get("source") != "none":
            print(f"  功耗指标 (trace):")
            for key in ("display_avg", "cpu_avg", "networking_avg"):
                val = metrics.get(key)
                if val is not None:
                    print(f"    {key}: {val}")

        # Live analysis
        live = report.get("live_analysis", {})
        if live.get("status") == "completed":
            print(f"  Syslog 实时分析: {live.get('lines_processed', 0)} 行, {live.get('total_alerts', 0)} 个告警")
            counts = live.get("alert_counts", {})
            if counts:
                for rule_name, count in sorted(counts.items(), key=lambda x: -x[1]):
                    print(f"    {rule_name}: {count}")

        # Metrics stream
        stream = report.get("metrics_stream", {})
        if stream.get("status") == "completed":
            print(f"  指标流: {stream.get('snapshots', 0)} 次快照, {stream.get('alerts', 0)} 次告警")
            stats = stream.get("stats", {})
            if stats.get("samples", 0) > 0:
                for field_name in ("display_mw", "cpu_mw", "networking_mw", "cpu_pct", "gpu_fps", "mem_mb"):
                    fstats = stats.get(field_name, {})
                    if fstats.get("avg") is not None:
                        jitter = fstats.get("jitter", 0)
                        print(f"    {field_name}: avg={fstats['avg']}, peak={fstats['peak']}, jitter={jitter}")

        # Sampling 热点
        sampling = report.get("sampling", {})
        if sampling.get("status") == "completed":
            print(f"  Sampling: {sampling.get('cycles', 0)} cycles")
            agg = sampling.get("aggregate", {})
            if agg.get("top"):
                text = format_hotspots_text([agg], top_n=10)
                print(text)

        # DvtBridge 实时指标
        dvt_metrics = report.get("dvt_metrics", {})
        if dvt_metrics:
            dvt_text = self.session.format_dvt_metrics_text(dvt_metrics)
            if dvt_text:
                print(dvt_text)

        # Baseline / Gate
        gate = report.get("gate", {})
        if gate.get("checked"):
            status = "PASS" if gate["passed"] else "FAIL"
            print(f"  Gate: {status} — {gate.get('reason', '')}")

        print(f"  {'='*50}\n")
