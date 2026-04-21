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
import re
import signal
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from .config import PerfConfig
from .device_metrics import BatteryPoller, ProcessMetricsStreamer
from .webcontent import WebContentProfiler
from .dvt_bridge import DvtBridgeThread, check_dvt_available
from .sampling import (
    SamplingProfilerSidecar,
    export_xctrace_schema,
    extract_mnemonic_value,
    parse_timeprofiler_xml,
)
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

    # ---------- lifecycle ----------
    def start(self) -> Dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.traces_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)

        meta = self._load_meta()
        if meta.get("status") == "running":
            return meta

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

    # ---------- internals ----------
    def _trace_metrics(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        from concurrent.futures import ThreadPoolExecutor

        trace_str = meta.get("xctrace", {}).get("trace", "")
        if not trace_str:
            return {"source": "none", "display_avg": None, "cpu_avg": None, "networking_avg": None}
        trace_file = Path(trace_str)
        if not trace_file.exists():
            return {"source": "none", "display_avg": None, "cpu_avg": None, "networking_avg": None}

        power_xml = self.exports_dir / "SystemPowerLevel.xml"
        proc_xml = self.exports_dir / "ProcessSubsystemPowerImpact.xml"

        # 并行导出两个 schema
        with ThreadPoolExecutor(max_workers=2) as pool:
            pool.submit(self._export_schema, trace_file, "SystemPowerLevel", power_xml)
            pool.submit(self._export_schema, trace_file, "ProcessSubsystemPowerImpact", proc_xml)

        display_vals = self._extract_column_values(power_xml, "Display")
        cpu_vals = self._extract_column_values(proc_xml, "CPU")
        net_vals = self._extract_column_values(proc_xml, "Networking")

        return {
            "source": str(trace_file),
            "display_avg": self._avg(display_vals),
            "cpu_avg": self._avg(cpu_vals),
            "networking_avg": self._avg(net_vals),
            "display_samples": len(display_vals),
            "cpu_samples": len(cpu_vals),
            "networking_samples": len(net_vals),
        }

    def _export_schema(self, trace_file: Path, schema: str, output: Path):
        try:
            export_xctrace_schema(trace_file, schema, output)
        except Exception:
            pass

    def _extract_column_values(self, xml_file: Path, column_name: str) -> list:
        if not xml_file.exists():
            return []
        try:
            text = xml_file.read_text(errors="replace")
        except Exception:
            return []

        import re
        columns = []
        for m in re.finditer(r'<col[^>]*name="([^"]+)"', text):
            columns.append(m.group(1))
        if not columns:
            return []
        idx = None
        for i, name in enumerate(columns):
            if name.lower() == column_name.lower():
                idx = i
                break
        if idx is None:
            return []

        vals = []
        row_pat = re.compile(r"<row>(.*?)</row>", re.S)
        cell_pat = re.compile(r"<c[^>]*>(.*?)</c>", re.S)
        for row_m in row_pat.finditer(text):
            row = row_m.group(1)
            cells = [c.strip() for c in cell_pat.findall(row)]
            if idx < len(cells):
                try:
                    vals.append(float(cells[idx]))
                except Exception:
                    continue
        return vals

    def _calc_delta(self, base: Dict[str, Any], cur: Dict[str, Any]) -> Dict[str, Any]:
        def pct(a, b):
            if a is None or b is None or a == 0:
                return None
            return (b - a) / a * 100.0
        return {
            "display_avg_pct": pct(base.get("display_avg"), cur.get("display_avg")),
            "cpu_avg_pct": pct(base.get("cpu_avg"), cur.get("cpu_avg")),
            "networking_avg_pct": pct(base.get("networking_avg"), cur.get("networking_avg")),
        }

    def _gate_check(self, delta: Dict[str, Any], threshold_pct: float) -> Dict[str, Any]:
        reasons = []
        for key in ("display_avg_pct", "cpu_avg_pct", "networking_avg_pct"):
            v = delta.get(key)
            if v is not None and v > threshold_pct:
                reasons.append(f"{key}={v:.1f}% > {threshold_pct:.1f}%")
        return {
            "checked": True,
            "passed": len(reasons) == 0,
            "reason": "; ".join(reasons) if reasons else "ok",
        }

    def _timeline_stats(self) -> Dict[str, Any]:
        if not self.timeline_file.exists():
            return {"events": 0, "levels": []}
        try:
            payload = json.loads(self.timeline_file.read_text())
            events = payload.get("events", [])
        except Exception:
            return {"events": 0, "levels": []}

        level_ranges = {}
        for e in events:
            idx = e.get("level_idx")
            name = e.get("event", "")
            ts = e.get("ts", 0)
            if idx is None:
                continue
            level_ranges.setdefault(idx, {"start": None, "end": None, "tasks": []})
            if "level_start" in name:
                level_ranges[idx]["start"] = ts
                level_ranges[idx]["tasks"] = e.get("tasks", [])
            elif "level_end" in name:
                level_ranges[idx]["end"] = ts

        levels = []
        for idx in sorted(level_ranges.keys()):
            r = level_ranges[idx]
            dur = None
            if r["start"] and r["end"] and r["end"] >= r["start"]:
                dur = round(r["end"] - r["start"], 2)
            levels.append({
                "level_idx": idx,
                "duration_sec": dur,
                "tasks": r["tasks"],
            })
        return {"events": len(events), "levels": levels}

    def _syslog_stats(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        log_str = meta.get("syslog", {}).get("log", "")
        if not log_str:
            return {"source": "none", "reliable": False, "lines": 0}
        log_file = Path(log_str)
        if not log_file.exists():
            return {"source": "none", "reliable": False, "lines": 0}
        lines = log_file.read_text(errors="replace").splitlines()
        return {
            "source": str(log_file),
            "lines": len(lines),
            "reliable": bool(meta.get("syslog", {}).get("reliable", False)),
        }

    def _check_syslog_reliability(self, meta: Dict[str, Any]):
        log_file = Path(meta.get("syslog", {}).get("log", ""))
        reliable = False
        if log_file.exists():
            size = log_file.stat().st_size
            if size > 128:
                txt = log_file.read_text(errors="replace")
                if "[connected:" in txt and len(txt.strip().splitlines()) <= 2:
                    reliable = False
                else:
                    reliable = True
        meta["syslog"]["reliable"] = reliable
        self._save_meta(meta)

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

    def _avg(self, arr: list) -> Optional[float]:
        if not arr:
            return None
        return round(sum(arr) / len(arr), 4)

    def _main_has_timeprofiler(self, meta: Dict[str, Any]) -> bool:
        """检查主链路 xctrace 是否已包含 Time Profiler 模板。"""
        tpl = meta.get("xctrace", {}).get("template", "")
        if tpl.lower() in ("time", "time profiler"):
            return True
        for entry in meta.get("xctrace_multi", []):
            if entry.get("template", "").lower() in ("time", "time profiler"):
                return True
        return False

    # ── 调用栈分析 (Time Profiler) ──

    def callstack(
        self,
        top_n: int = 20,
        min_weight: float = 0.5,
        flatten: bool = True,
        full_stack: bool = False,
        time_from: float = 0,
        time_to: float = 0,
    ) -> Dict[str, Any]:
        """
        解析 Time Profiler 调用栈，返回热点函数排名。

        Args:
            top_n:       返回前 N 个热点
            min_weight:  最小权重百分比 (低于此值忽略)
            flatten:     True=按函数聚合(火焰图风格), False=保留完整调用路径
            full_stack:  True=保留完整调用链, False=只取叶子函数
            time_from:   时间切片起点（秒，0=不限）
            time_to:     时间切片终点（秒，0=不限）

        Returns:
            dict 包含 hot_functions / call_paths / summary
        """
        meta = self._load_meta()
        if meta.get("status") not in ("stopped", "running"):
            return {"error": "没有已完成的 perf 采集会话", "hot_functions": [], "call_paths": []}

        # 查找 TimeProfiler trace 文件
        trace_files = self._find_timeprofiler_traces(meta)
        if not trace_files:
            return {"error": "未找到 Time Profiler trace (录制时需要 --templates time)", "hot_functions": [], "call_paths": []}

        # 并行导出所有 trace 文件的 Time Profiler schema
        from concurrent.futures import ThreadPoolExecutor

        t_range = None
        if time_from > 0 or time_to > 0:
            t_range = (time_from, time_to if time_to > 0 else float("inf"))

        def _export_and_parse(trace_file: Path) -> list:
            xml_path = self.exports_dir / f"time_profile_{trace_file.stem}.xml"
            self._export_schema(trace_file, "time-profile", xml_path)
            if not xml_path.exists() or xml_path.stat().st_size < 100:
                xml_path = self.exports_dir / f"TimeProfiler_{trace_file.stem}.xml"
                self._export_schema(trace_file, "TimeProfiler", xml_path)
            if not xml_path.exists():
                return []
            return parse_timeprofiler_xml(
                xml_path,
                keep_full_stack=full_stack,
                time_range=t_range,
            )

        all_samples = []
        with ThreadPoolExecutor(max_workers=min(len(trace_files), 4)) as pool:
            futures = [pool.submit(_export_and_parse, tf) for tf in trace_files]
            for f in futures:
                try:
                    all_samples.extend(f.result())
                except Exception:
                    pass

        if not all_samples:
            return {"error": "TimeProfiler XML 中无采样数据", "hot_functions": [], "call_paths": [], "total_samples": 0}

        total_samples = len(all_samples)

        if flatten:
            # 按函数聚合权重 (火焰图风格)
            func_weight = defaultdict(float)
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

        # 提取 Top N 完整调用路径 (最深栈帧 → 最浅)
        path_weight = defaultdict(float)
        for frame, weight in all_samples:
            path_weight[frame] += weight
        top_paths = sorted(path_weight.items(), key=lambda x: x[1], reverse=True)[:top_n]
        call_paths = []
        for path, w in top_paths:
            pct = w / total_samples * 100.0
            if pct < min_weight:
                break
            # 按调用层级拆分
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

    def format_callstack_text(self, data: Dict[str, Any], max_depth: int = 8) -> str:
        """将调用栈分析结果格式化为可读文本"""
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
                lines.append(f"  {i+1:2d}. {sym:<{max_sym_len}}  {h['weight_pct']:5.1f}%  ({h['samples']} samples)  {bar}")
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
                # 从底层(leaf)向上显示调用链
                display_frames = frames[:max_depth]
                for j, frame in enumerate(display_frames):
                    indent = "      " + "  " * j
                    arrow = "→ " if j > 0 else "  "
                    lines.append(f"{indent}{arrow}{frame}")
                if len(frames) > max_depth:
                    lines.append(f"      ... ({len(frames) - max_depth} more frames)")
                lines.append("")

        return "\n".join(lines)

    # ── callstack 内部辅助 ──

    def _find_timeprofiler_traces(self, meta: Dict[str, Any]) -> List[Path]:
        """查找含 Time Profiler 数据的 trace 文件（包括 systemtrace）。"""
        traces = []
        _TIME_TEMPLATES = ("time", "time profiler", "systrace", "systemtrace", "system trace")

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
        if not traces and self.traces_dir.exists():
            for pattern in ("*time*.trace", "*systrace*.trace"):
                for f in self.traces_dir.glob(pattern):
                    if f not in traces:
                        traces.append(f)

        return traces

    def _parse_timeprofiler_xml(self, xml_path: Path) -> List[Tuple[str, float]]:
        return parse_timeprofiler_xml(xml_path)

    def _extract_mnemonic_value(self, row_text: str, mnemonic: str, default: str = "") -> str:
        return extract_mnemonic_value(row_text, mnemonic, default)

    # ── DvtBridge 指标分析 ──

    def _dvt_metrics_report(self, meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """分析 DvtBridge 采集的进程/系统指标 JSONL，返回统计摘要。"""
        dm = meta.get("device_metrics", {})
        if dm.get("source") != "dvt_bridge":
            return None

        from .dvt_bridge import read_dvt_process_jsonl, read_dvt_system_jsonl

        process_jsonl = Path(dm.get("process_jsonl", ""))
        system_jsonl = Path(dm.get("system_jsonl", ""))

        result: Dict[str, Any] = {"source": "dvt_bridge"}

        # 进程指标统计
        proc_records = read_dvt_process_jsonl(process_jsonl) if process_jsonl.exists() else []
        if proc_records:
            # 按进程名分组
            by_name: Dict[str, List[Dict[str, Any]]] = {}
            for r in proc_records:
                name = r.get("name", "unknown")
                by_name.setdefault(name, []).append(r)

            proc_stats = {}
            for name, records in by_name.items():
                cpu_vals = [r["cpuUsage"] for r in records if isinstance(r.get("cpuUsage"), (int, float))]
                mem_vals = [r["physFootprintMB"] for r in records if isinstance(r.get("physFootprintMB"), (int, float))]
                thread_vals = [r["threadCount"] for r in records if isinstance(r.get("threadCount"), (int, float))]

                proc_stats[name] = {
                    "samples": len(records),
                    "pid": records[-1].get("pid", "?"),
                    "cpu_pct": {
                        "avg": round(sum(cpu_vals) / len(cpu_vals), 2) if cpu_vals else None,
                        "peak": round(max(cpu_vals), 2) if cpu_vals else None,
                        "min": round(min(cpu_vals), 2) if cpu_vals else None,
                    },
                    "mem_mb": {
                        "avg": round(sum(mem_vals) / len(mem_vals), 1) if mem_vals else None,
                        "peak": round(max(mem_vals), 1) if mem_vals else None,
                        "min": round(min(mem_vals), 1) if mem_vals else None,
                    },
                    "threads": {
                        "avg": round(sum(thread_vals) / len(thread_vals), 1) if thread_vals else None,
                        "peak": max(thread_vals) if thread_vals else None,
                    },
                }

            result["process_stats"] = proc_stats
            result["total_process_samples"] = len(proc_records)

        # 系统指标统计
        sys_records = read_dvt_system_jsonl(system_jsonl) if system_jsonl.exists() else []
        if sys_records:
            cpu_total = [r["cpuTotal"] for r in sys_records if isinstance(r.get("cpuTotal"), (int, float))]
            mem_free = [r["physMemoryFreeMB"] for r in sys_records if isinstance(r.get("physMemoryFreeMB"), (int, float))]
            mem_used = [r["physMemoryUsedMB"] for r in sys_records if isinstance(r.get("physMemoryUsedMB"), (int, float))]

            result["system_stats"] = {
                "samples": len(sys_records),
                "cpu_total_pct": {
                    "avg": round(sum(cpu_total) / len(cpu_total), 2) if cpu_total else None,
                    "peak": round(max(cpu_total), 2) if cpu_total else None,
                },
                "mem_free_mb": {
                    "avg": round(sum(mem_free) / len(mem_free), 1) if mem_free else None,
                    "min": round(min(mem_free), 1) if mem_free else None,
                },
                "mem_used_mb": {
                    "avg": round(sum(mem_used) / len(mem_used), 1) if mem_used else None,
                    "peak": round(max(mem_used), 1) if mem_used else None,
                },
            }

        return result if result.get("process_stats") or result.get("system_stats") else None

    def format_dvt_metrics_text(self, dvt_data: Dict[str, Any]) -> str:
        """将 DvtBridge 指标分析结果格式化为可读文本。"""
        if not dvt_data:
            return "  (无 DvtBridge 数据)"

        lines = []
        lines.append("  ── DvtBridge 实时指标 ──")
        lines.append("")

        # 进程统计
        proc_stats = dvt_data.get("process_stats", {})
        for name, stats in sorted(proc_stats.items(), key=lambda x: -(x[1].get("cpu_pct", {}).get("avg") or 0)):
            cpu = stats.get("cpu_pct", {})
            mem = stats.get("mem_mb", {})
            threads = stats.get("threads", {})
            samples = stats.get("samples", 0)

            cpu_avg = cpu.get("avg", "?")
            cpu_peak = cpu.get("peak", "?")
            mem_avg = mem.get("avg", "?")
            mem_peak = mem.get("peak", "?")

            cpu_avg_str = f"{cpu_avg:.1f}%" if isinstance(cpu_avg, (int, float)) else "?"
            cpu_peak_str = f"{cpu_peak:.1f}%" if isinstance(cpu_peak, (int, float)) else "?"
            mem_avg_str = f"{mem_avg:.0f}MB" if isinstance(mem_avg, (int, float)) else "?"
            mem_peak_str = f"{mem_peak:.0f}MB" if isinstance(mem_peak, (int, float)) else "?"

            lines.append(
                f"  {name}(pid={stats.get('pid', '?')})  "
                f"CPU: avg={cpu_avg_str} peak={cpu_peak_str}  "
                f"MEM: avg={mem_avg_str} peak={mem_peak_str}  "
                f"({samples} samples)"
            )

        # 系统统计
        sys_stats = dvt_data.get("system_stats", {})
        if sys_stats:
            lines.append("")
            cpu_total = sys_stats.get("cpu_total_pct", {})
            mem_free = sys_stats.get("mem_free_mb", {})
            cpu_avg = cpu_total.get("avg")
            mem_min = mem_free.get("min")
            if cpu_avg is not None:
                lines.append(f"  系统CPU: avg={cpu_avg:.1f}%")
            if mem_min is not None:
                lines.append(f"  可用内存: min={mem_min:.0f}MB")

        return "\n".join(lines)
