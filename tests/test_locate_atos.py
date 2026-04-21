import unittest
from unittest.mock import patch, MagicMock


class AtosDaemonTest(unittest.TestCase):

    def test_daemon_starts_atos_subprocess(self):
        from src.perf.locate.atos import AtosDaemon
        with patch("subprocess.Popen") as popen:
            popen.return_value = MagicMock()
            d = AtosDaemon(binary_path="/tmp/fake_bin", load_addr=0x100000000)
            d.start()
            args = popen.call_args[0][0]
            self.assertIn("atos", args[0])
            self.assertIn("-o", args)
            self.assertIn("-l", args)

    def test_lookup_returns_hex_when_not_started(self):
        from src.perf.locate.atos import AtosDaemon
        d = AtosDaemon(binary_path="/tmp/fake_bin", load_addr=0x100000000)
        result = d.lookup(0x100001234)
        self.assertTrue(result.startswith("0x"))

    def test_lookup_sends_addr_and_reads_symbol(self):
        """lookup 写 stdin 后读 atos 响应。

        注: Fix 4 (drain-before-write) 后,响应必须在 stdin.write 被调用之后
        才能出现在 queue (模拟 atos 收到请求后才输出)。
        """
        from src.perf.locate.atos import AtosDaemon
        d = AtosDaemon(binary_path="/tmp/fake_bin", load_addr=0x100000000)
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        # 在 stdin.write 触发时注入响应 (模拟真 atos 行为)
        original_write = mock_proc.stdin.write
        def write_and_respond(data):
            d._put_response("MyClass.swiftFunc()")
            return original_write(data)
        mock_proc.stdin.write = write_and_respond
        d._proc = mock_proc
        d._started = True
        result = d.lookup(0x100001234)
        self.assertEqual(result, "MyClass.swiftFunc()")

    def test_shutdown_terminates_process(self):
        from src.perf.locate.atos import AtosDaemon
        d = AtosDaemon(binary_path="/tmp/fake_bin", load_addr=0x100000000)
        proc = MagicMock()
        d._proc = proc
        d._started = True
        d.shutdown()
        proc.terminate.assert_called_once()

    def test_blacklist_after_consecutive_failures(self):
        from src.perf.locate.atos import AtosDaemon
        d = AtosDaemon(binary_path="/tmp/fake_bin", load_addr=0x100000000)
        d._started = True
        # 5 次连续失败后该地址入黑名单
        addr = 0x100001234
        for _ in range(6):
            d._record_failure(addr)
        self.assertIn(addr, d._blacklist)


if __name__ == "__main__":
    unittest.main()
