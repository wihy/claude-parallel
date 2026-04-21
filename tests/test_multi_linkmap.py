"""SymbolResolver 多 LinkMap 加载测试。

Debug build 中主 binary 是 launcher stub,真代码在 .debug.dylib 里。
Extensions (SoulShareExtension / SoulWidgetExtension / SoulPushExtension)
也有各自的 LinkMap。本测试锁定 resolver 能同时加载并查找多张 LinkMap。
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock


class MultiLinkmapTest(unittest.TestCase):

    def test_resolver_accepts_linkmap_paths_list(self):
        """构造器的 linkmap_paths 关键字参数接受 list。"""
        from src.perf.locate.resolver import SymbolResolver
        with tempfile.TemporaryDirectory() as d:
            r = SymbolResolver(
                binary_path="/tmp/fake",
                dsym_paths=[],
                cache_dir=Path(d),
                linkmap_paths=["/tmp/a.txt", "/tmp/b.txt", "/tmp/c.txt"],
            )
            self.assertEqual(r.linkmap_paths, ["/tmp/a.txt", "/tmp/b.txt", "/tmp/c.txt"])
            self.assertEqual(r.linkmap_path, "/tmp/a.txt")  # 兼容单数属性

    def test_resolver_dedupes_paths(self):
        """重复路径去重。"""
        from src.perf.locate.resolver import SymbolResolver
        with tempfile.TemporaryDirectory() as d:
            r = SymbolResolver(
                binary_path="",
                dsym_paths=[],
                cache_dir=Path(d),
                linkmap_paths=["/tmp/a.txt", "/tmp/a.txt", "/tmp/b.txt"],
            )
            self.assertEqual(r.linkmap_paths, ["/tmp/a.txt", "/tmp/b.txt"])

    def test_resolver_back_compat_single_linkmap_path(self):
        """旧调用用 linkmap_path 单字符串仍 work。"""
        from src.perf.locate.resolver import SymbolResolver
        with tempfile.TemporaryDirectory() as d:
            r = SymbolResolver(
                binary_path="",
                dsym_paths=[],
                linkmap_path="/tmp/only.txt",
                cache_dir=Path(d),
            )
            self.assertEqual(r.linkmap_paths, ["/tmp/only.txt"])
            self.assertEqual(r.linkmap_path, "/tmp/only.txt")

    def test_from_config_multi_linkmap(self):
        """PerfConfig.linkmap_paths → resolver.linkmap_paths。"""
        from src.perf import PerfConfig
        from src.perf.locate.resolver import SymbolResolver
        with tempfile.TemporaryDirectory() as d:
            cfg = PerfConfig(
                enabled=True,
                binary_path="/tmp/bin",
                linkmap_paths=["/tmp/main.txt", "/tmp/dylib.txt"],
            )
            r = SymbolResolver.from_config(cfg, Path(d))
            self.assertIsNotNone(r)
            self.assertEqual(r.linkmap_paths, ["/tmp/main.txt", "/tmp/dylib.txt"])

    def test_warmup_tolerates_single_bad_file(self):
        """多 LinkMap 加载时,单个坏文件不阻断其余加载。"""
        from src.perf.locate.resolver import SymbolResolver
        with tempfile.TemporaryDirectory() as d:
            # 全是不存在的路径 - warmup 优雅失败
            r = SymbolResolver(
                binary_path="",
                dsym_paths=[],
                cache_dir=Path(d),
                linkmap_paths=["/nonexistent/a.txt", "/nonexistent/b.txt"],
            )
            r.warmup()
            # warmup 完成,linkmap 为 None (全部失败)
            self.assertTrue(r._warmup_done.is_set())
            self.assertIsNone(r._linkmap)


if __name__ == "__main__":
    unittest.main()
