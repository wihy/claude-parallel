"""SymbolCache — address→symbol LRU 缓存 + 原子 JSON 持久化。

用途:
    session 级缓存,命中后下次同包 UUID 启动即可恢复,避免重复符号化。
"""

from collections import OrderedDict
from pathlib import Path
from typing import Optional

from src.infrastructure.storage.atomic import atomic_write_json, safe_read_json


DEFAULT_CAPACITY = 10_000


class SymbolCache:
    """LRU + 落盘 JSON。线程安全性:外层持锁调用 (resolver 层单线程化)。"""

    def __init__(self, cache_dir: Path, capacity: int = DEFAULT_CAPACITY):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.cache_dir / "symbols.json"
        self._capacity = capacity
        self._data: "OrderedDict[int, str]" = OrderedDict()

    def get(self, addr: int) -> Optional[str]:
        sym = self._data.get(addr)
        if sym is not None:
            self._data.move_to_end(addr)
        return sym

    def put(self, addr: int, symbol: str) -> None:
        if addr in self._data:
            self._data.move_to_end(addr)
        self._data[addr] = symbol
        while len(self._data) > self._capacity:
            self._data.popitem(last=False)

    def load(self) -> None:
        raw = safe_read_json(self._path, None)
        if not isinstance(raw, dict):
            return
        for k, v in raw.items():
            try:
                self._data[int(k)] = str(v)
            except ValueError:
                continue

    def flush(self) -> None:
        payload = {str(k): v for k, v in self._data.items()}
        atomic_write_json(self._path, payload)

    def __len__(self) -> int:
        return len(self._data)
