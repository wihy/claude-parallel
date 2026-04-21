"""Transitional shim — 迁移到 src.perf.analyze.power_attribution."""
from .analyze.power_attribution import *  # noqa: F401,F403
from .analyze.power_attribution import (  # noqa: F401 — 显式 re-export 带下划线符号
    _iterparse_power_rows,
    _parse_mw_value,
    _parse_cpu_from_jsonl,
    _parse_cpu_from_timeprofile,
    _parse_cpu_from_timeprofile_backtrace,
    _parse_cpu_from_timeprofile_legacy,
    _append_lifecycle_event,
    _detect_memory_growth,
)
