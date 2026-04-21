import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock


class SamplingSymbolicationTest(unittest.TestCase):

    def test_enrich_top_with_resolver(self):
        from src.perf.sampling import _enrich_top_with_resolver
        from src.perf.locate.resolver import Symbol
        resolver = MagicMock()
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
        # symbol 字段:resolver 返回 "0x200" (unresolved) → entry.symbol 更新为 "0x200" 保持一致
        self.assertEqual(enriched[1]["symbol"], "0x200")

    def test_enrich_handles_none_resolver(self):
        """resolver 为 None 时退化为标 unresolved + 保留原 symbol。"""
        from src.perf.sampling import _enrich_top_with_resolver
        top = [{"symbol": "0x100", "addr": 0x100, "samples": 5}]
        enriched = _enrich_top_with_resolver(top, None)
        self.assertEqual(enriched[0]["source"], "unresolved")
        self.assertEqual(enriched[0]["symbol"], "0x100")

    def test_enrich_skips_entries_without_addr(self):
        """无 addr 字段的 entry (旧格式) 不参与批量查询,保留原 symbol,不打 source 标签。"""
        from src.perf.sampling import _enrich_top_with_resolver
        resolver = MagicMock()
        resolver.resolve_batch.return_value = {}
        top = [{"symbol": "AlreadyNamed()", "samples": 5}]
        enriched = _enrich_top_with_resolver(top, resolver)
        self.assertEqual(enriched[0]["symbol"], "AlreadyNamed()")
        self.assertNotIn("source", enriched[0])

    def test_enrich_preserves_order_and_other_fields(self):
        """enrich 不能打乱顺序,不能丢 samples/pct 等字段。"""
        from src.perf.sampling import _enrich_top_with_resolver
        from src.perf.locate.resolver import Symbol
        resolver = MagicMock()
        resolver.resolve_batch.return_value = {
            0x100: Symbol("A()", "linkmap"),
            0x200: Symbol("B()", "linkmap"),
        }
        top = [
            {"symbol": "0x100", "addr": 0x100, "samples": 10, "pct": 50.0},
            {"symbol": "0x200", "addr": 0x200, "samples": 5, "pct": 25.0},
        ]
        enriched = _enrich_top_with_resolver(top, resolver)
        self.assertEqual(enriched[0]["symbol"], "A()")
        self.assertEqual(enriched[0]["samples"], 10)
        self.assertEqual(enriched[0]["pct"], 50.0)
        self.assertEqual(enriched[1]["symbol"], "B()")
        self.assertEqual(enriched[1]["samples"], 5)


if __name__ == "__main__":
    unittest.main()
