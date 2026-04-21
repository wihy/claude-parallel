"""AtosDaemon stale response queue 竞态修复验证 (Fix 4)。

真机场景: 上一次 lookup(addr_A) 在 200ms 软超时后,atos 晚一点写出 addr_A
的响应到 stdout。下一次 lookup(addr_B) 写入请求后,如果 response_queue 已
经有 stale 的 A 响应,会读到它返回给 B — 错位缓存。

修复方案: lookup 持锁后先 drain queue,再 write stdin,确保读到的响应是
我们刚写的请求的。
"""

import unittest
from unittest.mock import MagicMock


class AtosStaleQueueTest(unittest.TestCase):

    def test_drain_queue_removes_stale_responses(self):
        """_drain_queue() 清空所有残余响应,返回丢弃的条数。"""
        from src.perf.locate.atos import AtosDaemon
        d = AtosDaemon(binary_path="/tmp/fake", load_addr=0x100000000)
        # 模拟前一次 lookup 超时后晚到的响应
        d._put_response("-[StaleClass staleFunc]")
        d._put_response("-[AnotherStale method]")
        dropped = d._drain_queue()
        self.assertEqual(dropped, 2)
        # 再次 drain 返回 0 (已空)
        self.assertEqual(d._drain_queue(), 0)

    def test_lookup_ignores_stale_before_new_request(self):
        """lookup 不会读到之前超时留下的 stale 响应。"""
        from src.perf.locate.atos import AtosDaemon
        d = AtosDaemon(binary_path="/tmp/fake", load_addr=0x100000000)
        # 注入 stale 响应 (模拟上次 timeout 后晚到的)
        d._put_response("-[StaleClass staleFunc]")

        # 设置好状态, 新 lookup 请求 addr_B
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        d._proc = mock_proc
        d._started = True

        # 这次我们期望的响应 (模拟 atos 对 addr_B 的实时响应)
        # 但 drain 执行后 queue 空,需要在 lookup 调用中另外注入 "新响应"
        # 策略: patch stdin.write 同时 put 新响应
        real_write = mock_proc.stdin.write
        def write_and_respond(data):
            d._put_response("-[NewClass newFunc]")
            return real_write(data)
        mock_proc.stdin.write = write_and_respond

        result = d.lookup(0xBEEF)
        # 没有读到 stale,读到的是新响应
        self.assertEqual(result, "-[NewClass newFunc]")

    def test_drain_on_empty_queue_is_noop(self):
        """queue 本来就空时 drain 不出错, 返回 0。"""
        from src.perf.locate.atos import AtosDaemon
        d = AtosDaemon(binary_path="/tmp/fake", load_addr=0x100000000)
        dropped = d._drain_queue()
        self.assertEqual(dropped, 0)


if __name__ == "__main__":
    unittest.main()
