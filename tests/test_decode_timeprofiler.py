"""Tests for src.perf.decode.timeprofiler — 确认解析函数从 decode/ 可导入,
同时 capture/sampling 保留 re-export 兼容性。
"""

import unittest


class DecodeTimeprofilerTest(unittest.TestCase):

    def test_parse_from_decode(self):
        """parse_timeprofiler_xml 应可从 decode.timeprofiler 导入。"""
        from src.perf.decode.timeprofiler import parse_timeprofiler_xml
        self.assertTrue(callable(parse_timeprofiler_xml))

    def test_aggregate_from_decode(self):
        from src.perf.decode.timeprofiler import aggregate_top_n
        self.assertTrue(callable(aggregate_top_n))

    def test_export_from_decode(self):
        from src.perf.decode.timeprofiler import export_xctrace_schema
        self.assertTrue(callable(export_xctrace_schema))

    def test_sampling_still_exposes_parse_for_compat(self):
        """capture/sampling.py 保留 re-export,避免一次性改所有消费方。"""
        from src.perf.capture.sampling import parse_timeprofiler_xml
        self.assertTrue(callable(parse_timeprofiler_xml))

    def test_sampling_still_exposes_aggregate_for_compat(self):
        from src.perf.capture.sampling import aggregate_top_n
        self.assertTrue(callable(aggregate_top_n))

    def test_sampling_still_exposes_export_for_compat(self):
        from src.perf.capture.sampling import export_xctrace_schema
        self.assertTrue(callable(export_xctrace_schema))


if __name__ == "__main__":
    unittest.main()
