"""A-W4 Task 14 — capture/ + decode/ 子包迁移回归测试。

验证 7 个模块既可从新路径访问,也可从旧路径 shim 访问。
"""

import unittest


class CaptureDecodePackagesTest(unittest.TestCase):

    # ── capture/ 层新路径 ──

    def test_sampling_from_new_path(self):
        from src.perf.capture.sampling import SamplingProfilerSidecar
        self.assertTrue(callable(SamplingProfilerSidecar))

    def test_webcontent_from_new_path(self):
        from src.perf.capture.webcontent import WebContentProfiler
        self.assertTrue(callable(WebContentProfiler))

    def test_live_metrics_from_new_path(self):
        from src.perf.capture.live_metrics import LiveMetricsStreamer
        self.assertTrue(callable(LiveMetricsStreamer))

    def test_live_log_from_new_path(self):
        from src.perf.capture.live_log import LiveLogAnalyzer
        self.assertTrue(callable(LiveLogAnalyzer))

    # ── decode/ 层新路径 ──

    def test_templates_from_new_path(self):
        from src.perf.decode.templates import BUILTIN_TEMPLATES, build_xctrace_record_cmd
        self.assertTrue(callable(build_xctrace_record_cmd))
        self.assertIsNotNone(BUILTIN_TEMPLATES)

    def test_deep_export_from_new_path(self):
        from src.perf.decode.deep_export import export_deep_schema, deep_export_all
        self.assertTrue(callable(export_deep_schema))
        self.assertTrue(callable(deep_export_all))

    def test_time_sync_from_new_path(self):
        from src.perf.decode.time_sync import align_timelines, run_time_sync
        self.assertTrue(callable(align_timelines))
        self.assertTrue(callable(run_time_sync))

    # ── 老路径 shim 验证 (7 个) ──

    def test_sampling_shim(self):
        from src.perf.sampling import SamplingProfilerSidecar
        self.assertTrue(callable(SamplingProfilerSidecar))

    def test_webcontent_shim(self):
        from src.perf.webcontent import WebContentProfiler
        self.assertTrue(callable(WebContentProfiler))

    def test_live_metrics_shim(self):
        from src.perf.live_metrics import LiveMetricsStreamer
        self.assertTrue(callable(LiveMetricsStreamer))

    def test_live_log_shim(self):
        from src.perf.live_log import LiveLogAnalyzer
        self.assertTrue(callable(LiveLogAnalyzer))

    def test_templates_shim(self):
        from src.perf.templates import BUILTIN_TEMPLATES, build_xctrace_record_cmd
        self.assertIsNotNone(BUILTIN_TEMPLATES)
        self.assertTrue(callable(build_xctrace_record_cmd))

    def test_deep_export_shim(self):
        from src.perf.deep_export import export_deep_schema
        self.assertTrue(callable(export_deep_schema))

    def test_time_sync_shim(self):
        from src.perf.time_sync import align_timelines
        self.assertTrue(callable(align_timelines))


if __name__ == "__main__":
    unittest.main()
