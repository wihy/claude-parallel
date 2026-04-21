import argparse
import asyncio
import tempfile
import time
import unittest
from pathlib import Path


class PerfLiveStreamRegressionTest(unittest.TestCase):
    def test_live_log_buffer_lines_accepts_string_and_recent_lines_no_typeerror(self):
        from src.perf.capture.live_log import LiveLogAnalyzer

        analyzer = LiveLogAnalyzer(buffer_lines="8")
        self.assertEqual(analyzer.buffer_lines, 8)

        analyzer._line_buffer = [f"line-{i}" for i in range(10)]
        recent = analyzer.get_recent_lines("3")
        self.assertEqual(recent, ["line-7", "line-8", "line-9"])

    def test_cmd_perf_stream_no_nameerror_on_snapshot_counter(self):
        from src.app import perf_cli
        import src.perf as perf_mod

        class FakeStreamer:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def start(self):
                return {"status": "running"}

            def get_summary(self):
                return {
                    "snapshots": 1,
                    "latest": {
                        "ts": time.time(),
                        "cpu_pct": 37.0,
                        "display_mw": 120.0,
                    },
                }

            def stop(self):
                return {"snapshots": 1, "alerts": 0, "stats": {"samples": 0}}

        with tempfile.TemporaryDirectory() as td:
            trace = Path(td) / "dummy.trace"
            trace.write_text("trace")

            args = argparse.Namespace(trace=str(trace), interval=0.01, window=5)

            old_cls = perf_mod.LiveMetricsStreamer
            old_sleep = perf_cli.time.sleep
            try:
                perf_mod.LiveMetricsStreamer = FakeStreamer

                def _break_sleep(_):
                    raise KeyboardInterrupt()

                perf_cli.time.sleep = _break_sleep
                asyncio.run(perf_cli.cmd_perf_stream(args))
            finally:
                perf_mod.LiveMetricsStreamer = old_cls
                perf_cli.time.sleep = old_sleep


if __name__ == "__main__":
    unittest.main()
