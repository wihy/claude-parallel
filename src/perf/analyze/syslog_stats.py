"""syslog / timeline 事件统计 + 可靠性探测。

从 session.py 搬出 — session 只负责生命周期, 本模块负责从已落地的
timeline.json / syslog 文件汇总统计 & 可靠性判定。
"""

import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional


def compute_timeline_stats(timeline_file: Path) -> Dict[str, Any]:
    """统计 timeline.json 里的 level_start/level_end 事件, 计算每个 level 的耗时。"""
    if not timeline_file.exists():
        return {"events": 0, "levels": []}
    try:
        payload = json.loads(timeline_file.read_text())
        events = payload.get("events", [])
    except Exception:
        return {"events": 0, "levels": []}

    level_ranges: Dict[Any, Dict[str, Any]] = {}
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


def compute_syslog_stats(meta: Dict[str, Any]) -> Dict[str, Any]:
    """从 meta 里读出 syslog 路径, 返回 lines / reliable 等统计。"""
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


def check_syslog_reliability(
    meta: Dict[str, Any],
    save_meta: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> bool:
    """判定 syslog 是否可靠, 回写 meta['syslog']['reliable']。

    如果传入 save_meta, 会调用它落盘。返回判定结果。
    """
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
    meta.setdefault("syslog", {})["reliable"] = reliable
    if save_meta is not None:
        save_meta(meta)
    return reliable
