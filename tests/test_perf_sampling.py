"""
SamplingProfilerSidecar + 共享解析函数的单元测试。

测试覆盖:
- parse_timeprofiler_xml: fixture 端到端
- aggregate_top_n: 边界情况
- read_hotspots_jsonl: 读/切片/聚合
- format_hotspots_text: 格式化
- SamplingProfilerSidecar: mock xctrace 下的 cycle pipeline
- Config clamp: interval < 5 → 钳位
"""

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.perf.sampling import (
    SamplingProfilerSidecar,
    HotspotSnapshot,
    parse_timeprofiler_xml,
    aggregate_top_n,
    extract_mnemonic_value,
    export_xctrace_schema,
    read_hotspots_jsonl,
    format_hotspots_text,
)
from src.perf.config import PerfConfig


# ── Fixture: minimal TimeProfiler XML ──

TIMEPROFILER_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<trace-query-result>
<node>
<row-schema>
<col><name>Symbol Name</name></col>
<col><name>Sample Count</name></col>
<col><name>Thread</name></col>
</row-schema>
<row>
<symbol-name id="1" fmt="-[SOMetalRenderer drawFrame:]">0x1234</symbol-name>
<sample-count id="2" fmt="287">287</sample-count>
<thread id="3" fmt="main">1</thread>
</row>
<row>
<symbol-name id="4" fmt="objc_msgSend">0x5678</symbol-name>
<sample-count id="5" fmt="156">156</sample-count>
<thread id="6" fmt="main">1</thread>
</row>
<row>
<symbol-name id="7" fmt="-[UIView layoutSubviews]">0x9abc</symbol-name>
<sample-count id="8" fmt="98">98</sample-count>
<thread id="9" fmt="main">1</thread>
</row>
<row>
<symbol-name id="10" fmt="?">0xdead</symbol-name>
<sample-count id="11" fmt="5">5</sample-count>
<thread id="12" fmt="worker">2</thread>
</row>
</node>
</trace-query-result>
"""

TIMEPROFILER_XML_WITH_CALLER = """\
<?xml version="1.0" encoding="UTF-8"?>
<trace-query-result>
<node>
<row-schema>
<col><name>Symbol Name</name></col>
<col><name>Sample Count</name></col>
</row-schema>
<row>
<caller id="0" fmt="main">0</caller>
<symbol-name id="1" fmt="doWork">0x1</symbol-name>
<sample-count id="2" fmt="100">100</sample-count>
</row>
<row>
<caller id="3" fmt="doWork">1</caller>
<symbol-name id="4" fmt="compute">0x2</symbol-name>
<sample-count id="5" fmt="80">80</sample-count>
</row>
</node>
</trace-query-result>
"""


class TestParseTimeprofilerXml(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_xml(self, content: str) -> Path:
        p = Path(self.tmpdir) / "test.xml"
        p.write_text(content)
        return p

    def test_basic_parse(self):
        xml_path = self._write_xml(TIMEPROFILER_XML)
        samples = parse_timeprofiler_xml(xml_path)
        self.assertEqual(len(samples), 3)
        symbols = [s[0] for s in samples]
        self.assertIn("-[SOMetalRenderer drawFrame:]", symbols)
        self.assertIn("objc_msgSend", symbols)
        self.assertIn("-[UIView layoutSubviews]", symbols)

    def test_question_mark_filtered(self):
        xml_path = self._write_xml(TIMEPROFILER_XML)
        samples = parse_timeprofiler_xml(xml_path)
        symbols = [s[0] for s in samples]
        self.assertNotIn("?", symbols)

    def test_weights_parsed(self):
        xml_path = self._write_xml(TIMEPROFILER_XML)
        samples = parse_timeprofiler_xml(xml_path)
        weight_map = {s[0]: s[1] for s in samples}
        self.assertEqual(weight_map["-[SOMetalRenderer drawFrame:]"], 287.0)
        self.assertEqual(weight_map["objc_msgSend"], 156.0)

    def test_caller_chain(self):
        xml_path = self._write_xml(TIMEPROFILER_XML_WITH_CALLER)
        samples = parse_timeprofiler_xml(xml_path)
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0][0], "main → doWork")
        self.assertEqual(samples[1][0], "doWork → compute")

    def test_empty_xml(self):
        xml_path = self._write_xml("<trace-query-result></trace-query-result>")
        samples = parse_timeprofiler_xml(xml_path)
        self.assertEqual(samples, [])

    def test_missing_file(self):
        samples = parse_timeprofiler_xml(Path("/nonexistent/path.xml"))
        self.assertEqual(samples, [])


class TestAggregateTopN(unittest.TestCase):
    def test_basic_aggregate(self):
        samples = [
            ("A → X", 10),
            ("B → X", 5),
            ("A → Y", 3),
            ("C → Z", 1),
        ]
        top = aggregate_top_n(samples, 3)
        self.assertEqual(len(top), 3)
        self.assertEqual(top[0]["symbol"], "X")
        self.assertEqual(top[0]["samples"], 15)
        self.assertAlmostEqual(top[0]["pct"], 15 / 19 * 100, places=0)

    def test_empty_samples(self):
        self.assertEqual(aggregate_top_n([], 10), [])

    def test_top_n_exceeds_symbols(self):
        samples = [("A", 1)]
        top = aggregate_top_n(samples, 100)
        self.assertEqual(len(top), 1)

    def test_all_same_symbol(self):
        samples = [("X", 1)] * 100
        top = aggregate_top_n(samples, 5)
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["samples"], 100)
        self.assertAlmostEqual(top[0]["pct"], 100.0, places=1)


class TestExtractMnemonicValue(unittest.TestCase):
    def test_fmt_attribute(self):
        row = '<symbol-name id="1" fmt="doWork">0x1234</symbol-name>'
        self.assertEqual(extract_mnemonic_value(row, "symbol-name"), "doWork")

    def test_inner_text(self):
        row = "<symbol-name>doWork</symbol-name>"
        self.assertEqual(extract_mnemonic_value(row, "symbol-name"), "doWork")

    def test_missing_returns_default(self):
        self.assertEqual(extract_mnemonic_value("<row></row>", "missing", "N/A"), "N/A")


class TestReadHotspotsJsonl(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.jsonl = Path(self.tmpdir) / "hotspots.jsonl"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_snapshots(self, count: int):
        with open(self.jsonl, "w") as f:
            for i in range(count):
                snap = {
                    "ts": 1000 + i,
                    "cycle": i + 1,
                    "duration_s": 10,
                    "sample_count": 100,
                    "top": [
                        {"symbol": f"func_{i}", "samples": 50, "pct": 50.0},
                        {"symbol": "common", "samples": 50, "pct": 50.0},
                    ],
                }
                f.write(json.dumps(snap) + "\n")

    def test_read_all(self):
        self._write_snapshots(5)
        snaps = read_hotspots_jsonl(self.jsonl)
        self.assertEqual(len(snaps), 5)

    def test_last_n(self):
        self._write_snapshots(10)
        snaps = read_hotspots_jsonl(self.jsonl, last_n=3)
        self.assertEqual(len(snaps), 3)
        self.assertEqual(snaps[0]["cycle"], 8)

    def test_aggregate(self):
        self._write_snapshots(5)
        snaps = read_hotspots_jsonl(self.jsonl, aggregate=True)
        self.assertEqual(len(snaps), 1)
        self.assertTrue(snaps[0]["aggregate"])
        self.assertEqual(snaps[0]["cycles"], 5)
        symbols = {e["symbol"] for e in snaps[0]["top"]}
        self.assertIn("common", symbols)

    def test_missing_file(self):
        snaps = read_hotspots_jsonl(Path("/nonexistent/hotspots.jsonl"))
        self.assertEqual(snaps, [])

    def test_corrupt_line_skipped(self):
        with open(self.jsonl, "w") as f:
            f.write('{"ts":1,"cycle":1,"top":[]}\n')
            f.write("not json\n")
            f.write('{"ts":2,"cycle":2,"top":[]}\n')
        snaps = read_hotspots_jsonl(self.jsonl)
        self.assertEqual(len(snaps), 2)


class TestFormatHotspotsText(unittest.TestCase):
    def test_empty(self):
        text = format_hotspots_text([])
        self.assertIn("无热点数据", text)

    def test_basic_format(self):
        snaps = [
            {
                "ts": 1713250010,
                "cycle": 1,
                "duration_s": 10,
                "sample_count": 100,
                "top": [
                    {"symbol": "funcA", "samples": 60, "pct": 60.0},
                    {"symbol": "funcB", "samples": 40, "pct": 40.0},
                ],
            }
        ]
        text = format_hotspots_text(snaps)
        self.assertIn("Cycle 1", text)
        self.assertIn("funcA", text)
        self.assertIn("60.0%", text)

    def test_aggregate_format(self):
        snaps = [
            {
                "aggregate": True,
                "cycles": 5,
                "total_samples": 500,
                "top": [{"symbol": "funcA", "samples": 300, "pct": 60.0}],
            }
        ]
        text = format_hotspots_text(snaps)
        self.assertIn("全会话聚合", text)
        self.assertIn("5 cycles", text)


class TestSamplingProfilerSidecar(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.session_root = Path(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_interval_clamp(self):
        sidecar = SamplingProfilerSidecar(
            session_root=self.session_root,
            device_udid="TEST",
            process="TestApp",
            interval_sec=1,
        )
        self.assertEqual(sidecar.interval_sec, SamplingProfilerSidecar.MIN_INTERVAL)

    def test_interval_normal(self):
        sidecar = SamplingProfilerSidecar(
            session_root=self.session_root,
            device_udid="TEST",
            process="TestApp",
            interval_sec=15,
        )
        self.assertEqual(sidecar.interval_sec, 15)

    @patch("src.perf.capture.sampling.subprocess.Popen")
    def test_cycle_with_mock_xctrace(self, mock_popen):
        """Mock xctrace, 写入 fixture XML, 验证 JSONL 输出。"""
        xml_content = TIMEPROFILER_XML

        def fake_popen(cmd, **kwargs):
            proc = MagicMock()
            # xctrace record → 创建假 trace 目录 + 写 fixture XML
            if "record" in cmd:
                for arg_i, arg in enumerate(cmd):
                    if arg == "--output":
                        trace_path = Path(cmd[arg_i + 1])
                        trace_path.mkdir(parents=True, exist_ok=True)
                        break
                proc.communicate.return_value = (b"", b"")
                proc.returncode = 0
            proc.terminate = MagicMock()
            proc.kill = MagicMock()
            proc.wait = MagicMock()
            return proc

        mock_popen.side_effect = fake_popen

        sidecar = SamplingProfilerSidecar(
            session_root=self.session_root,
            device_udid="TEST",
            process="TestApp",
            interval_sec=5,
            top_n=3,
            retention=10,
        )

        # Mock export to write fixture XML
        def fake_export(trace_file, schema, output):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(xml_content)

        with patch("src.perf.capture.sampling.export_xctrace_schema", side_effect=fake_export):
            snapshot = sidecar._run_one_cycle(1)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.cycle, 1)
        self.assertEqual(snapshot.sample_count, 287 + 156 + 98)
        self.assertEqual(len(snapshot.top), 3)
        self.assertEqual(snapshot.top[0]["symbol"], "-[SOMetalRenderer drawFrame:]")

    def test_rotation(self):
        sidecar = SamplingProfilerSidecar(
            session_root=self.session_root,
            device_udid="TEST",
            process="TestApp",
            retention=3,
        )
        sidecar.logs_dir.mkdir(parents=True, exist_ok=True)

        for i in range(5):
            snap = HotspotSnapshot(
                ts=time.time(), cycle=i + 1, duration_s=10,
                sample_count=100, top=[{"symbol": f"f{i}", "samples": 100, "pct": 100}],
            )
            sidecar._append_snapshot(snap)
            sidecar._rotate_if_needed()

        lines = sidecar.hotspots_file.read_text().strip().splitlines()
        self.assertEqual(len(lines), 3)
        last = json.loads(lines[-1])
        self.assertEqual(last["cycle"], 5)


class TestPerfConfigSampling(unittest.TestCase):
    def test_default_values(self):
        cfg = PerfConfig()
        self.assertFalse(cfg.sampling_enabled)
        self.assertEqual(cfg.sampling_interval_sec, 10)
        self.assertEqual(cfg.sampling_top_n, 10)
        self.assertEqual(cfg.sampling_retention, 30)

    def test_custom_values(self):
        cfg = PerfConfig(
            sampling_enabled=True,
            sampling_interval_sec=15,
            sampling_top_n=20,
            sampling_retention=50,
        )
        self.assertTrue(cfg.sampling_enabled)
        self.assertEqual(cfg.sampling_interval_sec, 15)


class TestSessionManagerSamplingIntegration(unittest.TestCase):
    def test_main_has_timeprofiler_single(self):
        from src.perf.session import PerfSessionManager
        cfg = PerfConfig(tag="test")
        tmpdir = tempfile.mkdtemp()
        try:
            mgr = PerfSessionManager(tmpdir, ".claude-parallel", cfg)
            meta = {"xctrace": {"template": "time"}}
            self.assertTrue(mgr._main_has_timeprofiler(meta))
            meta2 = {"xctrace": {"template": "power"}}
            self.assertFalse(mgr._main_has_timeprofiler(meta2))
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_main_has_timeprofiler_multi(self):
        from src.perf.session import PerfSessionManager
        cfg = PerfConfig(tag="test")
        tmpdir = tempfile.mkdtemp()
        try:
            mgr = PerfSessionManager(tmpdir, ".claude-parallel", cfg)
            meta = {
                "xctrace": {"template": "power"},
                "xctrace_multi": [
                    {"template": "power"},
                    {"template": "Time Profiler"},
                ],
            }
            self.assertTrue(mgr._main_has_timeprofiler(meta))
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
