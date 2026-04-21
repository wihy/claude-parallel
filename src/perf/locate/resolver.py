"""SymbolResolver — 符号化统一入口。

三层查询顺序:
    cache → linkmap → atos → hex
每层命中立即返回,并把结果回填 cache。
"""

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .cache import SymbolCache
from .atos import AtosDaemon


DEFAULT_TIMEOUT_MS = 500


@dataclass(frozen=True)
class Symbol:
    """resolver 输出 — 名字 + 溯源 (便于下游 UI 着色)。"""
    name: str
    source: str  # cache | linkmap | atos | unresolved


class SymbolResolver:
    """线程安全 — warmup 后只读的 linkmap / atos;cache 自带弱一致性。"""

    def __init__(
        self,
        binary_path: str,
        dsym_paths: list,
        linkmap_path=None,                  # 兼容: Optional[str | list[str]]
        cache_dir: Path = None,
        *,
        linkmap_paths: Optional[list] = None,
    ):
        self.binary_path = binary_path
        self.dsym_paths = list(dsym_paths)
        # 合并 linkmap_path (单数, 兼容旧调用) 与 linkmap_paths (复数, 新规范)
        paths: list = []
        if linkmap_paths:
            paths.extend(str(p) for p in linkmap_paths if p)
        if linkmap_path:
            if isinstance(linkmap_path, (list, tuple)):
                paths.extend(str(p) for p in linkmap_path if p)
            else:
                paths.append(str(linkmap_path))
        # 去重保序
        seen = set()
        self.linkmap_paths: list = []
        for p in paths:
            if p not in seen:
                seen.add(p)
                self.linkmap_paths.append(p)
        # 单数属性保留 (hint 兼容): 取第一个
        self.linkmap_path = self.linkmap_paths[0] if self.linkmap_paths else None
        self._cache = SymbolCache(cache_dir)
        self._linkmap = None  # MultiLinkMap, warmup 时加载
        self._atos: Optional[AtosDaemon] = None
        self._warmup_done = threading.Event()
        self._warmup_lock = threading.Lock()

    def warmup(self) -> None:
        """加载 LinkMap + 启 atos daemon + 恢复缓存。幂等。"""
        with self._warmup_lock:
            if self._warmup_done.is_set():
                return
            self._cache.load()
            if self.linkmap_paths:
                try:
                    from .linkmap import MultiLinkMap, LinkMap
                    lm = MultiLinkMap()
                    for p in self.linkmap_paths:
                        try:
                            lm.add(LinkMap.load(p))
                        except (OSError, ValueError):
                            continue  # 单个坏文件不阻断其余加载
                    self._linkmap = lm if lm.linkmaps else None
                except (OSError, ValueError):
                    self._linkmap = None
            if self.binary_path and Path(self.binary_path).exists():
                try:
                    # iOS 用户 app 默认 load base 0x100000000 (arm64)
                    # 采样地址是设备运行时绝对地址, atos 用 -l 计算 offset = addr - base
                    daemon = AtosDaemon(self.binary_path, load_addr=0x100000000)
                    daemon.start()
                    self._atos = daemon
                except OSError:
                    self._atos = None
            self._warmup_done.set()

    def resolve(self, addr: int, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Symbol:
        # warmup 未完成 → hex 兜底,永不阻塞
        if not self._warmup_done.is_set():
            return Symbol(f"0x{addr:x}", source="unresolved")

        # ① cache
        cached = self._cache.get(addr)
        if cached is not None:
            return Symbol(cached, source="cache")

        # ② linkmap bisect (MultiLinkMap 返回 Symbol 对象, mock 可能返回 str)
        if self._linkmap is not None:
            try:
                raw = self._linkmap.lookup(addr)
                if raw:
                    name = getattr(raw, "name", raw)
                    if isinstance(name, str) and name:
                        self._cache.put(addr, name)
                        return Symbol(name, source="linkmap")
            except Exception:
                pass

        # ③ atos daemon
        if self._atos is not None:
            try:
                sym = self._atos.lookup(addr)
                if sym and not sym.startswith("0x"):
                    self._cache.put(addr, sym)
                    return Symbol(sym, source="atos")
            except Exception:
                pass

        # ④ hex 兜底
        return Symbol(f"0x{addr:x}", source="unresolved")

    def resolve_batch(self, addrs: list) -> dict:
        return {a: self.resolve(a) for a in addrs}

    def shutdown(self) -> None:
        try:
            self._cache.flush()
        except OSError:
            pass
        if self._atos is not None:
            self._atos.shutdown()
            self._atos = None

    @classmethod
    def from_config(cls, cfg, repo_path: Path):
        """工厂 — 若 binary / linkmap / dsym 都发现不了则返回 None。

        调用者自行判空后决定是否注入 sampling。
        """
        binary = getattr(cfg, "binary_path", "") or ""
        # 新 API linkmap_paths (list), 向后兼容 linkmap_path (单字符串或 None)
        linkmaps = list(getattr(cfg, "linkmap_paths", []) or [])
        if not linkmaps:
            single = getattr(cfg, "linkmap_path", "") or ""
            if single:
                linkmaps = [single]
        dsyms = list(getattr(cfg, "dsym_paths", []) or [])
        if not binary and not linkmaps and not dsyms:
            return None
        cache_dir = Path(repo_path) / ".claude-parallel" / "locate_cache"
        return cls(
            binary_path=binary,
            dsym_paths=dsyms,
            linkmap_paths=linkmaps,
            cache_dir=cache_dir,
        )
