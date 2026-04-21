"""
cpar perf 子包 — 真机性能采集、实时分析、报告生成。

模块:
- config:             PerfConfig 数据类
- session:            PerfSessionManager 生命周期管理
- live_log:           LiveLogAnalyzer 实时 syslog 流式分析
- live_metrics:       LiveMetricsStreamer 实时 xctrace 指标流
- templates:          TemplateLibrary Instruments 模板注册与扩展
- integrator:         与 Orchestrator 的深度集成胶水
- symbolicate:        dSYM 符号化 (atos / dsymutil / Swift demangle)
- time_sync:          syslog-xctrace 时序对齐与事件归因
- power_attribution:  进程级功耗归因与异常检测
"""

from .config import PerfConfig
from .protocol.reconnect import ReconnectableMixin, ReconnectPolicy
from .session import PerfSessionManager
from .live_log import LiveLogAnalyzer, LogRule, DEFAULT_RULES
from .live_metrics import (
    LiveMetricsStreamer,
    MetricSnapshot,
    MetricThreshold,
    DEFAULT_THRESHOLDS,
    build_snapshot_from_exports,
)
from .templates import (
    InstrumentTemplate,
    TemplateLibrary,
    BUILTIN_TEMPLATES,
    COMPOSITE_PRESETS,
    build_xctrace_record_cmd,
    build_composite_record_cmd,
    resolve_composite,
    list_available_devices,
    list_available_templates,
)
from .protocol.device import (
    BatteryPoller,
    ProcessMetricsStreamer,
    read_battery_jsonl,
    read_process_metrics_jsonl,
    format_battery_text,
    format_process_metrics_text,
)
from .protocol.dvt import (
    DvtBridgeThread,
    DvtBridgeSession,
    DvtProcessSnapshot,
    DvtSystemSnapshot,
    DvtNetworkEvent,
    DvtGraphicsSnapshot,
    check_dvt_available,
    read_dvt_process_jsonl,
    read_dvt_system_jsonl,
    format_dvt_process_text,
    dvt_bridge_main,
)
from .sampling import (
    SamplingProfilerSidecar,
    HotspotSnapshot,
    export_xctrace_schema,
    parse_timeprofiler_xml,
    aggregate_top_n,
    read_hotspots_jsonl,
    format_hotspots_text,
)
from .webcontent import (
    WebContentProfiler,
    find_webcontent_pids,
    read_webcontent_hotspots,
    format_webcontent_hotspots,
)
from .deep_export import (
    DEEP_SCHEMAS,
    export_deep_schema,
    parse_gpu_frame_time,
    parse_network_stat,
    parse_vm_tracking,
    parse_metal_performance,
    deep_export_all,
    format_deep_report,
    probe_trace_schemas,
)
from .symbolicate import (
    find_dsym,
    find_dsym_by_uuid,
    find_dsym_in_archives,
    find_dsym_app_store_connect,
    symbolicate_addresses,
    swift_demangle,
    symbolicate_hotspots,
    cache_dsym_map,
    auto_symbolicate,
    extract_binary_uuid,
    extract_app_uuid_from_device,
)
from .time_sync import (
    SyslogEvent,
    CorrelatedEvent,
    get_device_uptime,
    parse_syslog_timestamps,
    parse_xctrace_timeline,
    align_timelines,
    correlate_events,
    format_event_report,
    run_time_sync,
)
from .integrator import PerfIntegrator
from .power_attribution import (
    ProcessPower,
    ProcessLifecycleEvent,
    AnomalyEvent,
    parse_system_power,
    parse_process_cpu,
    attribute_power,
    discover_process_tree,
    track_process_lifecycle,
    detect_anomalies,
    format_attribution_report,
    read_lifecycle_events,
)
from .ai_diagnosis import (
    DiagnosisContext,
    DiagnosisResult,
    collect_diagnosis_context,
    build_diagnosis_prompt,
    call_llm,
    parse_diagnosis_response,
    generate_regression_analysis,
    generate_webkit_report,
    format_diagnosis_report,
    run_diagnosis,
)
from .report_html import generate_html_report
from .perf_defaults import PerfDefaults

__all__ = [
    "PerfConfig",
    "ReconnectableMixin",
    "ReconnectPolicy",
    "PerfSessionManager",
    "LiveLogAnalyzer",
    "LogRule",
    "DEFAULT_RULES",
    "LiveMetricsStreamer",
    "MetricSnapshot",
    "MetricThreshold",
    "DEFAULT_THRESHOLDS",
    "build_snapshot_from_exports",
    "InstrumentTemplate",
    "TemplateLibrary",
    "BUILTIN_TEMPLATES",
    "COMPOSITE_PRESETS",
    "build_xctrace_record_cmd",
    "build_composite_record_cmd",
    "resolve_composite",
    "list_available_devices",
    "list_available_templates",
    "PerfIntegrator",
    "SamplingProfilerSidecar",
    "HotspotSnapshot",
    "BatteryPoller",
    "ProcessMetricsStreamer",
    "read_battery_jsonl",
    "read_process_metrics_jsonl",
    "format_battery_text",
    "format_process_metrics_text",
    # dvt_bridge
    "DvtBridgeThread",
    "DvtBridgeSession",
    "DvtProcessSnapshot",
    "DvtSystemSnapshot",
    "DvtNetworkEvent",
    "DvtGraphicsSnapshot",
    "check_dvt_available",
    "read_dvt_process_jsonl",
    "read_dvt_system_jsonl",
    "format_dvt_process_text",
    "dvt_bridge_main",
    "export_xctrace_schema",
    "parse_timeprofiler_xml",
    "aggregate_top_n",
    "read_hotspots_jsonl",
    "format_hotspots_text",
    "find_dsym",
    "find_dsym_by_uuid",
    "find_dsym_in_archives",
    "find_dsym_app_store_connect",
    "symbolicate_addresses",
    "swift_demangle",
    "symbolicate_hotspots",
    "cache_dsym_map",
    "auto_symbolicate",
    "extract_binary_uuid",
    "extract_app_uuid_from_device",
    # deep_export
    "DEEP_SCHEMAS",
    "export_deep_schema",
    "parse_gpu_frame_time",
    "parse_network_stat",
    "parse_vm_tracking",
    "parse_metal_performance",
    "deep_export_all",
    "format_deep_report",
    "probe_trace_schemas",
    # time_sync
    "SyslogEvent",
    "CorrelatedEvent",
    "get_device_uptime",
    "parse_syslog_timestamps",
    "parse_xctrace_timeline",
    "align_timelines",
    "correlate_events",
    "format_event_report",
    "run_time_sync",
    # power_attribution
    "ProcessPower",
    "ProcessLifecycleEvent",
    "AnomalyEvent",
    "parse_system_power",
    "parse_process_cpu",
    "attribute_power",
    "discover_process_tree",
    "track_process_lifecycle",
    "detect_anomalies",
    "format_attribution_report",
    "read_lifecycle_events",
    # ai_diagnosis
    "DiagnosisContext",
    "DiagnosisResult",
    "collect_diagnosis_context",
    "build_diagnosis_prompt",
    "call_llm",
    "parse_diagnosis_response",
    "generate_regression_analysis",
    "generate_webkit_report",
    "format_diagnosis_report",
    "run_diagnosis",
    # report_html
    "generate_html_report",
    # perf_defaults
    "PerfDefaults",
]
