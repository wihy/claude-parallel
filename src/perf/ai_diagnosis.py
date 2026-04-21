"""Transitional shim — 迁移到 src.perf.analyze.ai_diagnosis."""
from .analyze.ai_diagnosis import *  # noqa: F401,F403
from .analyze.ai_diagnosis import (  # noqa: F401 — 显式 re-export 带下划线符号
    _read_meta_summary,
    _read_timeline_summary,
    _read_alert_log,
    _tail_file,
    _fallback_read_jsonl_text,
    _collect_deep_schemas,
    _load_meta_json,
    _estimate_tokens,
    _truncate_to_tokens,
    _extract_section,
    _extract_numbered_items,
    _extract_priority_items,
    _fallback_extract_paragraphs,
    _extract_power_summary,
    _extract_top_symbols,
    _detect_webkit_patterns,
    _get_webkit_suggestions,
)
