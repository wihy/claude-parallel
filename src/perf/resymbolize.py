"""
二次符号化 — 用 LinkMap 反查替换 hotspots 里的 dyld 兜底/未解析符号。

输入: hotspots top 项 (含 addr 字段, sampling.py 新版输出)
输出: 替换 symbol 为业务真实函数名 (来自 LinkMap)

应用场景:
  xctrace export 时业务地址无 dSYM, fallback 到 dyld_findClosestSymbol
  → sampling.py 把原始 addr 保留到 hotspots top item.addr
  → 这里用 MultiLinkMap.lookup(addr) 找回业务函数名
  → dashboard 显示真实业务函数

设计要点:
- 完全 noop 友好: addr 缺失 / lookup 未命中 / LinkMap 未配 → 原样返回
- 命中时附加 _resymbolized: True 标记 + _origin 记录原 symbol
- 不破坏旧 hotspots.jsonl 格式
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 这些符号是 xctrace/dyld 兜底的标志, 命中即说明原符号化失败
# 一旦看到这种符号 + 有 addr, 就值得用 LinkMap 反查
_PLACEHOLDER_PATTERNS = [
    re.compile(r"^dyld\d?::"),
    re.compile(r"findClosestSymbol"),
    re.compile(r"^0x[0-9a-fA-F]+$"),
    re.compile(r"^<0x[0-9a-fA-F]+>$"),
    re.compile(r"^\?\?\?$"),
    re.compile(r"^_dyld_"),
]


def is_placeholder_symbol(name: str) -> bool:
    """判断符号是否是占位/兜底, 应该尝试反查"""
    if not name:
        return True
    for pat in _PLACEHOLDER_PATTERNS:
        if pat.search(name):
            return True
    return False


def resymbolize_hotspot_items(
    items: List[Dict[str, Any]],
    multi_linkmap: Any,  # MultiLinkMap (避免循环 import)
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """对 hotspots 一组 top 项做二次符号化。

    返回: (新 items 列表, 统计 dict)
        统计 = {total, with_addr, placeholders, resymbolized, hit_rate}
    """
    if not items or multi_linkmap is None:
        return items, {"total": len(items or []), "with_addr": 0,
                       "placeholders": 0, "resymbolized": 0}

    out = []
    total = len(items)
    with_addr = 0
    placeholders = 0
    resymbolized = 0

    for item in items:
        # 复制避免修改源数据
        new_item = dict(item)
        sym = new_item.get("symbol", "") or new_item.get("name", "")
        addr_str = new_item.get("addr", "")
        # 只对有 addr 且看起来是兜底的符号尝试反查
        if addr_str:
            with_addr += 1
        is_ph = is_placeholder_symbol(sym)
        if is_ph:
            placeholders += 1

        if addr_str and is_ph:
            try:
                addr = int(addr_str, 0) if isinstance(addr_str, str) else int(addr_str)
                lk_sym = multi_linkmap.lookup(addr)
                if lk_sym:
                    new_item["_origin_symbol"] = sym
                    new_item["symbol"] = lk_sym.name
                    new_item["_resymbolized"] = True
                    new_item["_resym_source"] = "LinkMap"
                    if lk_sym.file_path:
                        new_item["_resym_file"] = lk_sym.file_path
                    resymbolized += 1
            except (ValueError, TypeError) as e:
                logger.debug("[resymbolize] lookup 失败 addr=%s: %s", addr_str, e)

        out.append(new_item)

    stats = {
        "total": total,
        "with_addr": with_addr,
        "placeholders": placeholders,
        "resymbolized": resymbolized,
        "hit_rate": round(resymbolized / placeholders * 100, 1) if placeholders else 0.0,
    }
    return out, stats


def resymbolize_cycle(cycle: Dict[str, Any], multi_linkmap: Any) -> Dict[str, Any]:
    """对单个 cycle 数据做二次符号化 (含 top 数组)。"""
    if not cycle or multi_linkmap is None:
        return cycle
    new_cycle = dict(cycle)
    top = cycle.get("top") or cycle.get("functions") or []
    if not top:
        return new_cycle
    new_top, stats = resymbolize_hotspot_items(top, multi_linkmap)
    if cycle.get("top") is not None:
        new_cycle["top"] = new_top
    if cycle.get("functions") is not None:
        new_cycle["functions"] = new_top
    if stats["resymbolized"] > 0:
        new_cycle["_resymbolize_stats"] = stats
    return new_cycle


def resymbolize_cycles(cycles: List[Dict[str, Any]], multi_linkmap: Any) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """批量处理多 cycle, 返回新 cycles + 全局统计"""
    if not cycles or multi_linkmap is None:
        return cycles, {"cycles": len(cycles or []), "total": 0,
                        "placeholders": 0, "resymbolized": 0, "hit_rate": 0.0}
    out = []
    agg = {"total": 0, "with_addr": 0, "placeholders": 0, "resymbolized": 0}
    for c in cycles:
        new_c = resymbolize_cycle(c, multi_linkmap)
        out.append(new_c)
        s = new_c.get("_resymbolize_stats")
        if s:
            for k in ("total", "with_addr", "placeholders", "resymbolized"):
                agg[k] += s.get(k, 0)
    agg["cycles"] = len(cycles)
    agg["hit_rate"] = (round(agg["resymbolized"] / agg["placeholders"] * 100, 1)
                       if agg["placeholders"] else 0.0)
    return out, agg
