"""
文件系统工具 — 原子写入 + 安全 JSON 读取。

atomic_write_text / atomic_write_json 通过 tmp 文件 + os.replace 保证
崩溃/中断不会留下损坏的半写文件（resume 关键前置条件）。
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """把文本原子写入 path。先写同目录 tmp 文件，然后 os.replace。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, payload: Any, *, indent: int = 2) -> None:
    """把 JSON payload 原子写入 path。"""
    text = json.dumps(payload, ensure_ascii=False, indent=indent)
    atomic_write_text(path, text)


def safe_read_json(path: Path, default: Any = None) -> Any:
    """读取 JSON；不存在或损坏时返回 default（避免 resume 因损坏文件崩溃）。"""
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
