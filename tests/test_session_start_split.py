"""Task 35: session.start() 拆成 7 个 _start_* 子方法 + 文件 <400 行."""
import unittest


class SessionStartSplitTest(unittest.TestCase):

    def test_start_has_init_meta_helper(self):
        from src.perf.session import PerfSessionManager
        self.assertTrue(hasattr(PerfSessionManager, "_init_meta"))

    def test_start_has_syslog_starter(self):
        from src.perf.session import PerfSessionManager
        self.assertTrue(hasattr(PerfSessionManager, "_start_syslog"))

    def test_start_has_battery_starter(self):
        from src.perf.session import PerfSessionManager
        self.assertTrue(hasattr(PerfSessionManager, "_start_battery"))

    def test_start_has_dvt_starter(self):
        from src.perf.session import PerfSessionManager
        self.assertTrue(hasattr(PerfSessionManager, "_start_dvt"))

    def test_start_has_sampling_starter(self):
        from src.perf.session import PerfSessionManager
        self.assertTrue(hasattr(PerfSessionManager, "_start_sampling"))

    def test_session_file_under_400_lines(self):
        from pathlib import Path
        p = Path("src/perf/session.py")
        n = len(p.read_text().splitlines())
        self.assertLess(n, 400, f"session.py currently {n} lines, target <400")


if __name__ == "__main__":
    unittest.main()
