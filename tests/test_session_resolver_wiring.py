import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock


class SessionResolverWiringTest(unittest.TestCase):
    """Task 5: PerfSessionManager 接入 SymbolResolver 生命周期。"""

    def _make_mgr(self, d):
        from src.perf.session import PerfSessionManager
        from src.perf import PerfConfig
        cfg = PerfConfig(enabled=True, sampling_enabled=True, tag="test")
        # PerfConfig 已有 binary_path/linkmap_paths/dsym_paths 字段 (v0.4.0+)
        cfg.binary_path = "/tmp/fake_bin"
        cfg.linkmap_paths = []
        cfg.dsym_paths = []
        return PerfSessionManager(repo=str(d), coordination_dir=".claude-parallel", config=cfg), cfg

    def test_wire_resolver_builds_from_config(self):
        """_wire_resolver 调用 SymbolResolver.from_config 并启动 warmup 后台线程。"""
        with tempfile.TemporaryDirectory() as d:
            mgr, cfg = self._make_mgr(d)
            with patch("src.perf.session.SymbolResolver") as R:
                instance = MagicMock()
                instance.warmup = MagicMock()
                R.from_config.return_value = instance

                mgr._wire_resolver(cfg)

                self.assertIs(mgr._resolver, instance)
                R.from_config.assert_called_once()

    def test_wire_resolver_accepts_none(self):
        """from_config 返回 None 时 _resolver 保持 None,不崩。"""
        with tempfile.TemporaryDirectory() as d:
            mgr, cfg = self._make_mgr(d)
            with patch("src.perf.session.SymbolResolver") as R:
                R.from_config.return_value = None
                mgr._wire_resolver(cfg)
                self.assertIsNone(mgr._resolver)

    def test_teardown_resolver_calls_shutdown(self):
        """_teardown_resolver 调 shutdown + 置 None。"""
        with tempfile.TemporaryDirectory() as d:
            mgr, _ = self._make_mgr(d)
            resolver = MagicMock()
            mgr._resolver = resolver

            mgr._teardown_resolver()

            resolver.shutdown.assert_called_once()
            self.assertIsNone(mgr._resolver)

    def test_teardown_resolver_safe_when_none(self):
        """_teardown_resolver 在 _resolver 为 None 时不 raise。"""
        with tempfile.TemporaryDirectory() as d:
            mgr, _ = self._make_mgr(d)
            mgr._resolver = None
            mgr._teardown_resolver()  # 不应 raise
            self.assertIsNone(mgr._resolver)


if __name__ == "__main__":
    unittest.main()
