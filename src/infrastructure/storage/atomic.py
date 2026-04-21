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


# ── PID 锁: 多实例并发安全 ────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    """检查指定 PID 是否还活着 (POSIX, signal 0)。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但无权限访问 — 仍视为活动
        return True
    except OSError:
        return False


def acquire_pid_lock(lock_dir: Path) -> Path:
    """在 lock_dir 下创建 pid-{pid}.lock 文件，返回锁文件路径。

    锁文件本身用于声明"我这个 cpar 实例正在使用此协调目录"。
    cleanup 类操作前应调用 list_active_locks 检查是否存在其他活动实例。
    """
    lock_dir = Path(lock_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    lock_file = lock_dir / f"pid-{pid}.lock"
    atomic_write_text(lock_file, f"{pid}\n{os.path.basename(__file__)}\n")
    return lock_file


def release_pid_lock(lock_file: Path) -> None:
    """释放（删除）当前进程的 PID 锁。幂等。"""
    try:
        Path(lock_file).unlink(missing_ok=True)
    except OSError:
        pass


def list_active_locks(lock_dir: Path, exclude_self: bool = True) -> list[int]:
    """列出 lock_dir 下仍存活的进程 PID。

    顺带清理掉对应进程已死的陈旧锁文件（无主锁不阻塞清理操作）。
    """
    lock_dir = Path(lock_dir)
    if not lock_dir.exists():
        return []
    self_pid = os.getpid() if exclude_self else None
    active: list[int] = []
    for f in lock_dir.iterdir():
        if not f.is_file() or not f.name.startswith("pid-") or not f.name.endswith(".lock"):
            continue
        try:
            pid = int(f.name[len("pid-"):-len(".lock")])
        except ValueError:
            continue
        if not _pid_alive(pid):
            # 陈旧锁，直接清理
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        if self_pid is not None and pid == self_pid:
            continue
        active.append(pid)
    return active
