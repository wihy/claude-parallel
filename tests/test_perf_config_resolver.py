import unittest
from pathlib import Path


class PerfConfigResolverTest(unittest.TestCase):

    def test_perfconfig_has_resolver_fields(self):
        """PerfConfig 应有 binary_path / linkmap_path / dsym_paths 3 个字段。"""
        from src.perf import PerfConfig
        cfg = PerfConfig(enabled=True)
        # 允许默认值为 "" / [] (不强求具体值,只求字段存在)
        self.assertTrue(hasattr(cfg, "binary_path"))
        self.assertTrue(hasattr(cfg, "linkmap_path"))
        self.assertTrue(hasattr(cfg, "dsym_paths"))
        # 默认值应可转为 falsy (空字符串 / 空 list)
        self.assertFalse(cfg.binary_path)
        self.assertFalse(cfg.linkmap_path)
        self.assertFalse(cfg.dsym_paths)

    def test_perfconfig_accepts_resolver_fields_via_kwargs(self):
        """PerfConfig(binary_path=..., linkmap_path=..., dsym_paths=...) 构造可用。"""
        from src.perf import PerfConfig
        cfg = PerfConfig(
            enabled=True,
            binary_path="/tmp/fake/Soul",
            linkmap_path="/tmp/fake/Soul-LinkMap.txt",
            dsym_paths=["/tmp/fake/Soul.app.dSYM"],
        )
        self.assertEqual(cfg.binary_path, "/tmp/fake/Soul")
        self.assertEqual(cfg.linkmap_path, "/tmp/fake/Soul-LinkMap.txt")
        self.assertEqual(cfg.dsym_paths, ["/tmp/fake/Soul.app.dSYM"])

    def test_symbol_resolver_from_config_activates_with_binary(self):
        """配了 binary_path 后 from_config 应返回 SymbolResolver (非 None)。"""
        from src.perf import PerfConfig
        from src.perf.locate.resolver import SymbolResolver
        import tempfile
        cfg = PerfConfig(enabled=True, binary_path="/tmp/any_path")
        with tempfile.TemporaryDirectory() as d:
            r = SymbolResolver.from_config(cfg, Path(d))
            self.assertIsNotNone(r)
            self.assertEqual(r.binary_path, "/tmp/any_path")

    def test_symbol_resolver_from_config_returns_none_when_empty(self):
        """所有 3 字段都空时,from_config 应返回 None (保持现有行为不变)。"""
        from src.perf import PerfConfig
        from src.perf.locate.resolver import SymbolResolver
        import tempfile
        cfg = PerfConfig(enabled=True)
        with tempfile.TemporaryDirectory() as d:
            r = SymbolResolver.from_config(cfg, Path(d))
            self.assertIsNone(r)


if __name__ == "__main__":
    unittest.main()
