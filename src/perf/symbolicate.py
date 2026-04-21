"""深度后置符号化 CLI 工具 — Shim forwarding to src.perf.locate.dsym.

公共 API 保留:作为 resolver 的补充,用于:
- perf symbolicate CLI 命令
- 离线批量处理已采集的 hotspots.jsonl
"""
from .locate.dsym import *  # noqa: F401,F403
from .locate.dsym import (  # noqa: F401 — 显式 re-export 带下划线符号
    _is_unsymbolicated,
    _extract_address,
    _ensure_cache_dir,
    _cache_path,
    _find_dsym_in_products,
    _read_bundle_id,
    _read_archive_bundle_id,
    _try_xcodebuild_download,
    _try_asc_api_download,
    _find_recently_downloaded_dsym,
    _find_containing_dsym,
    _find_binary_in_dsym,
    _atos_batch,
    _swift_demangle_regex,
)
