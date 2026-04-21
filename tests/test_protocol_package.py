import unittest


class ProtocolPackageTest(unittest.TestCase):

    def test_reconnect_from_new_path(self):
        from src.perf.protocol.reconnect import ReconnectableMixin, ReconnectPolicy
        self.assertTrue(callable(ReconnectableMixin))
        self.assertTrue(callable(ReconnectPolicy))

    def test_dvt_from_new_path(self):
        from src.perf.protocol.dvt import DvtBridgeThread, check_dvt_available
        self.assertTrue(callable(DvtBridgeThread))
        self.assertTrue(callable(check_dvt_available))

    def test_device_from_new_path(self):
        from src.perf.protocol.device import BatteryPoller, ProcessMetricsStreamer
        self.assertTrue(callable(BatteryPoller))
        self.assertTrue(callable(ProcessMetricsStreamer))

    def test_reconnect_old_path_shim(self):
        from src.perf.reconnect import ReconnectableMixin
        self.assertTrue(callable(ReconnectableMixin))

    def test_dvt_bridge_old_path_shim(self):
        from src.perf.dvt_bridge import DvtBridgeThread
        self.assertTrue(callable(DvtBridgeThread))

    def test_device_metrics_old_path_shim(self):
        from src.perf.device_metrics import BatteryPoller
        self.assertTrue(callable(BatteryPoller))


if __name__ == "__main__":
    unittest.main()
