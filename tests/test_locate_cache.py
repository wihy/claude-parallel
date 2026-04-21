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
