"""
LinkMap 解析器 — Xcode 编译产物的地址→符号映射。

设计目标 (准确 + 高效):
- 解析: 单遍流式扫描 (内存恒定), parallel 多 LinkMap 同时解析
- 查询: bisect O(log n), Symbol 字段 lazy 填充避免内存浪费
- 缓存: pickle 持久化 ~/.cache/cpar/linkmap/, mtime 校验失效
- 多 LinkMap 统一查询: MultiLinkMap 容器，按地址区间路由

LinkMap 格式（简化）:
    # Path: /path/to/Soul_New
    # Arch: arm64
    # Object files:
    [  0] linker synthesized
    [  1] /path/to/SOPatRoomVC.o
    ...
    # Symbols:
    # Address	Size      File Name
    0x100008000	0x00000050 [  1] -[SOPatRoomVC handleAudio:]

用法:
    # 单 LinkMap (带缓存)
    >>> lm = LinkMap.load("Soul_New-LinkMap-normal-arm64.txt")
    >>> lm.lookup(0x100008010)
    Symbol(...)

    # 多 LinkMap 统一查询
    >>> mlm = MultiLinkMap.warm_all_from_derived_data()
    >>> mlm.lookup(0x123456789)  # 自动路由到对应 LinkMap
"""

from __future__ import annotations

import bisect
import hashlib
import logging
import os
import pickle
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 缓存格式版本 — 升级解析器/数据结构时 +1，让旧缓存自动失效
CACHE_VERSION = 2

CACHE_DIR = Path.home() / ".cache" / "cpar" / "linkmap"


@dataclass
class Symbol:
    addr: int          # 镜像内 VM 地址 (静态地址, 未加 ASLR slide)
    size: int          # 符号大小
    file_idx: int      # 来自 # Object files 区的索引
    name: str          # demangled 符号名
    file_path: str = ""  # 关联的 .o 文件路径 (lazy 填充)

    @property
    def end_addr(self) -> int:
        return self.addr + self.size

    def __repr__(self) -> str:
        return (f"Symbol(0x{self.addr:x}+{self.size}, [{self.file_idx}] "
                f"{self.name[:60]}{'...' if len(self.name) > 60 else ''})")


_HEADER_RE = re.compile(r"^# (Path|Arch|Object files|Sections|Symbols)")
_OBJ_RE = re.compile(r"^\[\s*(\d+)\]\s+(.+?)\s*$")
# 符号行: 0xADDR<TAB>0xSIZE<TAB>[<idx>]<space>name
_SYM_RE = re.compile(
    r"^0x([0-9A-Fa-f]+)\s+0x([0-9A-Fa-f]+)\s+\[\s*(\d+)\]\s+(.+?)\s*$"
)


class LinkMap:
    """LinkMap 内存表示: 按地址有序的符号列表 + 二分查找。"""

    def __init__(self):
        self.path: str = ""
        self.arch: str = ""
        self.binary: str = ""
        self.symbols: List[Symbol] = []  # 按 addr 升序
        self.object_files: Dict[int, str] = {}  # idx → file path
        self._addr_index: List[int] = []  # 与 symbols 同序的 addr 列表，bisect 用
        self.parse_seconds: float = 0.0  # 解析耗时 (用于性能基准)

    # ── 缓存路径 ──
    @staticmethod
    def _cache_path_for(linkmap_path: str) -> Path:
        """缓存文件路径: ~/.cache/cpar/linkmap/<sha1(abs_path)>.pkl"""
        h = hashlib.sha1(linkmap_path.encode("utf-8")).hexdigest()[:16]
        return CACHE_DIR / f"{h}.pkl"

    @classmethod
    def load(cls, path: str, use_cache: bool = True) -> "LinkMap":
        """加载 LinkMap (带缓存)。优先 cache, mtime 校验, 失效则重新解析。"""
        abs_path = str(Path(path).resolve())
        if use_cache:
            cached = cls._try_load_cache(abs_path)
            if cached is not None:
                return cached
        lm = cls.parse(abs_path)
        if use_cache:
            cls._save_cache(abs_path, lm)
        return lm

    @classmethod
    def _try_load_cache(cls, abs_path: str) -> Optional["LinkMap"]:
        """尝试从缓存加载，校验源文件 mtime 一致。"""
        cache_path = cls._cache_path_for(abs_path)
        if not cache_path.exists():
            return None
        try:
            src_mtime = Path(abs_path).stat().st_mtime
            with open(cache_path, "rb") as f:
                payload = pickle.load(f)
            if (payload.get("version") == CACHE_VERSION
                    and payload.get("src_path") == abs_path
                    and payload.get("src_mtime") == src_mtime):
                lm = payload["linkmap"]
                lm.parse_seconds = 0.0  # 命中缓存
                return lm
        except (OSError, pickle.PickleError, KeyError, EOFError) as e:
            logger.debug("[linkmap] 缓存加载失败 %s: %s", cache_path, e)
        return None

    @classmethod
    def _save_cache(cls, abs_path: str, lm: "LinkMap") -> None:
        """持久化到缓存。"""
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_path = cls._cache_path_for(abs_path)
            payload = {
                "version": CACHE_VERSION,
                "src_path": abs_path,
                "src_mtime": Path(abs_path).stat().st_mtime,
                "linkmap": lm,
            }
            tmp = cache_path.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, cache_path)
        except Exception as e:
            logger.debug("[linkmap] 缓存写入失败: %s", e)

    @classmethod
    def parse(cls, path: str) -> "LinkMap":
        """从 LinkMap 文本文件加载 (无缓存路径)。"""
        t0 = time.perf_counter()
        lm = cls()
        path_obj = Path(path)
        if not path_obj.exists():
            raise FileNotFoundError(f"LinkMap 文件不存在: {path}")

        lm.path = str(path_obj)
        section = ""
        # 局部引用提速 (Python attribute lookup 慢)
        symbols = lm.symbols
        object_files = lm.object_files
        sym_re_match = _SYM_RE.match
        obj_re_match = _OBJ_RE.match

        with open(path_obj, "rb") as f_bin:  # 二进制更快, decode 单行
            for raw in f_bin:
                if not raw or raw[0] == 0x0a:  # 空行
                    continue
                # 段头快速分支
                if raw[0] == 0x23:  # '#'
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line.startswith("# Path: "):
                        lm.binary = line[len("# Path: "):].strip()
                    elif line.startswith("# Arch: "):
                        lm.arch = line[len("# Arch: "):].strip()
                    elif line.startswith("# Object files:"):
                        section = "objects"
                    elif line.startswith("# Sections:"):
                        section = "sections"
                    elif line.startswith("# Symbols:"):
                        section = "symbols"
                    continue

                if section == "symbols":
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    m = sym_re_match(line)
                    if m:
                        symbols.append(Symbol(
                            int(m.group(1), 16),
                            int(m.group(2), 16),
                            int(m.group(3)),
                            m.group(4),
                        ))
                elif section == "objects":
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    m = obj_re_match(line)
                    if m:
                        object_files[int(m.group(1))] = m.group(2)

        # 排序+构建二分查找索引
        symbols.sort(key=lambda s: s.addr)
        lm._addr_index = [s.addr for s in symbols]
        # 反向填充 file_path 到 Symbol
        for s in symbols:
            s.file_path = object_files.get(s.file_idx, "")
        lm.parse_seconds = time.perf_counter() - t0
        return lm

    def lookup(self, addr: int) -> Optional[Symbol]:
        """通过 VM 地址（不带 ASLR slide）查符号。"""
        if not self._addr_index:
            return None
        # 二分找最大的 ≤ addr 的符号
        idx = bisect.bisect_right(self._addr_index, addr) - 1
        if idx < 0:
            return None
        sym = self.symbols[idx]
        # 验证落在符号范围内
        if sym.addr <= addr < sym.end_addr:
            return sym
        return None

    def lookup_by_offset(self, offset: int, base_addr: Optional[int] = None) -> Optional[Symbol]:
        """通过镜像内偏移查符号。

        base_addr: 镜像 __TEXT 段起始（默认用第一个 symbol 地址）
        """
        if base_addr is None and self.symbols:
            base_addr = self.symbols[0].addr & ~0xFFFFF  # 对齐到 1MB
        return self.lookup(base_addr + offset) if base_addr else None

    def search_by_name(self, pattern: str, max_results: int = 20) -> List[Symbol]:
        """按符号名子串搜索（不区分大小写）。"""
        pat = pattern.lower()
        out = []
        for s in self.symbols:
            if pat in s.name.lower():
                out.append(s)
                if len(out) >= max_results:
                    break
        return out

    def stats(self) -> Dict[str, int]:
        """返回统计信息。"""
        return {
            "total_symbols": len(self.symbols),
            "total_object_files": len(self.object_files),
            "addr_min": self.symbols[0].addr if self.symbols else 0,
            "addr_max": self.symbols[-1].end_addr if self.symbols else 0,
            "biz_symbols": sum(1 for s in self.symbols
                              if s.name.startswith(("-[SO", "+[SO", "SO"))
                              and not s.name.startswith("___")),
            "objc_symbols": sum(1 for s in self.symbols
                               if s.name.startswith(("-[", "+["))),
            "cpp_symbols": sum(1 for s in self.symbols if "::" in s.name),
        }


# ── 多 LinkMap 统一查询 ──────────────────────────────────────

class MultiLinkMap:
    """跨多个 LinkMap 的统一符号查询。

    地址区间不重叠时用 (addr_min, addr_max) 路由到对应 LinkMap; 否则全表查找。
    """

    def __init__(self):
        self.linkmaps: List[LinkMap] = []
        # (addr_min, addr_max, lm_index) 区间索引, 按 addr_min 排序
        self._range_index: List[Tuple[int, int, int]] = []
        # symbol name → list of Symbol (跨 lm 全文搜索缓存)
        self._name_cache: Optional[Dict[str, List[Tuple[int, int]]]] = None

    def add(self, lm: LinkMap) -> None:
        if not lm.symbols:
            return
        idx = len(self.linkmaps)
        self.linkmaps.append(lm)
        addr_min = lm.symbols[0].addr
        addr_max = lm.symbols[-1].end_addr
        self._range_index.append((addr_min, addr_max, idx))
        self._range_index.sort(key=lambda x: x[0])
        self._name_cache = None  # 失效

    def lookup(self, addr: int) -> Optional[Symbol]:
        """按地址查符号 — 优先用区间路由，回退全表扫描。"""
        # 1) 区间路由
        for amin, amax, idx in self._range_index:
            if amin <= addr < amax:
                sym = self.linkmaps[idx].lookup(addr)
                if sym is not None:
                    return sym
        # 2) 区间外的孤儿地址 fallback - 全 LinkMap 扫
        for lm in self.linkmaps:
            sym = lm.lookup(addr)
            if sym is not None:
                return sym
        return None

    def search_by_name(self, pattern: str, max_results: int = 30) -> List[Tuple[Symbol, str]]:
        """跨 LinkMap 名字搜索, 返回 (Symbol, linkmap_binary)。"""
        out = []
        for lm in self.linkmaps:
            for s in lm.search_by_name(pattern, max_results=max_results):
                out.append((s, lm.binary))
                if len(out) >= max_results:
                    return out
        return out

    def stats(self) -> Dict[str, int]:
        return {
            "linkmaps": len(self.linkmaps),
            "total_symbols": sum(len(lm.symbols) for lm in self.linkmaps),
            "biz_symbols": sum(lm.stats()["biz_symbols"] for lm in self.linkmaps),
            "objc_symbols": sum(lm.stats()["objc_symbols"] for lm in self.linkmaps),
            "cpp_symbols": sum(lm.stats()["cpp_symbols"] for lm in self.linkmaps),
        }

    @classmethod
    def warm_all_from_derived_data(
        cls,
        project_name: str = "Soul_New",
        arch: str = "arm64",
        max_workers: int = 4,
    ) -> "MultiLinkMap":
        """并发预热 DerivedData 中所有 LinkMap, 全部加缓存。"""
        files = find_linkmaps(project_name=project_name, arch=arch)
        mlm = cls()
        if not files:
            return mlm
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(LinkMap.load, str(f)): f for f in files}
            for fut in as_completed(futures):
                f = futures[fut]
                try:
                    lm = fut.result()
                    mlm.add(lm)
                    logger.debug("[linkmap] warmed %s (%d syms, %.1fs)",
                                 f.name, len(lm.symbols), lm.parse_seconds)
                except Exception as e:
                    logger.warning("[linkmap] warm failed %s: %s", f, e)
        return mlm


# ── LinkMap 自动发现 ─────────────────────────────────────────

def find_linkmaps(
    derived_data: Optional[str] = None,
    project_name: str = "Soul_New",
    arch: str = "arm64",
) -> List[Path]:
    """从 ~/Library/Developer/Xcode/DerivedData 自动找 LinkMap 文件。

    Returns: 按 mtime 降序的 LinkMap 路径列表（最新的在前）
    """
    root = Path(derived_data or Path.home() / "Library" / "Developer" / "Xcode" / "DerivedData")
    if not root.exists():
        return []
    pattern = f"*{project_name}*-LinkMap-normal-{arch}.txt"
    out = list(root.rglob(pattern))
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


# ── CLI 入口 ─────────────────────────────────────────────────

def main():
    """cpar perf linkmap CLI 入口。"""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="LinkMap 解析与查询")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_find = sub.add_parser("find", help="自动发现 LinkMap 文件")
    p_find.add_argument("--project", default="Soul_New")
    p_find.add_argument("--arch", default="arm64")

    p_parse = sub.add_parser("parse", help="解析 LinkMap 并显示统计")
    p_parse.add_argument("file", help="LinkMap 文件路径")
    p_parse.add_argument("--json", action="store_true")

    p_lookup = sub.add_parser("lookup", help="按地址查符号")
    p_lookup.add_argument("file", help="LinkMap 文件路径")
    p_lookup.add_argument("addr", help="VM 地址 (0x... 或十进制)")

    p_search = sub.add_parser("search", help="按名字搜符号")
    p_search.add_argument("file", help="LinkMap 文件路径")
    p_search.add_argument("pattern", help="符号名子串")
    p_search.add_argument("--max", type=int, default=20)

    args = parser.parse_args()

    if args.cmd == "find":
        files = find_linkmaps(project_name=args.project, arch=args.arch)
        if not files:
            print("(未找到 LinkMap 文件，请确认 Build Settings → Write Link Map File: Yes)")
            return
        for f in files:
            mtime = f.stat().st_mtime
            size_mb = f.stat().st_size / 1024 / 1024
            from datetime import datetime
            print(f"  {datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')}  "
                  f"{size_mb:>6.1f}MB  {f}")
        return

    if args.cmd == "parse":
        lm = LinkMap.parse(args.file)
        stats = lm.stats()
        if args.json:
            print(json.dumps({**stats, "binary": lm.binary, "arch": lm.arch}, indent=2))
        else:
            print(f"  Binary:  {lm.binary}")
            print(f"  Arch:    {lm.arch}")
            print(f"  Symbols: {stats['total_symbols']:,}")
            print(f"    OC 方法 (-[/+[): {stats['objc_symbols']:,}")
            print(f"    C++ (::): {stats['cpp_symbols']:,}")
            print(f"    Soul 业务 (SO 前缀): {stats['biz_symbols']:,}")
            print(f"  Object files: {stats['total_object_files']:,}")
            print(f"  Address range: 0x{stats['addr_min']:x} ~ 0x{stats['addr_max']:x}")
        return

    if args.cmd == "lookup":
        lm = LinkMap.parse(args.file)
        addr = int(args.addr, 0)  # 自动识别 0x / 0o / 0b / 十进制
        sym = lm.lookup(addr)
        if sym:
            print(f"  地址:      0x{addr:x}")
            print(f"  符号:      {sym.name}")
            print(f"  范围:      0x{sym.addr:x} - 0x{sym.end_addr:x} (size={sym.size})")
            print(f"  Object:    {sym.file_path}")
            print(f"  偏移内部:  +0x{addr - sym.addr:x}")
        else:
            print(f"  地址 0x{addr:x} 未找到对应符号")
        return

    if args.cmd == "search":
        lm = LinkMap.parse(args.file)
        results = lm.search_by_name(args.pattern, max_results=args.max)
        print(f"  搜索 '{args.pattern}' 命中 {len(results)} (最多 {args.max}):")
        for s in results:
            print(f"  0x{s.addr:>10x}  size=0x{s.size:>5x}  {s.name[:80]}")
        return


if __name__ == "__main__":
    main()
