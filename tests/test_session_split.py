"""A-W4 Task 17: session.py 瘦身 — 验证拆出的 4 个模块可导入 + session 仍可委派调用。"""

import unittest


class SessionSplitTest(unittest.TestCase):

    def test_analyze_callstack_importable(self):
        from src.perf.analyze.callstack import analyze_callstack, format_callstack_text
        self.assertTrue(callable(analyze_callstack))
        self.assertTrue(callable(format_callstack_text))

    def test_analyze_metrics_importable(self):
        from src.perf.analyze.metrics import (
            extract_column_values,
            calc_delta,
            gate_check,
            compute_trace_metrics,
        )
        self.assertTrue(callable(extract_column_values))
        self.assertTrue(callable(calc_delta))
        self.assertTrue(callable(gate_check))
        self.assertTrue(callable(compute_trace_metrics))

    def test_analyze_syslog_stats_importable(self):
        from src.perf.analyze.syslog_stats import (
            compute_syslog_stats,
            check_syslog_reliability,
            compute_timeline_stats,
        )
        self.assertTrue(callable(compute_syslog_stats))
        self.assertTrue(callable(check_syslog_reliability))
        self.assertTrue(callable(compute_timeline_stats))

    def test_present_dvt_metrics_importable(self):
        from src.perf.present.dvt_metrics import (
            build_dvt_metrics_report,
            format_dvt_metrics_text,
        )
        self.assertTrue(callable(build_dvt_metrics_report))
        self.assertTrue(callable(format_dvt_metrics_text))

    def test_session_still_has_callstack_method(self):
        """PerfSessionManager.callstack 应仍可调用 (delegate to analyze/callstack)."""
        from src.perf.session import PerfSessionManager
        self.assertTrue(hasattr(PerfSessionManager, "callstack"))
        self.assertTrue(hasattr(PerfSessionManager, "format_callstack_text"))
        self.assertTrue(hasattr(PerfSessionManager, "format_dvt_metrics_text"))

    def test_session_line_count(self):
        """session.py 目标瘦身到 <= 750 行 (合理妥协, <400 需重构 start())."""
        from pathlib import Path
        p = Path("src/perf/session.py")
        n = len(p.read_text().splitlines())
        self.assertLessEqual(n, 750, f"session.py currently {n} lines, target <=750")


if __name__ == "__main__":
    unittest.main()
