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
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Dict, Any

from .config import PerfConfig
from .protocol.device import BatteryPoller, ProcessMetricsStreamer
from .webcontent import WebContentProfiler
from .protocol.dvt import DvtBridgeThread, check_dvt_available
from .locate.resolver import SymbolResolver
from .sampling import SamplingProfilerSidecar
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

        meta = self._load_meta()
        if meta.get("status") == "running":
            return meta

        # 启动符号解析器 (后台 warmup,sampling 起来时已可用)
        self._wire_resolver(self.config)

        meta = {
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

        # syslog sidecar
        if self.config.device:
            try:
                syslog_log = Path(meta["syslog"]["log"])
                cmd = ["idevicesyslog", "-u", self.config.device]
                f = open(syslog_log, "a", encoding="utf-8")
                proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT)
                # Popen 接管 fd 后立即关闭 Python 侧句柄，防止泄漏
                f.close()
                meta["syslog"]["enabled"] = True
                meta["syslog"]["pid"] = proc.pid
            except Exception as e:
                meta["errors"].append(f"syslog_start_failed: {e}")

        # ── BatteryPoller (始终启动，不占任何 slot) ──
        if self.config.device and self.config.battery_interval_sec > 0:
            battery_jsonl = self.logs_dir / "battery.jsonl"
            try:
                self.battery_poller = BatteryPoller(
                    device_udid=self.config.device,
                    interval_sec=self.config.battery_interval_sec,
                    output_file=battery_jsonl,
                )
                bp_pid = self.battery_poller.start()
                meta["battery"] = {
                    "enabled": True,
                    "pid": bp_pid,
                    "jsonl": str(battery_jsonl),
                }
            except Exception as e:
                meta["errors"].append(f"battery_poller_failed: {e}")

        # ── 指标采集源决策 ──
        use_device_metrics = False
        if self.config.device:
            src = self.config.metrics_source
            if src == "device":
                use_device_metrics = True
            elif src == "auto" and self.config.sampling_enabled:
                # auto + sampling → 优先 device 路径以避免 xctrace 互斥
                use_device_metrics = True

        if use_device_metrics:
            # 优先使用 DvtBridge (asyncio RPC 直连，更高精度、更低开销)
            # 如果 DvtBridge 不可用或启动失败，fallback 到 ProcessMetricsStreamer (CLI 子进程)
            if self.config.attach:
                dvt_output_dir = self.logs_dir / "dvt"
                dvt_output_dir.mkdir(parents=True, exist_ok=True)

                dvt_started = False
                dvt_check = check_dvt_available()

                if dvt_check.get("pymobiledevice3"):
                    try:
                        # 确定进程名列表 (attach + 可选的 webcontent 进程)
                        proc_names = [self.config.attach]
                        if self.config.attach_webcontent:
                            proc_names.append("WebContent")
                            proc_names.append("WebKitWebContent")

                        self.dvt_bridge_thread = DvtBridgeThread(
                            device_udid=self.config.device,
                            process_names=proc_names,
                            interval_ms=self.config.metrics_interval_ms,
                            output_dir=dvt_output_dir,
                            cpu_threshold=self.config.dvt_cpu_threshold if hasattr(self.config, 'dvt_cpu_threshold') else 80.0,
                            memory_threshold_mb=self.config.dvt_memory_threshold_mb if hasattr(self.config, 'dvt_memory_threshold_mb') else 1500.0,
                            collect_network=self.config.collect_network if hasattr(self.config, 'collect_network') else True,
                            collect_graphics=self.config.collect_graphics if hasattr(self.config, 'collect_graphics') else True,
                        )
                        dvt_result = self.dvt_bridge_thread.start()
                        dvt_started = dvt_result.get("status") in ("started", "starting")

                        if dvt_started:
                            meta["device_metrics"] = {
                                "enabled": True,
                                "source": "dvt_bridge",
                                "process_names": proc_names,
                                "interval_ms": self.config.metrics_interval_ms,
                                "process_jsonl": str(dvt_output_dir / "dvt_process.jsonl"),
                                "system_jsonl": str(dvt_output_dir / "dvt_system.jsonl"),
                                "network_jsonl": str(dvt_output_dir / "dvt_network.jsonl"),
                                "graphics_jsonl": str(dvt_output_dir / "dvt_graphics.jsonl"),
                            }
                            meta.setdefault("dvt_bridge", {
                                "status": "running",
                                "device": self.config.device,
                                "interval_ms": self.config.metrics_interval_ms,
                                "tunneld": dvt_check.get("tunneld", False),
                            })
                        else:
                            meta["errors"].append(
                                f"dvt_bridge_start_failed: status={dvt_result.get('status', 'unknown')}"
                            )
                    except Exception as e:
                        meta["errors"].append(f"dvt_bridge_start_failed: {e}")

                if not dvt_started:
                    # Fallback: ProcessMetricsStreamer (pymobiledevice3 CLI 子进程)
                    proc_jsonl = self.logs_dir / "process_metrics.jsonl"
                    try:
                        self.process_streamer = ProcessMetricsStreamer(
                            device_udid=self.config.device,
                            process_name=self.config.attach,
                            interval_ms=self.config.metrics_interval_ms,
                            output_file=proc_jsonl,
                        )
                        pm_pid = self.process_streamer.start()
                        meta["device_metrics"] = {
                            "enabled": bool(pm_pid),
                            "source": "cli_subprocess",
                            "process_pid": pm_pid,
                            "process_jsonl": str(proc_jsonl),
                        }
                        if not pm_pid:
                            meta["device_metrics"]["process_note"] = (
                                "tunneld not available; run: "
                                "sudo pymobiledevice3 remote tunneld"
                            )
                    except Exception as e:
                        meta["errors"].append(f"process_streamer_failed: {e}")

        # xctrace sidecar — 根据 templates 配置启动录制 (跳过 device 模式)
        if self.config.device and self.config.attach and not use_device_metrics:
            from .templates import (
                TemplateLibrary, build_xctrace_record_cmd,
                build_composite_record_cmd, resolve_composite, COMPOSITE_PRESETS,
            )
            tpl_lib = TemplateLibrary()
            tpls = tpl_lib.resolve_multi(self.config.templates)

            if not tpls:
                # fallback: 如果 resolve 失败，用原始 template 字符串直接传
                tpls = []
                meta["xctrace"]["template_raw"] = self.config.templates

            # ── 决策: 是否使用 composite 模式 ──
            use_composite = False
            composite_info = None

            composite_cfg = getattr(self.config, "composite", "auto")
            if composite_cfg == "":
                # 显式禁用 composite
                use_composite = False
            elif composite_cfg != "auto":
                # 指定了预置名或自由组合
                composite_info = resolve_composite(composite_cfg, tpl_lib)
                if composite_info:
                    use_composite = True
                else:
                    meta["errors"].append(
                        f"composite_resolve_failed: 无法解析 '{composite_cfg}', 退回多进程模式"
                    )
            elif len(tpls) > 1:
                # auto + 多模板 → 尝试自动 composite
                # 把第一个模板作为 base, 其余作为附加 instrument
                base_tpl = tpls[0]
                extra_instruments = [t.name for t in tpls[1:]]
                all_schemas = []
                for t in tpls:
                    all_schemas.extend(t.schemas)
                composite_info = {
                    "base_template": base_tpl,
                    "instruments": extra_instruments,
                    "schemas": all_schemas,
                    "preset": "",
                }
                use_composite = True

            # ── 执行录制 ──
            if use_composite and composite_info:
                base_tpl = composite_info["base_template"]
                instruments = composite_info["instruments"]
                trace_path = self.traces_dir / f"{self.config.tag}_composite.trace"
                stderr_path = Path(meta["xctrace"]["stderr"])
                cmd = build_composite_record_cmd(
                    base_template=base_tpl,
                    instruments=instruments,
                    device=self.config.device,
                    attach=self.config.attach,
                    duration_sec=self.config.duration_sec,
                    output_path=str(trace_path),
                )
                try:
                    ferr = open(stderr_path, "a", encoding="utf-8")
                    proc = subprocess.Popen(cmd, stdout=ferr, stderr=subprocess.STDOUT)
                    ferr.close()
                    meta["xctrace"]["enabled"] = True
                    meta["xctrace"]["pid"] = proc.pid
                    meta["xctrace"]["trace"] = str(trace_path)
                    meta["xctrace"]["template"] = base_tpl.alias or base_tpl.name
                    meta["xctrace"]["mode"] = "composite"
                    meta["xctrace"]["composite_instruments"] = [base_tpl.name] + instruments
                    meta["xctrace"]["composite_schemas"] = composite_info["schemas"]
                    if composite_info["preset"]:
                        meta["xctrace"]["composite_preset"] = composite_info["preset"]
                except Exception as e:
                    meta["errors"].append(f"xctrace_composite_start_failed: {e}")

            elif len(tpls) == 1:
                # 单模板: 用旧的单一 xctrace 结构
                tpl = tpls[0]
                trace_path = self.traces_dir / tpl.trace_filename(self.config.tag)
                stderr_path = Path(meta["xctrace"]["stderr"])
                cmd = build_xctrace_record_cmd(
                    template=tpl,
                    device=self.config.device,
                    attach=self.config.attach,
                    duration_sec=self.config.duration_sec,
                    output_path=str(trace_path),
                )
                try:
                    ferr = open(stderr_path, "a", encoding="utf-8")
                    proc = subprocess.Popen(cmd, stdout=ferr, stderr=subprocess.STDOUT)
                    ferr.close()
                    meta["xctrace"]["enabled"] = True
                    meta["xctrace"]["pid"] = proc.pid
                    meta["xctrace"]["trace"] = str(trace_path)
                    meta["xctrace"]["template"] = tpl.alias or tpl.name
                except Exception as e:
                    meta["errors"].append(f"xctrace_start_failed: {e}")
            elif len(tpls) > 1:
                # 多模板 + composite 被禁用 → 旧的多进程模式 (iOS 互斥, 仅第一个能成功)
                meta["xctrace_multi"] = []
                for tpl in tpls:
                    trace_path = self.traces_dir / tpl.trace_filename(self.config.tag)
                    stderr_path = self.logs_dir / f"xctrace_{tpl.alias or tpl.name}.stderr.log"
                    cmd = build_xctrace_record_cmd(
                        template=tpl,
                        device=self.config.device,
                        attach=self.config.attach,
                        duration_sec=self.config.duration_sec,
                        output_path=str(trace_path),
                    )
                    entry = {
                        "template": tpl.alias or tpl.name,
                        "enabled": False,
                        "pid": 0,
                        "trace": str(trace_path),
                        "stderr": str(stderr_path),
                    }
                    try:
                        ferr = open(stderr_path, "a", encoding="utf-8")
                        proc = subprocess.Popen(cmd, stdout=ferr, stderr=subprocess.STDOUT)
                        ferr.close()
                        entry["enabled"] = True
                        entry["pid"] = proc.pid
                    except Exception as e:
                        entry["error"] = str(e)
                        meta["errors"].append(f"xctrace_{tpl.alias}_start_failed: {e}")
                    meta["xctrace_multi"].append(entry)

        # Sampling Profiler 旁路
        # iOS 设备同一时刻只允许一个 xctrace 录制，
        # 因此 sampling 与主链路 xctrace 互斥。
        # composite 模式下: 如果已包含 Time Profiler, sampling 自动跳过
        if self.config.sampling_enabled and self.config.device and self.config.attach:
            main_has_xctrace = meta.get("xctrace", {}).get("enabled", False) or bool(meta.get("xctrace_multi"))
            main_has_time = self._main_has_timeprofiler(meta)

            # composite 模式: 检查 instruments 列表里是否有 Time Profiler
            composite_instruments = meta.get("xctrace", {}).get("composite_instruments", [])
            composite_has_time = any(
                "time" in inst.lower() or "profiler" in inst.lower()
                for inst in composite_instruments
            )

            if main_has_xctrace:
                tpl = meta.get("xctrace", {}).get("template", "")
                hint = ""
                if tpl in ("systrace", "systemtrace", "System Trace"):
                    hint = " (systemtrace already includes time-profile data)"
                if composite_has_time:
                    hint = " (composite 模式已包含 Time Profiler)"
                meta.setdefault("errors", []).append(
                    f"sampling_skipped: iOS device allows only one xctrace session{hint}"
                )
                meta["sampling"] = {"enabled": False, "reason": "xctrace_exclusive"}
            else:
                try:
                    self.sampling_sidecar = SamplingProfilerSidecar(
                        session_root=self.root,
                        device_udid=self.config.device,
                        process=self.config.attach,
                        interval_sec=self.config.sampling_interval_sec,
                        top_n=self.config.sampling_top_n,
                        retention=self.config.sampling_retention,
                        resolver=self._resolver,
                    )
                    started = self.sampling_sidecar.start(as_subprocess=True)
                    meta["sampling"] = {
                        "enabled": started,
                        "pid": self.sampling_sidecar._daemon_pid,
                        "interval_sec": self.config.sampling_interval_sec,
                        "top_n": self.config.sampling_top_n,
                        "retention": self.config.sampling_retention,
                        "hotspots_file": str(self.sampling_sidecar.hotspots_file),
                    }
                except Exception as e:
                    meta.setdefault("errors", []).append(f"sampling_start_failed: {e}")
                    meta["sampling"] = {"enabled": False, "reason": str(e)}

        # WebContent 采集（不占额外 xctrace slot — 只在无主 xctrace 时启动）
        if self.config.attach_webcontent and self.config.device:
            main_has_xctrace = meta.get("xctrace", {}).get("enabled", False) or bool(meta.get("xctrace_multi"))
            sampling_on = meta.get("sampling", {}).get("enabled", False)
            if main_has_xctrace or sampling_on:
                meta["webcontent"] = {
                    "enabled": False,
                    "reason": "xctrace_exclusive — App sampling/xctrace 占用 slot，WebContent 需分轮采集",
                }
            else:
                try:
                    self.webcontent_profiler = WebContentProfiler(
                        session_root=self.root,
                        device_udid=self.config.device,
                        interval_sec=self.config.sampling_interval_sec,
                        top_n=self.config.sampling_top_n,
                    )
                    wc_result = self.webcontent_profiler.start()
                    meta["webcontent"] = wc_result
                except Exception as e:
                    meta["webcontent"] = {"enabled": False, "reason": str(e)}

        self._save_meta(meta)
        if not self.timeline_file.exists():
            atomic_write_json(self.timeline_file, {"events": []})
        self.mark_event("perf_session_started", detail="collector booted")
        return meta

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
