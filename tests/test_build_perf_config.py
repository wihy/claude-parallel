"""build_perf_config_from_args factory: 确保 run/resume 路径也能激活 resolver。

前置补丁 (9d9c3aa) 只接通了 `cpar perf start` 子命令路径。本测试锁定:
`cpar run --with-perf` 主编排路径下,用户通过 --perf-binary/linkmap/dsym 传入
的字段能被 factory 正确映射到 PerfConfig。
"""

import argparse
import unittest


class BuildPerfConfigTest(unittest.TestCase):

    def test_factory_maps_binary_linkmap_dsym(self):
        """factory 应把 args.perf_binary / perf_linkmap / perf_dsym 映射到 cfg。

        argparse 用 action="append" 让 --perf-linkmap 和 --perf-dsym 都为 list。
        """
        from src.app.cli import build_perf_config_from_args

        args = argparse.Namespace()
        args.perf_binary = "/tmp/Soul"
        args.perf_linkmap = ["/tmp/Soul-LinkMap.txt", "/tmp/Soul.dylib-LinkMap.txt"]
        args.perf_dsym = ["/tmp/Soul.app.dSYM"]

        cfg = build_perf_config_from_args(args)

        self.assertEqual(cfg.binary_path, "/tmp/Soul")
        self.assertEqual(cfg.linkmap_paths, ["/tmp/Soul-LinkMap.txt", "/tmp/Soul.dylib-LinkMap.txt"])
        self.assertEqual(cfg.linkmap_path, "/tmp/Soul-LinkMap.txt")  # 单数 property
        self.assertEqual(cfg.dsym_paths, ["/tmp/Soul.app.dSYM"])

    def test_factory_empty_defaults_when_args_missing(self):
        """args 没这些字段时,factory 应回退到空值 (向后兼容,getattr 默认生效)。"""
        from src.app.cli import build_perf_config_from_args

        args = argparse.Namespace()

        cfg = build_perf_config_from_args(args)

        self.assertEqual(cfg.binary_path, "")
        self.assertEqual(cfg.linkmap_paths, [])
        self.assertEqual(cfg.linkmap_path, "")  # property 空 list 返回 ""
        self.assertEqual(cfg.dsym_paths, [])


if __name__ == "__main__":
    unittest.main()
