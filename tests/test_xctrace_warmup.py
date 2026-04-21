"""xctrace 首 cycle warmup 机制测试 (Fix 7)。

真机场景: xctrace record --attach 首次连接设备需要 10-20s (cold start),
常触发 subprocess timeout. 原实现下这会立刻累计 consecutive_failures,
连续 3 次就 MAX_CONSECUTIVE_FAILURES 自停 session. 用户看到的现象:
采了个寂寞, 全被判失败自动停.

Fix 7: 首 WARMUP_CYCLES=1 个 cycle 的失败不计入 consecutive_failures,
给冷启动一次"练手"机会. 失败后加 FAILURE_BACKOFF_SEC 延迟给设备喘息.
"""

import unittest


class XctraceWarmupTest(unittest.TestCase):

    def test_warmup_constant_exists(self):
        """WARMUP_CYCLES 默认 1 (只豁免首 cycle),避免过度宽松。"""
        from src.perf.capture.sampling import SamplingProfilerSidecar
        self.assertEqual(SamplingProfilerSidecar.WARMUP_CYCLES, 1)

    def test_failure_backoff_reasonable(self):
        """FAILURE_BACKOFF_SEC 2s 量级 (太长会拖慢节奏)。"""
        from src.perf.capture.sampling import SamplingProfilerSidecar
        self.assertGreaterEqual(SamplingProfilerSidecar.FAILURE_BACKOFF_SEC, 1.0)
        self.assertLessEqual(SamplingProfilerSidecar.FAILURE_BACKOFF_SEC, 5.0)

    def test_max_failures_still_3(self):
        """MAX_CONSECUTIVE_FAILURES 不动 (保持自保护阈值)。"""
        from src.perf.capture.sampling import SamplingProfilerSidecar
        self.assertEqual(SamplingProfilerSidecar.MAX_CONSECUTIVE_FAILURES, 3)


if __name__ == "__main__":
    unittest.main()
