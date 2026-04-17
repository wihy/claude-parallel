"""
SamplingProfilerSidecar — xctrace Time Profiler 短周期旁路采集。

以固定间隔（默认 10s）循环录制 Time Profiler，每 cycle 即时导出聚合
Top-N 热点函数，追加到 logs/hotspots.jsonl。与主 xctrace 长录制通道
并行，只负责"运行时预览"。

同时包含 TimeProfiler XML 解析、聚合、格式化等共享函数，供
PerfSessionManager.callstack() 复用。
"""

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from .templates import build_xctrace_record_cmd, BUILTIN_TEMPLATES

logger = logging.getLogger(__name__)


# ── Shared xctrace export / parse helpers ──


def export_xctrace_schema(trace_file: Path, schema: str, output: Path):
    """调用 xcrun xctrace export 导出指定 schema 的 XML。"""
    cmd = [
        "xcrun", "xctrace", "export",
        "--input", str(trace_file),
        "--xpath", f'/trace-toc/run/data/table[@schema="{schema}"]',
        "--output", str(output),
    ]
    subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False, text=True,
    )


def extract_mnemonic_value(
    row_text: str, mnemonic: str, default: str = "",
) -> str:
    """从 xctrace XML row 中提取指定 mnemonic 的 fmt 值。"""
    patterns = [
        re.compile(rf'<{re.escape(mnemonic)}[^>]*fmt="([^"]*)"', re.S),
        re.compile(
            rf'<{re.escape(mnemonic)}[^>]*>(.*?)</{re.escape(mnemonic)}>',
            re.S,
        ),
    ]
    for pat in patterns:
        m = pat.search(row_text)
        if m:
            val = m.group(1).strip()
            if val:
                return val
    return default


def parse_timeprofiler_xml(xml_path: Path) -> List[Tuple[str, float]]:
    """
    解析 xctrace export 的 TimeProfiler XML。

    Returns:
        [(frame_string, weight), ...]
        frame_string 格式: "caller → callee → leaf"
    """
    try:
        text = xml_path.read_text(errors="replace")
    except Exception:
        return []

    col_names = [m.group(1) for m in re.finditer(r'<col><name>([^<]+)</name>', text)]
    if not col_names:
        return []

    col_idx = {name.lower(): i for i, name in enumerate(col_names)}
    symbol_idx = col_idx.get(
        "symbol name",
        col_idx.get("symbol", col_idx.get("name", -1)),
    )
    weight_idx = col_idx.get(
        "sample count",
        col_idx.get("weight", col_idx.get("count", -1)),
    )

    if symbol_idx == -1:
        return []

    row_pat = re.compile(r"<row>(.*?)</row>", re.S)
    cell_pat = re.compile(r"<c[^>]*>(.*?)</c>", re.S)
    tag_strip = re.compile(r"<[^>]+/>")
    samples: List[Tuple[str, float]] = []

    for row_m in row_pat.finditer(text):
        row_text = row_m.group(1)

        symbol = extract_mnemonic_value(
            row_text, "symbol-name",
            extract_mnemonic_value(row_text, "symbol", ""),
        )
        if not symbol:
            symbol = extract_mnemonic_value(row_text, "name", "")

        if not symbol:
            cells = cell_pat.findall(row_text)
            if symbol_idx < len(cells):
                symbol = tag_strip.sub("", cells[symbol_idx]).strip()

        if not symbol or symbol == "?":
            continue

        weight = 1.0
        if weight_idx != -1:
            w_str = extract_mnemonic_value(
                row_text, "sample-count",
                extract_mnemonic_value(
                    row_text, "weight",
                    extract_mnemonic_value(row_text, "count", ""),
                ),
            )
            if w_str:
                try:
                    weight = float(re.sub(r"[^\d.]", "", w_str.split()[0]))
                except (ValueError, IndexError):
                    weight = 1.0

        caller = extract_mnemonic_value(row_text, "caller", "")
        frame = f"{caller} → {symbol}" if caller else symbol
        samples.append((frame, weight))

    return samples


def aggregate_top_n(
    samples: List[Tuple[str, float]], top_n: int,
) -> List[Dict[str, Any]]:
    """将原始采样聚合为 Top-N 热点函数（取 leaf 符号）。"""
    if not samples:
        return []

    func_weight: Dict[str, float] = defaultdict(float)
    for frame, weight in samples:
        leaf = frame.rsplit(" → ", 1)[-1]
        func_weight[leaf] += weight

    total = sum(func_weight.values())
    if total <= 0:
        return []

    hot = sorted(func_weight.items(), key=lambda x: x[1], reverse=True)
    return [
        {
            "symbol": func,
            "samples": int(round(w)),
            "pct": round(w / total * 100.0, 1),
        }
        for func, w in hot[:top_n]
    ]


# ── JSONL read / format helpers ──


def read_hotspots_jsonl(
    hotspots_file: Path,
    last_n: int = 0,
    aggregate: bool = False,
) -> List[Dict[str, Any]]:
    """读取 hotspots.jsonl 并返回快照列表。"""
    if not hotspots_file.exists():
        return []

    lines = hotspots_file.read_text(encoding="utf-8").strip().splitlines()
    snapshots = []
    for line in lines:
        try:
            snapshots.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if last_n > 0 and not aggregate:
        snapshots = snapshots[-last_n:]

    if aggregate and snapshots:
        func_weight: Dict[str, float] = defaultdict(float)
        total_samples = 0
        for snap in snapshots:
            for entry in snap.get("top", []):
                func_weight[entry["symbol"]] += entry.get("samples", 0)
                total_samples += entry.get("samples", 0)

        if total_samples > 0:
            hot = sorted(func_weight.items(), key=lambda x: x[1], reverse=True)
            agg_top = [
                {
                    "symbol": func,
                    "samples": int(round(w)),
                    "pct": round(w / total_samples * 100.0, 1),
                }
                for func, w in hot[:30]
            ]
            return [
                {
                    "aggregate": True,
                    "cycles": len(snapshots),
                    "total_samples": total_samples,
                    "top": agg_top,
                }
            ]

    return snapshots


def format_hotspots_text(
    snapshots: List[Dict[str, Any]], top_n: int = 10,
) -> str:
    """将热点快照格式化为可读文本。"""
    if not snapshots:
        return "  (无热点数据)"

    lines: List[str] = []
    for snap in snapshots:
        if snap.get("aggregate"):
            lines.append(
                f"  ── 全会话聚合 ({snap['cycles']} cycles, "
                f"{snap['total_samples']} samples) ──"
            )
        else:
            ts = snap.get("ts", 0)
            cycle = snap.get("cycle", "?")
            samples = snap.get("sample_count", 0)
            ts_str = (
                time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "?"
            )
            lines.append(
                f"  ── Cycle {cycle} @ {ts_str} "
                f"({samples} samples, {snap.get('duration_s', '?')}s) ──"
            )

        top = snap.get("top", [])[:top_n]
        if not top:
            lines.append("    (无采样)")
        else:
            max_sym = max(len(e["symbol"][:60]) for e in top)
            for i, e in enumerate(top):
                bar_len = int(e.get("pct", 0) / 2)
                bar = "█" * bar_len
                sym = e["symbol"][:60]
                lines.append(
                    f"  {i + 1:2d}. {sym:<{max_sym}}  "
                    f"{e.get('pct', 0):5.1f}%  "
                    f"({e.get('samples', 0)} samples)  {bar}"
                )
        lines.append("")

    return "\n".join(lines)


# ── Sidecar ──


@dataclass
class HotspotSnapshot:
    ts: float
    cycle: int
    duration_s: int
    sample_count: int
    top: List[Dict[str, Any]]


class SamplingProfilerSidecar:
    """
    xctrace Time Profiler 短周期旁路采集。

    以 interval_sec 为周期循环录制，每 cycle 导出 → 解析 → 追加 JSONL。
    与主 xctrace 长录制通道并行，cycle trace 用完即删。
    """

    MIN_INTERVAL = 5
    MAX_CONSECUTIVE_FAILURES = 3

    def __init__(
        self,
        session_root: Path,
        device_udid: str,
        process: str,
        interval_sec: int = 10,
        top_n: int = 10,
        retention: int = 30,
    ):
        self.session_root = Path(session_root)
        self.device_udid = device_udid
        self.process = process
        self.top_n = top_n
        self.retention = retention

        if interval_sec < self.MIN_INTERVAL:
            logger.warning(
                "sampling interval %ds < %ds, clamped",
                interval_sec,
                self.MIN_INTERVAL,
            )
            interval_sec = self.MIN_INTERVAL
        self.interval_sec = interval_sec

        self.logs_dir = self.session_root / "logs"
        self.hotspots_file = self.logs_dir / "hotspots.jsonl"
        self.stderr_file = self.logs_dir / "sampling.stderr"
        self._traces_tmp = self.session_root / "traces" / "_sampling_tmp"
        self._exports_tmp = self.session_root / "exports" / "_sampling_tmp"

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._cycle_count = 0
        self._current_proc: Optional[subprocess.Popen] = None
        self._proc_lock = threading.Lock()

    def start(self) -> bool:
        """启动旁路后台线程。返回 True 表示成功。"""
        if self._thread and self._thread.is_alive():
            return True

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._traces_tmp.mkdir(parents=True, exist_ok=True)
        self._exports_tmp.mkdir(parents=True, exist_ok=True)

        self._stop_event.clear()
        self._cycle_count = 0
        self._thread = threading.Thread(
            target=self._cycle_loop,
            name="sampling-profiler",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = 15.0) -> Dict[str, Any]:
        """通知退出并等待收尾。"""
        self._stop_event.set()
        self._kill_current_proc()

        if self._thread:
            self._thread.join(timeout=timeout)

        shutil.rmtree(self._traces_tmp, ignore_errors=True)
        shutil.rmtree(self._exports_tmp, ignore_errors=True)

        return {
            "cycles_completed": self._cycle_count,
            "hotspots_file": str(self.hotspots_file),
        }

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── internal ──

    def _cycle_loop(self):
        consecutive_failures = 0

        while not self._stop_event.is_set():
            cycle_num = self._cycle_count + 1
            try:
                snapshot = self._run_one_cycle(cycle_num)
                if snapshot:
                    self._append_snapshot(snapshot)
                    self._rotate_if_needed()
                    consecutive_failures = 0
                    self._cycle_count = cycle_num
                else:
                    consecutive_failures += 1
            except Exception as e:
                consecutive_failures += 1
                self._log_error(f"cycle {cycle_num} exception: {e}")

            if consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                self._log_error(
                    f"auto-stop: {consecutive_failures} consecutive failures"
                )
                break

    def _run_one_cycle(
        self, cycle_num: int,
    ) -> Optional[HotspotSnapshot]:
        if self._stop_event.is_set():
            return None

        trace_path = self._traces_tmp / f"cycle_{cycle_num}.trace"
        xml_path = self._exports_tmp / f"cycle_{cycle_num}.xml"

        tpl = BUILTIN_TEMPLATES["time"]
        cmd = build_xctrace_record_cmd(
            template=tpl,
            device=self.device_udid,
            attach=self.process,
            duration_sec=self.interval_sec,
            output_path=str(trace_path),
        )

        rc = self._record(cmd, cycle_num)
        if rc is None or self._stop_event.is_set():
            self._cleanup_path(trace_path)
            return None

        if not trace_path.exists():
            return None

        try:
            export_xctrace_schema(trace_path, "TimeProfiler", xml_path)
        except Exception as e:
            self._log_error(f"cycle {cycle_num}: export failed — {e}")
            self._cleanup_path(trace_path)
            return None

        samples = parse_timeprofiler_xml(xml_path)

        self._cleanup_path(trace_path)
        self._cleanup_path(xml_path)

        if not samples:
            return None

        top = aggregate_top_n(samples, self.top_n)
        total = sum(w for _, w in samples)

        return HotspotSnapshot(
            ts=time.time(),
            cycle=cycle_num,
            duration_s=self.interval_sec,
            sample_count=int(total),
            top=top,
        )

    def _record(self, cmd: List[str], cycle_num: int) -> Optional[int]:
        """执行 xctrace record, 返回 returncode 或 None（失败）。"""
        try:
            with self._proc_lock:
                if self._stop_event.is_set():
                    return None
                self._current_proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            proc = self._current_proc

            _, stderr_bytes = proc.communicate(
                timeout=self.interval_sec + 30,
            )

            with self._proc_lock:
                self._current_proc = None

            if stderr_bytes:
                stderr_text = stderr_bytes.decode("utf-8", errors="replace")
                if "already recording" in stderr_text.lower():
                    self._log_error(
                        f"cycle {cycle_num}: xctrace conflict — "
                        f"{stderr_text.strip()[:200]}"
                    )
                    return None

            return proc.returncode

        except subprocess.TimeoutExpired:
            self._kill_current_proc()
            self._log_error(f"cycle {cycle_num}: xctrace timed out")
            return None
        except Exception as e:
            self._log_error(f"cycle {cycle_num}: record failed — {e}")
            return None

    def _append_snapshot(self, snapshot: HotspotSnapshot):
        line = json.dumps(
            {
                "ts": snapshot.ts,
                "cycle": snapshot.cycle,
                "duration_s": snapshot.duration_s,
                "sample_count": snapshot.sample_count,
                "top": snapshot.top,
            },
            ensure_ascii=False,
        )
        try:
            with open(self.hotspots_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
        except Exception as e:
            self._log_error(f"jsonl write failed: {e}")

    def _rotate_if_needed(self):
        if not self.hotspots_file.exists():
            return
        try:
            lines = (
                self.hotspots_file.read_text(encoding="utf-8")
                .strip()
                .splitlines()
            )
            if len(lines) > self.retention:
                keep = lines[-self.retention :]
                self.hotspots_file.write_text(
                    "\n".join(keep) + "\n", encoding="utf-8",
                )
        except Exception:
            pass

    def _kill_current_proc(self):
        with self._proc_lock:
            proc = self._current_proc
            if proc is None:
                return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass

    def _cleanup_path(self, path: Path):
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink(missing_ok=True)
        except OSError:
            pass

    def _log_error(self, msg: str):
        logger.warning("[sampling] %s", msg)
        try:
            with open(self.stderr_file, "a", encoding="utf-8") as f:
                f.write(f"{time.time():.0f} {msg}\n")
        except Exception:
            pass
