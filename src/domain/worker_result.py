"""纯数据 — Worker 执行结果。

上升到 domain 层让 application / perf / infra 皆可直接依赖,不经过反向引用。
"""

from dataclasses import dataclass, field


@dataclass
class WorkerResult:
    """Worker 执行结果"""
    task_id: str
    success: bool
    output: str = ""
    error: str = ""
    session_id: str = ""
    cost_usd: float = 0.0
    duration_s: float = 0.0
    num_turns: int = 0
    model_used: str = ""
    worktree_path: str = ""
    stop_reason: str = ""
    retry_attempt: int = 0
    json_raw: dict = field(default_factory=dict)
