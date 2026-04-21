"""验证 aggregate_top_n 的 dyld overhead 过滤开关。"""

import unittest


class OverheadFilterTest(unittest.TestCase):

    def test_overhead_filtered_by_default(self):
        from src.perf.decode.timeprofiler import aggregate_top_n
        samples = [
            ("dyld3::MachOLoaded::findClosestSymbol(...)", 1000.0, "0x18e87edb5"),
            ("-[SOFeedController loadData]", 200.0, "0x102934465"),
            ("-[SOMessageView render]", 150.0, "0x103000000"),
        ]
        top = aggregate_top_n(samples, top_n=5)
        # dyld overhead 不应出现在 Top
        self.assertFalse(any("findClosestSymbol" in e["symbol"] for e in top))
        # 业务符号命中榜首
        self.assertIn("SOFeedController", top[0]["symbol"])

    def test_overhead_preserved_when_filter_off(self):
        from src.perf.decode.timeprofiler import aggregate_top_n
        samples = [
            ("dyld3::MachOLoaded::findClosestSymbol(...)", 1000.0, "0x18e87edb5"),
            ("-[SOFeedController loadData]", 200.0, "0x102934465"),
        ]
        top = aggregate_top_n(samples, top_n=5, filter_overhead=False)
        self.assertTrue(any("findClosestSymbol" in e["symbol"] for e in top))

    def test_pct_rebased_after_filter(self):
        """过滤 overhead 后 pct 基于剩余 weight, 业务代码 pct 应显著提升。"""
        from src.perf.decode.timeprofiler import aggregate_top_n
        samples = [
            ("dyld3::MachOLoaded::findClosestSymbol(...)", 1000.0, "0x18e87edb5"),
            ("-[App foo]", 100.0, "0x102000000"),
            ("-[App bar]", 100.0, "0x103000000"),
        ]
        top = aggregate_top_n(samples, top_n=5)
        # foo/bar 各占剩余 200 的一半 = 50%
        self.assertEqual(top[0]["pct"], 50.0)
        self.assertEqual(top[1]["pct"], 50.0)

    def test_multiple_overhead_patterns(self):
        """覆盖 dyld3/dyld4/dyld 三种版本命名。"""
        from src.perf.decode.timeprofiler import aggregate_top_n
        samples = [
            ("dyld3::MachOLoaded::findClosestSymbol(a,b,c)", 100.0, "0x1"),
            ("dyld4::MachOLoaded::findClosestSymbol(x)", 100.0, "0x2"),
            ("dyld::MachOLoaded::findClosestSymbol()", 100.0, "0x3"),
            ("-[App realFunc]", 1.0, "0x100"),
        ]
        top = aggregate_top_n(samples, top_n=5)
        self.assertEqual(len(top), 1)
        self.assertIn("realFunc", top[0]["symbol"])


if __name__ == "__main__":
    unittest.main()
