"""
任务解析器 — 从 YAML 文件解析任务定义，构建 DAG 依赖图，拓扑排序。
"""

import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Task:
    """单个并行任务定义"""
    id: str
    description: str
    files: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=lambda: ["Read", "Edit", "Write", "Bash"])
    max_turns: int = 15
    max_budget_usd: float = 2.0
    depends_on: list[str] = field(default_factory=list)
    model: str = ""  # 留空用默认
    effort: str = "medium"
    extra_prompt: str = ""  # 追加的上下文/指令
    status: str = "pending"  # pending / running / done / failed / cancelled

    def __repr__(self):
        return f"Task({self.id}, deps={self.depends_on}, status={self.status})"


@dataclass
class ProjectConfig:
    """项目级配置"""
    repo: str  # 项目仓库路径
    branch: str = "main"
    max_workers: int = 3
    default_model: str = ""
    default_effort: str = "medium"
    default_max_turns: int = 15
    default_max_budget_usd: float = 2.0
    coordination_dir: str = ".claude-parallel"  # 协调文件目录（相对于 repo）
    retry_count: int = 2  # Phase 2: 失败重试次数
    retry_backoff: float = 5.0  # Phase 2: 重试基础退避时间(秒)
    total_budget_usd: float = 0.0  # Phase 2: 总预算上限 (0=不限制)


def parse_task_file(filepath: str) -> tuple[ProjectConfig, list[Task]]:
    """
    解析 YAML 任务文件，返回项目配置和任务列表。
    
    YAML 格式:
    project:
      repo: ~/myproject
      branch: main
      max_workers: 3
    tasks:
      - id: task-1
        description: "做某事"
        files: ["a.py", "b.py"]
        depends_on: []
        ...
    """
    path = Path(filepath).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"任务文件不存在: {path}")
    
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    
    if not data or "tasks" not in data:
        raise ValueError("YAML 必须包含 'tasks' 字段")
    
    # 解析项目配置
    proj_data = data.get("project", {})
    repo = str(Path(proj_data.get("repo", ".")).expanduser().resolve())
    config = ProjectConfig(
        repo=repo,
        branch=proj_data.get("branch", "main"),
        max_workers=proj_data.get("max_workers", 3),
        default_model=proj_data.get("default_model", ""),
        default_effort=proj_data.get("default_effort", "medium"),
        default_max_turns=proj_data.get("default_max_turns", 15),
        default_max_budget_usd=proj_data.get("default_max_budget_usd", 2.0),
        retry_count=proj_data.get("retry_count", 2),
        retry_backoff=proj_data.get("retry_backoff", 5.0),
        total_budget_usd=proj_data.get("total_budget_usd", 0.0),
    )
    
    # 解析任务列表
    tasks = []
    task_ids = set()
    for t in data["tasks"]:
        tid = t["id"]
        if tid in task_ids:
            raise ValueError(f"重复的任务 ID: {tid}")
        task_ids.add(tid)
        
        task = Task(
            id=tid,
            description=t["description"],
            files=t.get("files", []),
            allowed_tools=t.get("allowed_tools", ["Read", "Edit", "Write", "Bash"]),
            max_turns=t.get("max_turns", config.default_max_turns),
            max_budget_usd=t.get("max_budget_usd", config.default_max_budget_usd),
            depends_on=t.get("depends_on", []),
            model=t.get("model", config.default_model),
            effort=t.get("effort", config.default_effort),
            extra_prompt=t.get("extra_prompt", ""),
        )
        tasks.append(task)
    
    # 验证依赖关系
    for task in tasks:
        for dep in task.depends_on:
            if dep not in task_ids:
                raise ValueError(f"任务 '{task.id}' 依赖不存在的任务 '{dep}'")
    
    return config, tasks


def topological_levels(tasks: list[Task]) -> list[list[Task]]:
    """
    拓扑排序，按层级分组。
    同一层级的任务无互相依赖，可以并行执行。
    
    返回: [[level_0_tasks], [level_1_tasks], ...]
    """
    task_map = {t.id: t for t in tasks}
    in_degree = {t.id: len(t.depends_on) for t in tasks}
    # 反向邻接表: dep → [tasks that depend on dep]
    reverse_adj = {t.id: [] for t in tasks}
    for t in tasks:
        for dep in t.depends_on:
            reverse_adj[dep].append(t.id)
    
    levels = []
    # 初始: 入度为 0 的节点
    current = [tid for tid, deg in in_degree.items() if deg == 0]
    
    while current:
        level = [task_map[tid] for tid in sorted(current)]  # 排序保证确定性
        levels.append(level)
        next_level = []
        for tid in current:
            for child in reverse_adj[tid]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    next_level.append(child)
        current = next_level
    
    # 检测环
    total = sum(len(level) for level in levels)
    if total != len(tasks):
        raise ValueError("检测到循环依赖！请检查 depends_on 字段")
    
    return levels


def get_task_map(tasks: list[Task]) -> dict[str, Task]:
    return {t.id: t for t in tasks}
