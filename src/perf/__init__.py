"""
cpar perf 子包 — 真机性能采集、实时分析、报告生成。

模块:
- config:         PerfConfig 数据类
- session:        PerfSessionManager 生命周期管理
- live_log:       LiveLogAnalyzer 实时 syslog 流式分析
- live_metrics:   LiveMetricsStreamer 实时 xctrace 指标流
- templates:      TemplateLibrary Instruments 模板注册与扩展
- integrator:     与 Orchestrator 的深度集成胶水
"""

from .config import PerfConfig
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
    build_xctrace_record_cmd,
    list_available_devices,
    list_available_templates,
)
from .integrator import PerfIntegrator

__all__ = [
    "PerfConfig",
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
    "build_xctrace_record_cmd",
    "list_available_devices",
    "list_available_templates",
    "PerfIntegrator",
]
