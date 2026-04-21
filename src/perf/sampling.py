"""Transitional shim — 迁移到 src.perf.capture.sampling."""
from .capture.sampling import *  # noqa: F401,F403
from .capture.sampling import (  # noqa: F401 — 显式 re-export 带下划线符号
    _enrich_top_with_resolver,
    _coerce_addr_to_int,
)
