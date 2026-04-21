import unittest


class LocatePackageTest(unittest.TestCase):
    def test_multilinkmap_import_from_new_path(self):
        from src.perf.locate.linkmap import MultiLinkMap, LinkMap, Symbol
        self.assertTrue(callable(MultiLinkMap))
        self.assertTrue(callable(LinkMap))
        self.assertTrue(callable(Symbol))

    def test_multilinkmap_import_from_old_path_still_works(self):
        # 过渡期兼容 — 应该能从老路径导入 (shim 转发)
        from src.perf.linkmap import MultiLinkMap
        self.assertTrue(callable(MultiLinkMap))

    def test_locate_package_exports(self):
        from src.perf import locate
        self.assertTrue(hasattr(locate, 'linkmap'))


if __name__ == "__main__":
    unittest.main()
