"""iOS Debug build .debug.dylib 自动识别。"""

import tempfile
import unittest
from pathlib import Path


class IosBinaryResolutionTest(unittest.TestCase):

    def test_app_dir_picks_debug_dylib(self):
        """传入 Soul_New.app 目录, 自动选内部 Soul_New.debug.dylib。"""
        from src.perf.locate.resolver import _resolve_ios_debug_binary
        with tempfile.TemporaryDirectory() as d:
            app = Path(d) / "Soul_New.app"
            app.mkdir()
            launcher = app / "Soul_New"
            launcher.write_bytes(b"\xcafe\xba\xbe")  # fake Mach-O
            dylib = app / "Soul_New.debug.dylib"
            dylib.write_bytes(b"\xcafe\xba\xbe")
            resolved = _resolve_ios_debug_binary(str(app))
            self.assertEqual(resolved, dylib)

    def test_app_dir_fallback_to_main_binary_when_no_dylib(self):
        """Release build (无 .debug.dylib) 回退到主 binary。"""
        from src.perf.locate.resolver import _resolve_ios_debug_binary
        with tempfile.TemporaryDirectory() as d:
            app = Path(d) / "Soul.app"
            app.mkdir()
            main_bin = app / "Soul"
            main_bin.write_bytes(b"\xcafe\xba\xbe")
            resolved = _resolve_ios_debug_binary(str(app))
            self.assertEqual(resolved, main_bin)

    def test_launcher_stub_with_sibling_dylib(self):
        """传入 launcher stub (Soul_New) 但同目录下有 Soul_New.debug.dylib → 选 dylib。"""
        from src.perf.locate.resolver import _resolve_ios_debug_binary
        with tempfile.TemporaryDirectory() as d:
            launcher = Path(d) / "Soul_New"
            launcher.write_bytes(b"\xcafe\xba\xbe")
            dylib = Path(d) / "Soul_New.debug.dylib"
            dylib.write_bytes(b"\xcafe\xba\xbe")
            resolved = _resolve_ios_debug_binary(str(launcher))
            self.assertEqual(resolved, dylib)

    def test_plain_binary_passes_through(self):
        """既不是 .app 目录也无 .debug.dylib 兄弟 → 原样返回。"""
        from src.perf.locate.resolver import _resolve_ios_debug_binary
        with tempfile.TemporaryDirectory() as d:
            bin_path = Path(d) / "my_binary"
            bin_path.write_bytes(b"\xcafe\xba\xbe")
            resolved = _resolve_ios_debug_binary(str(bin_path))
            self.assertEqual(resolved, bin_path)

    def test_nonexistent_returns_none(self):
        """不存在的路径返回 None (resolver warmup 会 silent skip)。"""
        from src.perf.locate.resolver import _resolve_ios_debug_binary
        self.assertIsNone(_resolve_ios_debug_binary("/tmp/definitely_not_exist_xyz"))

    def test_empty_app_dir_returns_none(self):
        """空 .app 目录 (既无 dylib 也无 main binary) 返回 None。"""
        from src.perf.locate.resolver import _resolve_ios_debug_binary
        with tempfile.TemporaryDirectory() as d:
            app = Path(d) / "Empty.app"
            app.mkdir()
            self.assertIsNone(_resolve_ios_debug_binary(str(app)))


if __name__ == "__main__":
    unittest.main()
