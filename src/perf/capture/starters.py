"""
PerfSessionManager.start() 子步骤 — 拆解 7 个子系统启动逻辑.

每个函数接收 session (PerfSessionManager) + meta dict,原地修改 meta (以及可能
设置 session 上的字段如 self.battery_poller)。全部捕获异常并写入 meta["errors"],
失败不影响其它子系统启动。
"""

import subprocess
from pathlib import Path
from typing import Any, Dict

from ..protocol.device import BatteryPoller, ProcessMetricsStreamer
from ..protocol.dvt import DvtBridgeThread, check_dvt_available
from .sampling import SamplingProfilerSidecar
from .webcontent import WebContentProfiler


def start_syslog(session, meta: Dict[str, Any]) -> None:
    if not session.config.device:
        return
    try:
        syslog_log = Path(meta["syslog"]["log"])
        cmd = ["idevicesyslog", "-u", session.config.device]
        f = open(syslog_log, "a", encoding="utf-8")
        proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT)
        # Popen 接管 fd 后立即关闭 Python 侧句柄，防止泄漏
        f.close()
        meta["syslog"]["enabled"] = True
        meta["syslog"]["pid"] = proc.pid
    except Exception as e:
        meta["errors"].append(f"syslog_start_failed: {e}")


def start_battery(session, meta: Dict[str, Any]) -> None:
    """BatteryPoller 始终启动,不占任何 slot."""
    if not (session.config.device and session.config.battery_interval_sec > 0):
        return
    battery_jsonl = session.logs_dir / "battery.jsonl"
    try:
        session.battery_poller = BatteryPoller(
            device_udid=session.config.device,
            interval_sec=session.config.battery_interval_sec,
            output_file=battery_jsonl,
        )
        bp_pid = session.battery_poller.start()
        meta["battery"] = {
            "enabled": True,
            "pid": bp_pid,
            "jsonl": str(battery_jsonl),
        }
    except Exception as e:
        meta["errors"].append(f"battery_poller_failed: {e}")


def start_dvt(session, meta: Dict[str, Any]) -> None:
    """DvtBridge 主路径 + ProcessMetricsStreamer CLI 子进程 fallback.

    把 `use_device_metrics` 决策结果挂到 session 上,供后续 start_xctrace 读取
    (xctrace 主通道与 device 模式互斥)。
    """
    # ── 指标采集源决策 ──
    use_device_metrics = False
    if session.config.device:
        src = session.config.metrics_source
        if src == "device":
            use_device_metrics = True
        elif src == "auto" and session.config.sampling_enabled:
            # auto + sampling → 优先 device 路径以避免 xctrace 互斥
            use_device_metrics = True
    session._use_device_metrics = use_device_metrics

    if not use_device_metrics or not session.config.attach:
        return

    # 优先使用 DvtBridge (asyncio RPC 直连，更高精度、更低开销)
    # 如果 DvtBridge 不可用或启动失败，fallback 到 ProcessMetricsStreamer (CLI 子进程)
    dvt_output_dir = session.logs_dir / "dvt"
    dvt_output_dir.mkdir(parents=True, exist_ok=True)

    dvt_started = False
    dvt_check = check_dvt_available()

    if dvt_check.get("pymobiledevice3"):
        try:
            # 确定进程名列表 (attach + 可选的 webcontent 进程)
            proc_names = [session.config.attach]
            if session.config.attach_webcontent:
                proc_names.append("WebContent")
                proc_names.append("WebKitWebContent")

            session.dvt_bridge_thread = DvtBridgeThread(
                device_udid=session.config.device,
                process_names=proc_names,
                interval_ms=session.config.metrics_interval_ms,
                output_dir=dvt_output_dir,
                cpu_threshold=session.config.dvt_cpu_threshold if hasattr(session.config, 'dvt_cpu_threshold') else 80.0,
                memory_threshold_mb=session.config.dvt_memory_threshold_mb if hasattr(session.config, 'dvt_memory_threshold_mb') else 1500.0,
                collect_network=session.config.collect_network if hasattr(session.config, 'collect_network') else True,
                collect_graphics=session.config.collect_graphics if hasattr(session.config, 'collect_graphics') else True,
            )
            dvt_result = session.dvt_bridge_thread.start()
            dvt_started = dvt_result.get("status") in ("started", "starting")

            if dvt_started:
                meta["device_metrics"] = {
                    "enabled": True,
                    "source": "dvt_bridge",
                    "process_names": proc_names,
                    "interval_ms": session.config.metrics_interval_ms,
                    "process_jsonl": str(dvt_output_dir / "dvt_process.jsonl"),
                    "system_jsonl": str(dvt_output_dir / "dvt_system.jsonl"),
                    "network_jsonl": str(dvt_output_dir / "dvt_network.jsonl"),
                    "graphics_jsonl": str(dvt_output_dir / "dvt_graphics.jsonl"),
                }
                meta.setdefault("dvt_bridge", {
                    "status": "running",
                    "device": session.config.device,
                    "interval_ms": session.config.metrics_interval_ms,
                    "tunneld": dvt_check.get("tunneld", False),
                })
            else:
                meta["errors"].append(
                    f"dvt_bridge_start_failed: status={dvt_result.get('status', 'unknown')}"
                )
        except Exception as e:
            meta["errors"].append(f"dvt_bridge_start_failed: {e}")

    if dvt_started:
        return

    # Fallback: ProcessMetricsStreamer (pymobiledevice3 CLI 子进程)
    proc_jsonl = session.logs_dir / "process_metrics.jsonl"
    try:
        session.process_streamer = ProcessMetricsStreamer(
            device_udid=session.config.device,
            process_name=session.config.attach,
            interval_ms=session.config.metrics_interval_ms,
            output_file=proc_jsonl,
        )
        pm_pid = session.process_streamer.start()
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


def start_xctrace(session, meta: Dict[str, Any]) -> None:
    """xctrace record 主通道 — composite / single / multi 分支."""
    if not (session.config.device and session.config.attach):
        return
    if getattr(session, "_use_device_metrics", False):
        return  # 走 device metrics 路径,跳过 xctrace

    from ..decode.templates import (
        TemplateLibrary, build_xctrace_record_cmd,
        build_composite_record_cmd, resolve_composite,
    )
    tpl_lib = TemplateLibrary()
    tpls = tpl_lib.resolve_multi(session.config.templates)

    if not tpls:
        # fallback: 如果 resolve 失败，用原始 template 字符串直接传
        tpls = []
        meta["xctrace"]["template_raw"] = session.config.templates

    # ── 决策: 是否使用 composite 模式 ──
    use_composite = False
    composite_info = None

    composite_cfg = getattr(session.config, "composite", "auto")
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
        trace_path = session.traces_dir / f"{session.config.tag}_composite.trace"
        stderr_path = Path(meta["xctrace"]["stderr"])
        cmd = build_composite_record_cmd(
            base_template=base_tpl,
            instruments=instruments,
            device=session.config.device,
            attach=session.config.attach,
            duration_sec=session.config.duration_sec,
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
        trace_path = session.traces_dir / tpl.trace_filename(session.config.tag)
        stderr_path = Path(meta["xctrace"]["stderr"])
        cmd = build_xctrace_record_cmd(
            template=tpl,
            device=session.config.device,
            attach=session.config.attach,
            duration_sec=session.config.duration_sec,
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
            trace_path = session.traces_dir / tpl.trace_filename(session.config.tag)
            stderr_path = session.logs_dir / f"xctrace_{tpl.alias or tpl.name}.stderr.log"
            cmd = build_xctrace_record_cmd(
                template=tpl,
                device=session.config.device,
                attach=session.config.attach,
                duration_sec=session.config.duration_sec,
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


def start_sampling(session, meta: Dict[str, Any]) -> None:
    """Sampling Profiler 旁路 — 与 xctrace 主链路互斥."""
    if not (session.config.sampling_enabled and session.config.device and session.config.attach):
        return

    main_has_xctrace = meta.get("xctrace", {}).get("enabled", False) or bool(meta.get("xctrace_multi"))

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
        return

    try:
        session.sampling_sidecar = SamplingProfilerSidecar(
            session_root=session.root,
            device_udid=session.config.device,
            process=session.config.attach,
            interval_sec=session.config.sampling_interval_sec,
            top_n=session.config.sampling_top_n,
            retention=session.config.sampling_retention,
            resolver=session._resolver,
        )
        started = session.sampling_sidecar.start(as_subprocess=True)
        meta["sampling"] = {
            "enabled": started,
            "pid": session.sampling_sidecar._daemon_pid,
            "interval_sec": session.config.sampling_interval_sec,
            "top_n": session.config.sampling_top_n,
            "retention": session.config.sampling_retention,
            "hotspots_file": str(session.sampling_sidecar.hotspots_file),
        }
    except Exception as e:
        meta.setdefault("errors", []).append(f"sampling_start_failed: {e}")
        meta["sampling"] = {"enabled": False, "reason": str(e)}


def start_webcontent(session, meta: Dict[str, Any]) -> None:
    """WebContent 采集 — 不占额外 xctrace slot."""
    if not (session.config.attach_webcontent and session.config.device):
        return
    main_has_xctrace = meta.get("xctrace", {}).get("enabled", False) or bool(meta.get("xctrace_multi"))
    sampling_on = meta.get("sampling", {}).get("enabled", False)
    if main_has_xctrace or sampling_on:
        meta["webcontent"] = {
            "enabled": False,
            "reason": "xctrace_exclusive — App sampling/xctrace 占用 slot，WebContent 需分轮采集",
        }
        return
    try:
        session.webcontent_profiler = WebContentProfiler(
            session_root=session.root,
            device_udid=session.config.device,
            interval_sec=session.config.sampling_interval_sec,
            top_n=session.config.sampling_top_n,
        )
        wc_result = session.webcontent_profiler.start()
        meta["webcontent"] = wc_result
    except Exception as e:
        meta["webcontent"] = {"enabled": False, "reason": str(e)}
