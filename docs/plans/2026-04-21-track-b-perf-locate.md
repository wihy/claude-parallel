# Track B: perf/locate 实时方法定位优化 — 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把符号化三路径（dSYM+atos / LinkMap / resymbolize）合并为 `SymbolResolver` 单一入口，并集成到 sampling cycle，使 `cpar perf hotspots --follow` 默认显示业务函数名（命中率 >90%），消除 hex 中间态。

**Architecture:** 新建 `src/perf/locate/` 子包作为前哨（即便 Track A 的 perf 5 层切分还没开始）。内部三层查询策略：LRU cache → LinkMap bisect → atos daemon → hex 兜底。每次 resolve 500ms timeout 硬保护，永不阻塞 cycle。sampling 只消费 resolver，不构造。

**Tech Stack:** Python 3.9+、`xml.etree.ElementTree.iterparse`、`subprocess` + `threading`（AtosDaemon 常驻子进程）、`bisect`（LinkMap 查找）、`collections.OrderedDict`（LRU）、`concurrent.futures`（timeout 保护）、`unittest`。

**设计出处:** `docs/plans/2026-04-21-layering-and-locate-design.md` §4 + §5 + §6

---

## 前置检查（开工前一次性做完）

### Task 0: Baseline 验证

**Files:** 不改，只确认环境

**Step 0.1: 确认分支和 worktree**

Run:
```bash
cd /Users/chunhaixu/claude-parallel/.worktrees/perf-locate
git status
git branch --show-current
```

Expected:
- branch: `feature/perf-locate`
- clean tree

**Step 0.2: Baseline 测试**

Run:
```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
```

Expected: `Ran 44 tests ... OK`

**Step 0.3: 确认 linkmap / symbolicate 现状**

Run:
```bash
ls src/perf/linkmap.py src/perf/symbolicate.py src/perf/resymbolize.py
python3 -c "from src.perf.linkmap import MultiLinkMap; from src.perf.symbolicate import auto_symbolicate; print('imports ok')"
```

Expected: `imports ok`

---

## 阶段 B-W1: Locate 基础设施（4 个原子任务）

### Task 1: 创建 locate/ 包骨架 + 迁移 linkmap.py

**Files:**
- Create: `src/perf/locate/__init__.py`
- Create: `src/perf/locate/linkmap.py`（从 `src/perf/linkmap.py` 复制）
- Modify: `src/perf/linkmap.py` → 改为 1 行 re-export shim（过渡期兼容）
- Test: `tests/test_locate_package.py`

**Step 1.1: 测试先行 — 保证迁移后公共接口不变**

写 `tests/test_locate_package.py`：
```python
import unittest


class LocatePackageTest(unittest.TestCase):
    def test_multilinkmap_import_from_new_path(self):
        from src.perf.locate.linkmap import MultiLinkMap, LinkMapEntry
        self.assertTrue(callable(MultiLinkMap))

    def test_multilinkmap_import_from_old_path_still_works(self):
        # 过渡期兼容 — 应该能从老路径导入（shim 转发）
        from src.perf.linkmap import MultiLinkMap
        self.assertTrue(callable(MultiLinkMap))

    def test_locate_package_exports(self):
        from src.perf import locate
        self.assertTrue(hasattr(locate, 'linkmap'))


if __name__ == "__main__":
    unittest.main()
```

**Step 1.2: 跑测试确认失败**

Run:
```bash
python3 -m unittest tests.test_locate_package -v 2>&1 | tail -10
```

Expected: 3 个 test 全 FAIL（`ModuleNotFoundError: No module named 'src.perf.locate'`）

**Step 1.3: 最小实现**

```bash
mkdir -p src/perf/locate
touch src/perf/locate/__init__.py
cp src/perf/linkmap.py src/perf/locate/linkmap.py
```

把 `src/perf/linkmap.py` 改成 shim：
```python
"""Transitional shim — will be removed after Track B completes."""
from .locate.linkmap import *  # noqa: F401,F403
```

**Step 1.4: 跑测试确认通过**

Run:
```bash
python3 -m unittest tests.test_locate_package -v 2>&1 | tail -10
```

Expected: `Ran 3 tests ... OK`

**Step 1.5: 回归测试**

Run:
```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
```

Expected: `Ran 47 tests ... OK`（44 + 3 新的）

**Step 1.6: 提交**

```bash
git add src/perf/locate/ src/perf/linkmap.py tests/test_locate_package.py
git commit -m "feat(locate): 建 perf/locate/ 包 + 迁移 linkmap.py (保留 shim)"
```

---

### Task 2: AtosDaemon — 常驻 atos 子进程

**Files:**
- Create: `src/perf/locate/atos.py`
- Test: `tests/test_locate_atos.py`

**Step 2.1: 写失败测试**

`tests/test_locate_atos.py`：
```python
import unittest
from unittest.mock import patch, MagicMock


class AtosDaemonTest(unittest.TestCase):

    def test_daemon_starts_atos_subprocess(self):
        from src.perf.locate.atos import AtosDaemon
        with patch("subprocess.Popen") as popen:
            popen.return_value = MagicMock()
            d = AtosDaemon(binary_path="/tmp/fake_bin", load_addr=0x100000000)
            d.start()
            args = popen.call_args[0][0]
            self.assertIn("atos", args[0])
            self.assertIn("-o", args)
            self.assertIn("-l", args)

    def test_lookup_returns_hex_when_not_started(self):
        from src.perf.locate.atos import AtosDaemon
        d = AtosDaemon(binary_path="/tmp/fake_bin", load_addr=0x100000000)
        result = d.lookup(0x100001234)
        self.assertTrue(result.startswith("0x"))

    def test_lookup_sends_addr_and_reads_symbol(self):
        from src.perf.locate.atos import AtosDaemon
        d = AtosDaemon(binary_path="/tmp/fake_bin", load_addr=0x100000000)
        # inject mock stdin/stdout via _stdin_writer/_stdout_reader queues
        d._started = True
        d._put_response("MyClass.swiftFunc()")
        result = d.lookup(0x100001234)
        self.assertEqual(result, "MyClass.swiftFunc()")

    def test_shutdown_terminates_process(self):
        from src.perf.locate.atos import AtosDaemon
        d = AtosDaemon(binary_path="/tmp/fake_bin", load_addr=0x100000000)
        proc = MagicMock()
        d._proc = proc
        d._started = True
        d.shutdown()
        proc.terminate.assert_called_once()

    def test_blacklist_after_consecutive_failures(self):
        from src.perf.locate.atos import AtosDaemon
        d = AtosDaemon(binary_path="/tmp/fake_bin", load_addr=0x100000000)
        d._started = True
        # 5 次连续失败后该地址入黑名单
        addr = 0x100001234
        for _ in range(6):
            d._record_failure(addr)
        self.assertIn(addr, d._blacklist)


if __name__ == "__main__":
    unittest.main()
```

**Step 2.2: 跑测试确认失败**

Run:
```bash
python3 -m unittest tests.test_locate_atos -v 2>&1 | tail -15
```

Expected: 5 个 test 全 FAIL（`No module named 'src.perf.locate.atos'`）

**Step 2.3: 实现 AtosDaemon**

Create `src/perf/locate/atos.py`：
```python
"""常驻 atos 子进程 — 消除每次 subprocess 的启动开销。

使用方式:
    daemon = AtosDaemon(binary_path, load_addr)
    daemon.start()          # 启动 atos -i -o bin -l addr
    sym = daemon.lookup(0x100001234)  # 喂 stdin，读 stdout
    daemon.shutdown()       # 停进程，flush
"""

import subprocess
import threading
import queue
import time
from pathlib import Path
from typing import Optional


ATOS_FAILURE_THRESHOLD = 5          # 连续 N 次失败后入黑名单
ATOS_READ_TIMEOUT_SEC = 0.2         # 单次 lookup 软超时


class AtosDaemon:
    """常驻 atos 进程 —— 用 stdin/stdout 流式查询符号。

    线程安全: lookup() 持锁串行化到 atos；对外 API 可多线程调用。
    """

    def __init__(self, binary_path: str, load_addr: int = 0):
        self.binary_path = str(Path(binary_path).expanduser())
        self.load_addr = load_addr
        self._proc: Optional[subprocess.Popen] = None
        self._started = False
        self._lock = threading.Lock()
        self._response_queue: queue.Queue[str] = queue.Queue()
        self._reader_thread: Optional[threading.Thread] = None
        self._failures: dict[int, int] = {}
        self._blacklist: set[int] = set()

    def start(self) -> None:
        if self._started:
            return
        args = [
            "atos", "-i",
            "-o", self.binary_path,
            "-l", hex(self.load_addr),
        ]
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,  # line-buffered
            text=True,
        )
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        self._started = True

    def _read_loop(self) -> None:
        if not self._proc or not self._proc.stdout:
            return
        for line in self._proc.stdout:
            self._response_queue.put(line.rstrip("\n"))

    def _put_response(self, text: str) -> None:
        """测试钩子 — 直接注入响应，绕开真实 atos 子进程。"""
        self._response_queue.put(text)

    def lookup(self, addr: int) -> str:
        """同步查询单个地址。失败或未启动时返回 hex 字符串。"""
        if addr in self._blacklist:
            return f"0x{addr:x}"
        if not self._started or not self._proc or not self._proc.stdin:
            return f"0x{addr:x}"

        with self._lock:
            try:
                self._proc.stdin.write(f"{hex(addr)}\n")
                self._proc.stdin.flush()
                sym = self._response_queue.get(timeout=ATOS_READ_TIMEOUT_SEC)
                if sym and not sym.startswith("0x"):
                    self._failures.pop(addr, None)
                    return sym
                self._record_failure(addr)
                return f"0x{addr:x}"
            except (BrokenPipeError, OSError, queue.Empty):
                self._record_failure(addr)
                return f"0x{addr:x}"

    def _record_failure(self, addr: int) -> None:
        self._failures[addr] = self._failures.get(addr, 0) + 1
        if self._failures[addr] >= ATOS_FAILURE_THRESHOLD:
            self._blacklist.add(addr)

    def shutdown(self) -> None:
        if not self._started:
            return
        self._started = False
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self._proc.kill()
                except OSError:
                    pass
        self._proc = None
```

**Step 2.4: 跑测试确认通过**

Run:
```bash
python3 -m unittest tests.test_locate_atos -v 2>&1 | tail -15
```

Expected: `Ran 5 tests ... OK`

**Step 2.5: 回归**

Run:
```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
```

Expected: `Ran 52 tests ... OK`

**Step 2.6: 提交**

```bash
git add src/perf/locate/atos.py tests/test_locate_atos.py
git commit -m "feat(locate): AtosDaemon 常驻 atos 子进程 + 黑名单 + 读超时保护"
```

---

### Task 3: SymbolCache — LRU + JSON 持久化

**Files:**
- Create: `src/perf/locate/cache.py`
- Test: `tests/test_locate_cache.py`

**Step 3.1: 写失败测试**

`tests/test_locate_cache.py`：
```python
import json
import tempfile
import unittest
from pathlib import Path


class SymbolCacheTest(unittest.TestCase):

    def test_empty_cache_returns_none(self):
        from src.perf.locate.cache import SymbolCache
        with tempfile.TemporaryDirectory() as d:
            c = SymbolCache(Path(d))
            self.assertIsNone(c.get(0x100))

    def test_put_and_get(self):
        from src.perf.locate.cache import SymbolCache
        with tempfile.TemporaryDirectory() as d:
            c = SymbolCache(Path(d))
            c.put(0x100, "MyFunc()")
            self.assertEqual(c.get(0x100), "MyFunc()")

    def test_lru_eviction(self):
        from src.perf.locate.cache import SymbolCache
        with tempfile.TemporaryDirectory() as d:
            c = SymbolCache(Path(d), capacity=3)
            c.put(0x1, "a")
            c.put(0x2, "b")
            c.put(0x3, "c")
            c.put(0x4, "d")  # 触发 0x1 淘汰
            self.assertIsNone(c.get(0x1))
            self.assertEqual(c.get(0x4), "d")

    def test_flush_writes_json(self):
        from src.perf.locate.cache import SymbolCache
        with tempfile.TemporaryDirectory() as d:
            c = SymbolCache(Path(d))
            c.put(0x100, "Func1()")
            c.put(0x200, "Func2()")
            c.flush()
            f = Path(d) / "symbols.json"
            self.assertTrue(f.exists())
            data = json.loads(f.read_text())
            self.assertEqual(data["256"], "Func1()")  # 0x100 = 256
            self.assertEqual(data["512"], "Func2()")

    def test_load_restores_cache(self):
        from src.perf.locate.cache import SymbolCache
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "symbols.json"
            f.write_text(json.dumps({"256": "Cached()", "512": "Other()"}))
            c = SymbolCache(Path(d))
            c.load()
            self.assertEqual(c.get(0x100), "Cached()")
            self.assertEqual(c.get(0x200), "Other()")


if __name__ == "__main__":
    unittest.main()
```

**Step 3.2: 跑测试确认失败**

Run:
```bash
python3 -m unittest tests.test_locate_cache -v 2>&1 | tail -15
```

Expected: 5 test 全 FAIL

**Step 3.3: 实现 SymbolCache**

Create `src/perf/locate/cache.py`：
```python
"""SymbolCache — address→symbol LRU 缓存 + 原子 JSON 持久化。

用途:
    session 级缓存,命中后下次同包 UUID 启动即可恢复,避免重复符号化。
"""

import json
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
        self._data: OrderedDict[int, str] = OrderedDict()

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
```

**Step 3.4: 跑测试确认通过**

Run:
```bash
python3 -m unittest tests.test_locate_cache -v 2>&1 | tail -15
```

Expected: `Ran 5 tests ... OK`

**Step 3.5: 回归**

Run:
```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
```

Expected: `Ran 57 tests ... OK`

**Step 3.6: 提交**

```bash
git add src/perf/locate/cache.py tests/test_locate_cache.py
git commit -m "feat(locate): SymbolCache — LRU + 原子 JSON 落盘"
```

---

### Task 4: SymbolResolver — 三层查询统一入口

**Files:**
- Create: `src/perf/locate/resolver.py`
- Test: `tests/test_locate_resolver.py`

**Step 4.1: 写失败测试**

`tests/test_locate_resolver.py`：
```python
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock


class SymbolResolverTest(unittest.TestCase):

    def _make_resolver(self, tmpdir, linkmap=None, atos=None):
        from src.perf.locate.resolver import SymbolResolver
        r = SymbolResolver(
            binary_path="/tmp/fake",
            dsym_paths=[],
            linkmap_path=None,
            cache_dir=Path(tmpdir),
        )
        r._linkmap = linkmap
        r._atos = atos
        r._warmup_done.set()
        return r

    def test_cache_hit_short_circuits(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._make_resolver(d)
            r._cache.put(0x100, "FromCache()")
            sym = r.resolve(0x100)
            self.assertEqual(sym.name, "FromCache()")
            self.assertEqual(sym.source, "cache")

    def test_linkmap_hit(self):
        with tempfile.TemporaryDirectory() as d:
            lm = MagicMock()
            lm.lookup.return_value = "Business.swiftFunc()"
            r = self._make_resolver(d, linkmap=lm)
            sym = r.resolve(0x200)
            self.assertEqual(sym.name, "Business.swiftFunc()")
            self.assertEqual(sym.source, "linkmap")

    def test_atos_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            lm = MagicMock()
            lm.lookup.return_value = None  # linkmap miss
            atos = MagicMock()
            atos.lookup.return_value = "libsystem_c.dylib`malloc"
            r = self._make_resolver(d, linkmap=lm, atos=atos)
            sym = r.resolve(0x300)
            self.assertEqual(sym.name, "libsystem_c.dylib`malloc")
            self.assertEqual(sym.source, "atos")

    def test_hex_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            lm = MagicMock()
            lm.lookup.return_value = None
            atos = MagicMock()
            atos.lookup.return_value = "0x400"  # atos also miss
            r = self._make_resolver(d, linkmap=lm, atos=atos)
            sym = r.resolve(0x400)
            self.assertTrue(sym.name.startswith("0x"))
            self.assertEqual(sym.source, "unresolved")

    def test_warmup_not_done_returns_hex(self):
        from src.perf.locate.resolver import SymbolResolver
        with tempfile.TemporaryDirectory() as d:
            r = SymbolResolver(
                binary_path="/tmp/fake",
                dsym_paths=[],
                linkmap_path=None,
                cache_dir=Path(d),
            )
            sym = r.resolve(0x500)
            self.assertEqual(sym.source, "unresolved")

    def test_resolve_batch_returns_dict(self):
        with tempfile.TemporaryDirectory() as d:
            lm = MagicMock()
            lm.lookup.side_effect = lambda addr: f"sym_{addr:x}"
            r = self._make_resolver(d, linkmap=lm)
            result = r.resolve_batch([0x1, 0x2, 0x3])
            self.assertEqual(set(result.keys()), {0x1, 0x2, 0x3})
            self.assertEqual(result[0x1].name, "sym_1")

    def test_cache_populated_after_resolve(self):
        with tempfile.TemporaryDirectory() as d:
            lm = MagicMock()
            lm.lookup.return_value = "Func1()"
            r = self._make_resolver(d, linkmap=lm)
            r.resolve(0x600)
            # 再查一次应走 cache
            lm.lookup.reset_mock()
            r.resolve(0x600)
            lm.lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
```

**Step 4.2: 跑测试确认失败**

Run:
```bash
python3 -m unittest tests.test_locate_resolver -v 2>&1 | tail -15
```

Expected: 7 test 全 FAIL

**Step 4.3: 实现 SymbolResolver**

Create `src/perf/locate/resolver.py`：
```python
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
        dsym_paths: list[str],
        linkmap_path: Optional[str],
        cache_dir: Path,
    ):
        self.binary_path = binary_path
        self.dsym_paths = list(dsym_paths)
        self.linkmap_path = linkmap_path
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
            if self.linkmap_path:
                try:
                    from .linkmap import MultiLinkMap
                    lm = MultiLinkMap()
                    lm.load([self.linkmap_path])
                    self._linkmap = lm
                except (OSError, ValueError):
                    self._linkmap = None
            if self.binary_path and Path(self.binary_path).exists():
                try:
                    daemon = AtosDaemon(self.binary_path, load_addr=0)
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

        # ② linkmap bisect
        if self._linkmap is not None:
            try:
                sym = self._linkmap.lookup(addr)
                if sym:
                    self._cache.put(addr, sym)
                    return Symbol(sym, source="linkmap")
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

    def resolve_batch(self, addrs: list[int]) -> dict[int, Symbol]:
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
    def from_config(cls, cfg, repo_path: Path) -> Optional["SymbolResolver"]:
        """工厂 — 若 binary / linkmap / dsym 都发现不了则返回 None。

        调用者自行判空后决定是否注入 sampling。
        """
        # 入参 cfg 约定含 binary_path / linkmap_path / dsym_paths / cache_dir 字段
        binary = getattr(cfg, "binary_path", "") or ""
        linkmap = getattr(cfg, "linkmap_path", "") or None
        dsyms = list(getattr(cfg, "dsym_paths", []) or [])
        if not binary and not linkmap and not dsyms:
            return None
        cache_dir = Path(repo_path) / ".claude-parallel" / "locate_cache"
        return cls(
            binary_path=binary,
            dsym_paths=dsyms,
            linkmap_path=linkmap,
            cache_dir=cache_dir,
        )
```

**Step 4.4: 跑测试确认通过**

Run:
```bash
python3 -m unittest tests.test_locate_resolver -v 2>&1 | tail -20
```

Expected: `Ran 7 tests ... OK`

**Step 4.5: 回归**

Run:
```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
```

Expected: `Ran 64 tests ... OK`

**Step 4.6: 提交**

```bash
git add src/perf/locate/resolver.py tests/test_locate_resolver.py
git commit -m "feat(locate): SymbolResolver 统一入口 — cache→linkmap→atos→hex 三层"
```

---

## 阶段 B-W2: 集成到 sampling cycle（4 个原子任务）

### Task 5: session.py 构建 resolver + 异步 warmup

**Files:**
- Modify: `src/perf/session.py`（仅 `PerfSessionManager.start()` 附近 ~25 行）
- Test: `tests/test_session_resolver_wiring.py`

**Step 5.1: 查明 session.py 现有 start() 结构**

Run:
```bash
grep -n "def start" src/perf/session.py | head -10
grep -n "SamplingProfilerSidecar" src/perf/session.py | head -5
```

记录行号（例如 start 在 L54，sidecar 构造在 L200+）。

**Step 5.2: 写失败测试**

`tests/test_session_resolver_wiring.py`：
```python
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock


class SessionResolverWiringTest(unittest.TestCase):

    def test_start_creates_resolver_when_binary_configured(self):
        from src.perf.session import PerfSessionManager
        from src.perf import PerfConfig
        with tempfile.TemporaryDirectory() as d:
            cfg = PerfConfig(enabled=True, sampling_enabled=True)
            cfg.binary_path = "/tmp/fake_bin"
            mgr = PerfSessionManager(repo_path=Path(d), tag="test")
            with patch("src.perf.session.SymbolResolver") as R:
                instance = MagicMock()
                instance.warmup = MagicMock()
                R.from_config.return_value = instance
                # 只测 wiring,跳过真实采集
                mgr._wire_resolver(cfg)
                self.assertIs(mgr._resolver, instance)

    def test_shutdown_closes_resolver(self):
        from src.perf.session import PerfSessionManager
        with tempfile.TemporaryDirectory() as d:
            mgr = PerfSessionManager(repo_path=Path(d), tag="test")
            resolver = MagicMock()
            mgr._resolver = resolver
            mgr._teardown_resolver()
            resolver.shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
```

**Step 5.3: 跑测试确认失败**

Run:
```bash
python3 -m unittest tests.test_session_resolver_wiring -v 2>&1 | tail -10
```

Expected: 2 test FAIL（`_wire_resolver` / `_resolver` 不存在）

**Step 5.4: 最小实现 — 往 session.py 添加 wiring**

在 `src/perf/session.py` 顶部 import 区追加：
```python
from .locate.resolver import SymbolResolver
import threading as _threading
```

在 `PerfSessionManager.__init__` 内添加：
```python
self._resolver: Optional[SymbolResolver] = None
```

添加两个辅助方法到 `PerfSessionManager`（类尾部）：
```python
def _wire_resolver(self, cfg) -> None:
    """session.start() 调用 — 构建 resolver + 后台 warmup。"""
    self._resolver = SymbolResolver.from_config(cfg, self.repo_path)
    if self._resolver is not None:
        _threading.Thread(
            target=self._resolver.warmup,
            name="resolver-warmup",
            daemon=True,
        ).start()

def _teardown_resolver(self) -> None:
    """session.stop() 调用 — 关 daemon、flush cache。"""
    if self._resolver is not None:
        try:
            self._resolver.shutdown()
        except Exception:
            pass
        self._resolver = None
```

在 `start()` 方法内部，**sidecar 构造之前**插入：
```python
self._wire_resolver(cfg)
```

在 `stop()` 方法内部，**收尾前**插入：
```python
self._teardown_resolver()
```

**Step 5.5: 跑测试确认通过**

Run:
```bash
python3 -m unittest tests.test_session_resolver_wiring -v 2>&1 | tail -10
```

Expected: `Ran 2 tests ... OK`

**Step 5.6: 回归**

Run:
```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
```

Expected: `Ran 66 tests ... OK`

**Step 5.7: 提交**

```bash
git add src/perf/session.py tests/test_session_resolver_wiring.py
git commit -m "feat(locate): session.py 启动时构建 SymbolResolver + 异步 warmup"
```

---

### Task 6: sampling.py 调用 resolver.resolve_batch + JSONL source 字段

**Files:**
- Modify: `src/perf/sampling.py`（`SamplingProfilerSidecar.__init__` 与 cycle 内 aggregate 后）
- Test: `tests/test_sampling_symbolication.py`

**Step 6.1: 定位注入点**

Run:
```bash
grep -n "aggregate_top_n\|class SamplingProfilerSidecar\|def __init__\|append.*snapshot\|_append_snapshot" src/perf/sampling.py | head -20
```

记录 3 个位置：① sidecar `__init__` 签名；② cycle 里 `aggregate_top_n` 返回后；③ JSONL 写入处。

**Step 6.2: 写失败测试**

`tests/test_sampling_symbolication.py`：
```python
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock


class SamplingSymbolicationTest(unittest.TestCase):

    def test_enrich_top_with_resolver(self):
        from src.perf.sampling import _enrich_top_with_resolver
        resolver = MagicMock()
        from src.perf.locate.resolver import Symbol
        resolver.resolve_batch.return_value = {
            0x100: Symbol("Business.func()", "linkmap"),
            0x200: Symbol("0x200", "unresolved"),
        }
        top = [
            {"symbol": "0x100", "addr": 0x100, "samples": 5},
            {"symbol": "0x200", "addr": 0x200, "samples": 3},
        ]
        enriched = _enrich_top_with_resolver(top, resolver)
        self.assertEqual(enriched[0]["symbol"], "Business.func()")
        self.assertEqual(enriched[0]["source"], "linkmap")
        self.assertEqual(enriched[1]["source"], "unresolved")

    def test_enrich_handles_none_resolver(self):
        from src.perf.sampling import _enrich_top_with_resolver
        top = [{"symbol": "0x100", "addr": 0x100, "samples": 5}]
        enriched = _enrich_top_with_resolver(top, None)
        self.assertEqual(enriched[0]["source"], "unresolved")
        self.assertEqual(enriched[0]["symbol"], "0x100")

    def test_enrich_skips_entries_without_addr(self):
        from src.perf.sampling import _enrich_top_with_resolver
        resolver = MagicMock()
        resolver.resolve_batch.return_value = {}
        top = [{"symbol": "AlreadyNamed()", "samples": 5}]
        enriched = _enrich_top_with_resolver(top, resolver)
        self.assertEqual(enriched[0]["symbol"], "AlreadyNamed()")
        self.assertNotIn("source", enriched[0])


if __name__ == "__main__":
    unittest.main()
```

**Step 6.3: 跑测试确认失败**

Run:
```bash
python3 -m unittest tests.test_sampling_symbolication -v 2>&1 | tail -10
```

Expected: 3 test FAIL

**Step 6.4: 实现 `_enrich_top_with_resolver` + wire 进 cycle**

在 `src/perf/sampling.py` 里 — 在 `aggregate_top_n` 定义下方添加独立函数：
```python
def _enrich_top_with_resolver(top_entries: list[dict], resolver) -> list[dict]:
    """在 aggregate_top_n 产出后批量符号化。resolver 为 None 或 warmup 未完成时退化为 hex + source=unresolved。

    约定:每条 entry 应已带 addr 字段 (hex int)。无 addr 的条目保留原 symbol,不打 source 标签。
    """
    if not top_entries:
        return top_entries
    addrs = [e["addr"] for e in top_entries if isinstance(e.get("addr"), int)]
    if resolver is None or not addrs:
        for e in top_entries:
            if isinstance(e.get("addr"), int):
                e.setdefault("source", "unresolved")
        return top_entries

    resolved = resolver.resolve_batch(addrs)
    for e in top_entries:
        addr = e.get("addr")
        if not isinstance(addr, int):
            continue
        sym = resolved.get(addr)
        if sym is not None:
            e["symbol"] = sym.name
            e["source"] = sym.source
        else:
            e["source"] = "unresolved"
    return top_entries
```

在 `SamplingProfilerSidecar.__init__` 签名追加 `resolver=None` 参数 + 保存 `self._resolver = resolver`。

在 cycle 里 `aggregate_top_n(...)` 调用产出 `top` 后、`_append_snapshot` 前插入：
```python
top = _enrich_top_with_resolver(top, self._resolver)
```

在 `session.py` 里构造 sidecar 的位置（Task 5 已 import resolver），给 sidecar 构造追加 `resolver=self._resolver`。

**Step 6.5: 跑测试确认通过**

Run:
```bash
python3 -m unittest tests.test_sampling_symbolication -v 2>&1 | tail -10
```

Expected: `Ran 3 tests ... OK`

**Step 6.6: 回归**

Run:
```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
```

Expected: `Ran 69 tests ... OK`

**Step 6.7: 提交**

```bash
git add src/perf/sampling.py tests/test_sampling_symbolication.py
git commit -m "feat(locate): sampling cycle 调用 resolver.resolve_batch + JSONL source 字段"
```

---

### Task 7: 删除 resymbolize.py + symbolicate.py 改走 resolver

**Files:**
- Delete: `src/perf/resymbolize.py`
- Modify: `src/perf/symbolicate.py`（内部改用 resolver.resolve 替代裸 atos 调用；对外 API `auto_symbolicate` 签名不变）
- Modify: `src/perf/__init__.py`（移除 resymbolize 导出）
- Modify: `src/app/perf_cli.py`（移除 resymbolize 调用点）

**Step 7.1: 摸清 resymbolize 消费方**

Run:
```bash
grep -rn "from.*resymbolize\|import resymbolize\|resymbolize_" --include="*.py" .
```

记录所有引用点，逐一替换。

**Step 7.2: 把 resymbolize 的逻辑迁移到 resolver (如果还有残留场景)**

本次设计里 LinkMap 查询已在 resolver.② 中实现，resymbolize 的 `resymbolize_hotspot_items` / `resymbolize_cycle` 能力被吸收。消费方若直接调用这两个函数，改为：
- 命中场景：`resolver.resolve(addr)` — 一对一替换
- 批量场景：`resolver.resolve_batch(addrs)`

**Step 7.3: 更新消费方 (perf_cli.py)**

在 `src/app/perf_cli.py` 里 `cmd_perf_hotspots` 当前调用 resymbolize 的地方（通过 Step 7.1 grep 结果定位），改为走 resolver。若 session 未启动/resolver 不可得，保持现有的 hex 显示行为。

**Step 7.4: 改 symbolicate.py 公共函数内部实现**

`src/perf/symbolicate.py` 的 `symbolicate_addresses` / `auto_symbolicate` 保留公共签名，内部改为：
- 若 `resolver` 参数传入，走 `resolver.resolve_batch`
- 否则走原逻辑（兼容离线/独立调用）

**Step 7.5: 移除 perf/__init__.py 的 resymbolize 导出**

打开 `src/perf/__init__.py`，找到 `from .resymbolize import ...` 行（若有），删除。

**Step 7.6: 删除 resymbolize.py**

```bash
git rm src/perf/resymbolize.py
```

**Step 7.7: 回归测试**

Run:
```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
```

Expected: `Ran 69 tests ... OK`

烟测命令：
```bash
python3 -c "from src.perf import PerfConfig, PerfSessionManager; print('ok')"
python3 -c "from src.perf.symbolicate import auto_symbolicate; print('ok')"
python3 run.py --help 2>&1 | head -5
```

Expected: 全 `ok` + 帮助文本正常

**Step 7.8: 提交**

```bash
git add src/perf/symbolicate.py src/perf/__init__.py src/app/perf_cli.py
git rm src/perf/resymbolize.py  # 若未 staged
git commit -m "refactor(locate): 删除 resymbolize.py + symbolicate 改走 SymbolResolver"
```

---

### Task 8: 真机冒烟验证（达成 §1 B1/B2/B3 成功标准）

**Files:** 不改代码，只跑冒烟

**Step 8.1: 环境检查**

Run:
```bash
xcrun xctrace list devices 2>&1 | head -5
cpar perf devices 2>&1 | head -10
```

Expected: 看到目标 iOS 真机 UDID。

**Step 8.2: 跑 sampling，观察业务符号命中**

开两个终端。终端 1：
```bash
cpar perf start --repo . --tag locate-smoke \
  --device <UDID> --attach Soul_New \
  --sampling --metrics-source device --battery-interval 5
```

等 30s（3 个 cycle）。终端 2：
```bash
cpar perf hotspots --repo . --tag locate-smoke --aggregate
```

**验收标准**
- ✅ B1：Top-20 中 ≥18 条 `symbol` 字段是业务函数名（非 `0x...` 开头）
- ✅ B2：查看 `hotspots.jsonl`，每条有 `source` 字段，取值 ∈ {`cache`, `linkmap`, `atos`, `unresolved`}
- ✅ B3：`cpar perf symbolicate` 子命令仍可运行（`--help` 可见）

**Step 8.3: cycle 时延观察**

```bash
grep -c "cycle complete" .claude-parallel/perf/locate-smoke/sampling.log | head -3
# 30s 内应见 3 cycle；若 cycle 数 <3 说明 cycle 拖慢
```

**验收标准**
- ✅ 30s 窗口内 ≥3 cycle（cycle 中位数 ≤10s，符合 <12% 涨幅约束）

**Step 8.4: 清理**

```bash
cpar perf stop --repo . --tag locate-smoke --clean
```

**Step 8.5: 合并前 checklist**

| 项 | 状态 |
|---|------|
| 单元测试全绿 | `python3 -m unittest discover -s tests -q` |
| 真机冒烟 B1 | ≥90% 业务符号命中 |
| 真机冒烟 B2 | cycle 时延 <9s |
| CLI 回归 | `cpar perf start/stop/hotspots/symbolicate/report` 各跑一次 |

**Step 8.6: 合流准备（待 Track A A-W4.2 到来）**

此时 Track B 的 `src/perf/locate/` 已完整，准备好迎接 Track A 的 `symbolicate.py` 拆分（Track A 会往 `locate/` 里加 `dsym.py` 新文件，不动 Track B 已有文件）。

打 tag：
```bash
git tag track-b-ready
git log --oneline -10
```

**Step 8.7: 提交最终里程碑**

```bash
git commit --allow-empty -m "chore(locate): Track B 完成 — hotspots 业务符号命中 >90%,cycle <9s"
```

---

## 完成标志

```bash
# Track B 所有任务完成后应满足:
git log --oneline | head -12  # 8 个 feat/refactor commit
python3 -m unittest discover -s tests -q  # Ran 69+ tests OK
ls src/perf/locate/  # __init__ linkmap.py atos.py cache.py resolver.py
test ! -e src/perf/resymbolize.py  # 已删除
```
