import unittest


class LocatePackageTest(unittest.TestCase):
    def test_multilinkmap_import_from_new_path(self):
        from src.perf.locate.linkmap import MultiLinkMap, LinkMap, Symbol
        self.assertTrue(callable(MultiLinkMap))
        self.assertTrue(callable(LinkMap))
        self.assertTrue(callable(Symbol))

    def test_locate_package_exports(self):
        from src.perf import locate
        self.assertTrue(hasattr(locate, 'linkmap'))


if __name__ == "__main__":
    unittest.main()
