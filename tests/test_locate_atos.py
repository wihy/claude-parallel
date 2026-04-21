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
        from src.perf.locate.atos import AtosDaemon
        d = AtosDaemon(binary_path="/tmp/fake_bin", load_addr=0x100000000)
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        d._proc = mock_proc
        d._started = True
        d._put_response("MyClass.swiftFunc()")
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
