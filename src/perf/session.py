"""
PerfSessionManager — 性能采集会话的完整生命周期管理。

提供:
- 真机 syslog 采集 (idevicesyslog)
- Instruments Power Profiler 长录制 (xcrun xctrace record)
- timeline 事件打点
- 采集会话的 start/stop/tail/report
- 基线对比 + 回归门禁
"""

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional, Dict, Any

from .config import PerfConfig


class PerfSessionManager:
    def __init__(self, repo: str, coordination_dir: str, config: PerfConfig):
        self.repo = Path(repo).expanduser().resolve()
        self.config = config
        self.coordination_dir = coordination_dir
        self.root = self.repo / coordination_dir / "perf" / config.tag
        self.logs_dir = self.root / "logs"
        self.traces_dir = self.root / "traces"
        self.exports_dir = self.root / "exports"
        self.meta_file = self.root / "meta.json"
        self.timeline_file = self.root / "timeline.json"
        self.report_file = self.root / "report.json"

    # ---------- lifecycle ----------
    def start(self) -> Dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.traces_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)

        meta = self._load_meta()
        if meta.get("status") == "running":
            return meta

        meta = {
            "tag": self.config.tag,
            "repo": str(self.repo),
            "started_at": time.time(),
            "ended_at": 0,
            "status": "running",
            "device": self.config.device,
            "attach": self.config.attach,
            "templates": self.config.templates,
            "duration_sec": self.config.duration_sec,
            "baseline_tag": self.config.baseline_tag,
            "threshold_pct": self.config.threshold_pct,
            "syslog": {
                "enabled": False,
                "pid": 0,
                "log": str(self.logs_dir / "syslog_full.log"),
                "reliable": None,
            },
            "xctrace": {
                "enabled": False,
                "pid": 0,
                "trace": str(self.traces_dir / "power.trace"),
                "stderr": str(self.logs_dir / "xctrace.stderr.log"),
            },
            "errors": [],
        }

        # syslog sidecar
        if self.config.device:
            try:
                syslog_log = Path(meta["syslog"]["log"])
                cmd = ["idevicesyslog", "-u", self.config.device]
                f = open(syslog_log, "a", encoding="utf-8")
                proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT)
                # Popen 接管 fd 后立即关闭 Python 侧句柄，防止泄漏
                f.close()
                meta["syslog"]["enabled"] = True
                meta["syslog"]["pid"] = proc.pid
            except Exception as e:
                meta["errors"].append(f"syslog_start_failed: {e}")

        # xctrace sidecar — 根据 templates 配置启动录制
        if self.config.device and self.config.attach:
            from .templates import TemplateLibrary, build_xctrace_record_cmd
            tpl_lib = TemplateLibrary()
            tpls = tpl_lib.resolve_multi(self.config.templates)

            if not tpls:
                # fallback: 如果 resolve 失败，用原始 template 字符串直接传
                tpls = []
                meta["xctrace"]["template_raw"] = self.config.templates

            # 如果只有单个模板，用旧的单一 xctrace 结构
            if len(tpls) == 1:
                tpl = tpls[0]
                trace_path = self.traces_dir / tpl.trace_filename(self.config.tag)
                stderr_path = Path(meta["xctrace"]["stderr"])
                cmd = build_xctrace_record_cmd(
                    template=tpl,
                    device=self.config.device,
                    attach=self.config.attach,
                    duration_sec=self.config.duration_sec,
                    output_path=str(trace_path),
                )
                try:
                    ferr = open(stderr_path, "a", encoding="utf-8")
                    proc = subprocess.Popen(cmd, stdout=ferr, stderr=subprocess.STDOUT)
                    ferr.close()
                    meta["xctrace"]["enabled"] = True
                    meta["xctrace"]["pid"] = proc.pid
                    meta["xctrace"]["trace"] = str(trace_path)
                    meta["xctrace"]["template"] = tpl.alias or tpl.name
                except Exception as e:
                    meta["errors"].append(f"xctrace_start_failed: {e}")
            elif len(tpls) > 1:
                # 多模板: 每个模板独立 xctrace 进程
                meta["xctrace_multi"] = []
                for tpl in tpls:
                    trace_path = self.traces_dir / tpl.trace_filename(self.config.tag)
                    stderr_path = self.logs_dir / f"xctrace_{tpl.alias or tpl.name}.stderr.log"
                    cmd = build_xctrace_record_cmd(
                        template=tpl,
                        device=self.config.device,
                        attach=self.config.attach,
                        duration_sec=self.config.duration_sec,
                        output_path=str(trace_path),
                    )
                    entry = {
                        "template": tpl.alias or tpl.name,
                        "enabled": False,
                        "pid": 0,
                        "trace": str(trace_path),
                        "stderr": str(stderr_path),
                    }
                    try:
                        ferr = open(stderr_path, "a", encoding="utf-8")
                        proc = subprocess.Popen(cmd, stdout=ferr, stderr=subprocess.STDOUT)
                        ferr.close()
                        entry["enabled"] = True
                        entry["pid"] = proc.pid
                    except Exception as e:
                        entry["error"] = str(e)
                        meta["errors"].append(f"xctrace_{tpl.alias}_start_failed: {e}")
                    meta["xctrace_multi"].append(entry)

        self._save_meta(meta)
        if not self.timeline_file.exists():
            self.timeline_file.parent.mkdir(parents=True, exist_ok=True)
            self.timeline_file.write_text(json.dumps({"events": []}, ensure_ascii=False, indent=2))
        self.mark_event("perf_session_started", detail="collector booted")
        return meta

    def stop(self) -> Dict[str, Any]:
        meta = self._load_meta()
        if not meta:
            return {}

        self._kill_pid(meta.get("syslog", {}).get("pid", 0))
        self._kill_pid(meta.get("xctrace", {}).get("pid", 0))
        # 多模板进程
        for entry in meta.get("xctrace_multi", []):
            self._kill_pid(entry.get("pid", 0))

        meta["ended_at"] = time.time()
        meta["status"] = "stopped"
        self._save_meta(meta)

        self._check_syslog_reliability(meta)
        self.mark_event("perf_session_stopped", detail="collector stopped")
        return meta

    def tail_syslog(self, lines: int = 80) -> str:
        meta = self._load_meta()
        log_path = Path(meta.get("syslog", {}).get("log", ""))
        if not log_path.exists():
            return "[perf] syslog file not found"
        data = log_path.read_text(errors="replace").splitlines()
        return "\n".join(data[-lines:])

    def mark_event(self, name: str, detail: str = "", level_idx: Optional[int] = None, tasks: Optional[list] = None):
        payload = {"events": []}
        if self.timeline_file.exists():
            try:
                payload = json.loads(self.timeline_file.read_text())
            except Exception:
                payload = {"events": []}
        if "events" not in payload or not isinstance(payload["events"], list):
            payload["events"] = []
        payload["events"].append({
            "ts": time.time(),
            "event": name,
            "detail": detail,
            "level_idx": level_idx,
            "tasks": tasks or [],
        })
        self.timeline_file.parent.mkdir(parents=True, exist_ok=True)
        self.timeline_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    # ---------- analysis ----------
    def report(self) -> Dict[str, Any]:
        meta = self._load_meta()
        report = {
            "tag": self.config.tag,
            "status": meta.get("status", "unknown"),
            "syslog": self._syslog_stats(meta),
            "timeline": self._timeline_stats(),
            "metrics": self._trace_metrics(meta),
            "baseline": {},
            "gate": {"checked": False, "passed": True, "reason": ""},
        }

        if self.config.baseline_tag:
            baseline = PerfSessionManager(str(self.repo), self.coordination_dir, PerfConfig(tag=self.config.baseline_tag))
            base_meta = baseline._load_meta()
            base_metrics = baseline._trace_metrics(base_meta)
            report["baseline"] = {
                "tag": self.config.baseline_tag,
                "metrics": base_metrics,
                "delta": self._calc_delta(base_metrics, report["metrics"]),
            }
            if self.config.threshold_pct > 0:
                report["gate"] = self._gate_check(report["baseline"]["delta"], self.config.threshold_pct)

        self.report_file.parent.mkdir(parents=True, exist_ok=True)
        self.report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        return report

    # ---------- internals ----------
    def _trace_metrics(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        trace_str = meta.get("xctrace", {}).get("trace", "")
        if not trace_str:
            return {"source": "none", "display_avg": None, "cpu_avg": None, "networking_avg": None}
        trace_file = Path(trace_str)
        if not trace_file.exists():
            return {"source": "none", "display_avg": None, "cpu_avg": None, "networking_avg": None}

        power_xml = self.exports_dir / "SystemPowerLevel.xml"
        proc_xml = self.exports_dir / "ProcessSubsystemPowerImpact.xml"

        self._export_schema(trace_file, "SystemPowerLevel", power_xml)
        self._export_schema(trace_file, "ProcessSubsystemPowerImpact", proc_xml)

        display_vals = self._extract_column_values(power_xml, "Display")
        cpu_vals = self._extract_column_values(proc_xml, "CPU")
        net_vals = self._extract_column_values(proc_xml, "Networking")

        return {
            "source": str(trace_file),
            "display_avg": self._avg(display_vals),
            "cpu_avg": self._avg(cpu_vals),
            "networking_avg": self._avg(net_vals),
            "display_samples": len(display_vals),
            "cpu_samples": len(cpu_vals),
            "networking_samples": len(net_vals),
        }

    def _export_schema(self, trace_file: Path, schema: str, output: Path):
        cmd = [
            "xcrun", "xctrace", "export",
            "--input", str(trace_file),
            "--xpath", f'/trace-toc/run/data/table[@schema="{schema}"]',
            "--output", str(output),
        ]
        try:
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, text=True)
        except Exception:
            pass

    def _extract_column_values(self, xml_file: Path, column_name: str) -> list:
        if not xml_file.exists():
            return []
        try:
            text = xml_file.read_text(errors="replace")
        except Exception:
            return []

        import re
        columns = []
        for m in re.finditer(r'<col[^>]*name="([^"]+)"', text):
            columns.append(m.group(1))
        if not columns:
            return []
        idx = None
        for i, name in enumerate(columns):
            if name.lower() == column_name.lower():
                idx = i
                break
        if idx is None:
            return []

        vals = []
        row_pat = re.compile(r"<row>(.*?)</row>", re.S)
        cell_pat = re.compile(r"<c[^>]*>(.*?)</c>", re.S)
        for row_m in row_pat.finditer(text):
            row = row_m.group(1)
            cells = [c.strip() for c in cell_pat.findall(row)]
            if idx < len(cells):
                try:
                    vals.append(float(cells[idx]))
                except Exception:
                    continue
        return vals

    def _calc_delta(self, base: Dict[str, Any], cur: Dict[str, Any]) -> Dict[str, Any]:
        def pct(a, b):
            if a is None or b is None or a == 0:
                return None
            return (b - a) / a * 100.0
        return {
            "display_avg_pct": pct(base.get("display_avg"), cur.get("display_avg")),
            "cpu_avg_pct": pct(base.get("cpu_avg"), cur.get("cpu_avg")),
            "networking_avg_pct": pct(base.get("networking_avg"), cur.get("networking_avg")),
        }

    def _gate_check(self, delta: Dict[str, Any], threshold_pct: float) -> Dict[str, Any]:
        reasons = []
        for key in ("display_avg_pct", "cpu_avg_pct", "networking_avg_pct"):
            v = delta.get(key)
            if v is not None and v > threshold_pct:
                reasons.append(f"{key}={v:.1f}% > {threshold_pct:.1f}%")
        return {
            "checked": True,
            "passed": len(reasons) == 0,
            "reason": "; ".join(reasons) if reasons else "ok",
        }

    def _timeline_stats(self) -> Dict[str, Any]:
        if not self.timeline_file.exists():
            return {"events": 0, "levels": []}
        try:
            payload = json.loads(self.timeline_file.read_text())
            events = payload.get("events", [])
        except Exception:
            return {"events": 0, "levels": []}

        level_ranges = {}
        for e in events:
            idx = e.get("level_idx")
            name = e.get("event", "")
            ts = e.get("ts", 0)
            if idx is None:
                continue
            level_ranges.setdefault(idx, {"start": None, "end": None, "tasks": []})
            if "level_start" in name:
                level_ranges[idx]["start"] = ts
                level_ranges[idx]["tasks"] = e.get("tasks", [])
            elif "level_end" in name:
                level_ranges[idx]["end"] = ts

        levels = []
        for idx in sorted(level_ranges.keys()):
            r = level_ranges[idx]
            dur = None
            if r["start"] and r["end"] and r["end"] >= r["start"]:
                dur = round(r["end"] - r["start"], 2)
            levels.append({
                "level_idx": idx,
                "duration_sec": dur,
                "tasks": r["tasks"],
            })
        return {"events": len(events), "levels": levels}

    def _syslog_stats(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        log_str = meta.get("syslog", {}).get("log", "")
        if not log_str:
            return {"source": "none", "reliable": False, "lines": 0}
        log_file = Path(log_str)
        if not log_file.exists():
            return {"source": "none", "reliable": False, "lines": 0}
        lines = log_file.read_text(errors="replace").splitlines()
        return {
            "source": str(log_file),
            "lines": len(lines),
            "reliable": bool(meta.get("syslog", {}).get("reliable", False)),
        }

    def _check_syslog_reliability(self, meta: Dict[str, Any]):
        log_file = Path(meta.get("syslog", {}).get("log", ""))
        reliable = False
        if log_file.exists():
            size = log_file.stat().st_size
            if size > 128:
                txt = log_file.read_text(errors="replace")
                if "[connected:" in txt and len(txt.strip().splitlines()) <= 2:
                    reliable = False
                else:
                    reliable = True
        meta["syslog"]["reliable"] = reliable
        self._save_meta(meta)

    def _kill_pid(self, pid: int):
        if not pid:
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            return

    def _load_meta(self) -> Dict[str, Any]:
        if not self.meta_file.exists():
            return {}
        try:
            return json.loads(self.meta_file.read_text())
        except Exception:
            return {}

    def _save_meta(self, meta: Dict[str, Any]):
        self.meta_file.parent.mkdir(parents=True, exist_ok=True)
        self.meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    def _avg(self, arr: list) -> Optional[float]:
        if not arr:
            return None
        return round(sum(arr) / len(arr), 4)
