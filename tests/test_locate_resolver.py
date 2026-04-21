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
