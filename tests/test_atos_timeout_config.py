"""AtosDaemon 可配置 read timeout 测试 (Fix 6)。

真机场景: 大 .debug.dylib (506 MB) 深 offset 地址, atos 解析需要 >200ms。
原始默认 200ms 会让这类地址被错误标记为 miss, 业务符号丢失。
Fix 6: 默认提升到 500ms + 构造器可覆盖。
"""

import unittest


class AtosTimeoutConfigTest(unittest.TestCase):

    def test_default_timeout_is_500ms(self):
        """默认 timeout 对齐 resolver 外层 DEFAULT_TIMEOUT_MS。"""
        from src.perf.locate.atos import AtosDaemon, ATOS_READ_TIMEOUT_SEC
        self.assertEqual(ATOS_READ_TIMEOUT_SEC, 0.5)
        d = AtosDaemon(binary_path="/tmp/fake")
        self.assertEqual(d.read_timeout_sec, 0.5)

    def test_custom_timeout_via_kwarg(self):
        """构造器可传入自定义 timeout (压力测试 / 大 binary 场景)。"""
        from src.perf.locate.atos import AtosDaemon
        d = AtosDaemon(binary_path="/tmp/fake", read_timeout_sec=1.0)
        self.assertEqual(d.read_timeout_sec, 1.0)

    def test_none_falls_back_to_default(self):
        """显式传 None 走默认值 (等价不传)。"""
        from src.perf.locate.atos import AtosDaemon, ATOS_READ_TIMEOUT_SEC
        d = AtosDaemon(binary_path="/tmp/fake", read_timeout_sec=None)
        self.assertEqual(d.read_timeout_sec, ATOS_READ_TIMEOUT_SEC)

    def test_timeout_actually_used_in_lookup(self):
        """lookup 实际用 self.read_timeout_sec, 非写死常量。"""
        import queue
        from unittest.mock import MagicMock
        from src.perf.locate.atos import AtosDaemon
        d = AtosDaemon(binary_path="/tmp/fake", read_timeout_sec=0.7)
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        d._proc = mock_proc
        d._started = True

        # 拦截 queue.get 检查 timeout 参数; 支持 get_nowait (drain 路径) 和 get(timeout=...)
        observed_timeouts = []
        def capture(block=True, timeout=None):
            if not block:  # drain_queue 调用 get_nowait → get(block=False)
                raise queue.Empty()
            observed_timeouts.append(timeout)
            raise queue.Empty()  # 触发失败路径
        d._response_queue.get = capture

        d.lookup(0x1000)
        self.assertEqual(observed_timeouts, [0.7])


if __name__ == "__main__":
    unittest.main()
