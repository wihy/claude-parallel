"""
device_metrics 单元测试。

覆盖:
- _read_battery plist 解析
- BatteryPoller 初始化
- ProcessMetricsStreamer.check_available
- read_battery_jsonl / read_process_metrics_jsonl
- format_battery_text / format_process_metrics_text
- PerfConfig 新字段
- metrics_source 决策逻辑
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.perf.protocol.device import (
    BatteryPoller,
    ProcessMetricsStreamer,
    _read_battery,
    read_battery_jsonl,
    read_process_metrics_jsonl,
    format_battery_text,
    format_process_metrics_text,
)
from src.perf.config import PerfConfig


BATTERY_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>BatteryCurrentCapacity</key>
    <integer>85</integer>
    <key>BatteryIsCharging</key>
    <true/>
    <key>ExternalConnected</key>
    <true/>
    <key>FullyCharged</key>
    <false/>
    <key>HasBattery</key>
    <true/>
</dict>
</plist>
"""


class TestReadBattery(unittest.TestCase):
    @patch("src.perf.protocol.device.subprocess.run")
    def test_parse_plist(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout=BATTERY_PLIST
        )
        data = _read_battery("TEST_UDID")
        self.assertIsNotNone(data)
        self.assertEqual(data["level_pct"], 85)
        self.assertTrue(data["is_charging"])
        self.assertTrue(data["external_connected"])
        self.assertFalse(data["fully_charged"])
        self.assertIn("ts", data)

    @patch("src.perf.protocol.device.subprocess.run")
    def test_ideviceinfo_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        data = _read_battery("TEST_UDID")
        self.assertIsNone(data)


class TestBatteryPoller(unittest.TestCase):
    def test_init(self):
        p = BatteryPoller("TEST", interval_sec=5.0)
        self.assertEqual(p.device_udid, "TEST")
        self.assertEqual(p.interval_sec, 5.0)


class TestProcessMetricsStreamer(unittest.TestCase):
    def test_check_available(self):
        # pymobiledevice3 is installed in this env
        self.assertTrue(ProcessMetricsStreamer.check_available())

    @patch("src.perf.protocol.device.subprocess.run")
    def test_check_tunneld_not_running(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        self.assertFalse(ProcessMetricsStreamer.check_tunneld_running())


class TestReadBatteryJsonl(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.jsonl = Path(self.tmpdir) / "battery.jsonl"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_read_all(self):
        with open(self.jsonl, "w") as f:
            for i in range(5):
                f.write(json.dumps({"ts": 1000 + i, "level_pct": 90 - i}) + "\n")
        records = read_battery_jsonl(self.jsonl)
        self.assertEqual(len(records), 5)

    def test_last_n(self):
        with open(self.jsonl, "w") as f:
            for i in range(10):
                f.write(json.dumps({"ts": 1000 + i, "level_pct": 90 - i}) + "\n")
        records = read_battery_jsonl(self.jsonl, last_n=3)
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["ts"], 1007)

    def test_missing_file(self):
        records = read_battery_jsonl(Path("/nonexistent.jsonl"))
        self.assertEqual(records, [])


class TestReadProcessMetricsJsonl(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.jsonl = Path(self.tmpdir) / "process_metrics.jsonl"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_read(self):
        with open(self.jsonl, "w") as f:
            f.write(json.dumps({"cpuUsage": 12.3, "physFootprint": 200000000, "pid": 100, "name": "App"}) + "\n")
            f.write(json.dumps({"cpuUsage": 8.1, "physFootprint": 190000000, "pid": 100, "name": "App"}) + "\n")
        records = read_process_metrics_jsonl(self.jsonl)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["cpuUsage"], 12.3)


class TestFormatBattery(unittest.TestCase):
    def test_empty(self):
        self.assertIn("无电池数据", format_battery_text([]))

    def test_format(self):
        records = [
            {"ts": 1713250000, "level_pct": 85, "is_charging": True, "external_connected": True},
            {"ts": 1713250010, "level_pct": 86, "is_charging": True, "external_connected": True},
        ]
        text = format_battery_text(records)
        self.assertIn("85%", text)
        self.assertIn("charging", text)
        self.assertIn("[USB]", text)


class TestFormatProcessMetrics(unittest.TestCase):
    def test_empty(self):
        self.assertIn("无进程指标", format_process_metrics_text([]))

    def test_format(self):
        records = [
            {"cpuUsage": 15.5, "physFootprint": 200 * 1024 * 1024, "pid": 5821, "name": "Soul_New"},
        ]
        text = format_process_metrics_text(records)
        self.assertIn("CPU=15.5%", text)
        self.assertIn("Soul_New", text)
        self.assertIn("200.0MB", text)


class TestPerfConfigMetrics(unittest.TestCase):
    def test_defaults(self):
        cfg = PerfConfig()
        self.assertEqual(cfg.metrics_source, "auto")
        self.assertEqual(cfg.metrics_interval_ms, 1000)
        self.assertEqual(cfg.battery_interval_sec, 10)

    def test_device_source(self):
        cfg = PerfConfig(metrics_source="device", metrics_interval_ms=500)
        self.assertEqual(cfg.metrics_source, "device")
        self.assertEqual(cfg.metrics_interval_ms, 500)


if __name__ == "__main__":
    unittest.main()
