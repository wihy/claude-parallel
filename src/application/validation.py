"""
YAML 任务文件校验器 — 在执行前验证配置正确性，提供友好的错误提示。

检查项:
- 文件格式 (YAML 语法)
- 必填字段
- 字段类型和取值范围
- 任务 ID 唯一性
- 依赖关系可达性 (无环、无悬空引用)
- 文件路径存在性
- Claude CLI 可用性
- Git 仓库状态
"""

import os
import re
import sys
from pathlib import Path
from typing import List, Tuple, Optional

import yaml


class ValidationError:
    """单个校验错误"""
    def __init__(self, level: str, field: str, message: str):
        self.level = level  # "error" or "warning"
        self.field = field
        self.message = message

    def __str__(self):
        icon = "✗" if self.level == "error" else "⚠"
        return f"  {icon} [{self.field}] {self.message}"


class TaskValidator:
    """任务文件校验器"""

    def __init__(self, task_file: str, perf_enabled: bool = False, perf_device: str = "", perf_attach: str = ""):
        self.task_file = task_file
        self.perf_enabled = perf_enabled
        self.perf_device = perf_device
        self.perf_attach = perf_attach
        self.errors: List[ValidationError] = []
        self.data: Optional[dict] = None

    def validate(self) -> bool:
        """执行所有校验，返回 True 表示通过"""
        self.errors = []

        self._check_file()
        if self.errors:
            return False

        self._check_yaml()
        if self.errors:
            return False

        self._check_project()
        self._check_tasks()
        self._check_dependencies()
        self._check_tool_availability()
        if self.perf_enabled:
            self._check_perf_prerequisites()

        return not any(e.level == "error" for e in self.errors)

    def print_report(self):
        """打印校验结果"""
        if not self.errors:
            print("  ✓ 配置校验通过")
            return

        errors = [e for e in self.errors if e.level == "error"]
        warnings = [e for e in self.errors if e.level == "warning"]

        if errors:
            print(f"\n  校验失败 — {len(errors)} 个错误, {len(warnings)} 个警告:\n")
            for e in self.errors:
                print(e)
        elif warnings:
            print(f"\n  校验通过 (有 {len(warnings)} 个警告):\n")
            for e in self.errors:
                print(e)

    # ── 校验步骤 ──

    def _check_file(self):
        """检查文件存在性和可读性"""
        path = Path(self.task_file)
        if not path.exists():
            self.errors.append(ValidationError(
                "error", "文件", f"任务文件不存在: {self.task_file}"
            ))
            return
        if not path.is_file():
            self.errors.append(ValidationError(
                "error", "文件", f"不是文件: {self.task_file}"
            ))
            return
        if path.suffix not in (".yaml", ".yml"):
            self.errors.append(ValidationError(
                "warning", "文件", f"文件扩展名应为 .yaml 或 .yml"
            ))

    def _check_yaml(self):
        """检查 YAML 语法"""
        try:
            with open(self.task_file, "r", encoding="utf-8") as f:
                self.data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            self.errors.append(ValidationError(
                "error", "YAML语法", str(e)[:200]
            ))
            return

        if not isinstance(self.data, dict):
            self.errors.append(ValidationError(
                "error", "格式", "YAML 顶层应为 dict (包含 project 和 tasks)"
            ))

    def _check_project(self):
        """检查 project 配置"""
        if self.data is None:
            return

        proj = self.data.get("project")
        if not proj:
            self.errors.append(ValidationError(
                "error", "project", "缺少 project 配置段"
            ))
            return

        if not isinstance(proj, dict):
            self.errors.append(ValidationError(
                "error", "project", "project 应为 dict"
            ))
            return

        # repo 必填
        repo = proj.get("repo", "")
        if not repo:
            self.errors.append(ValidationError(
                "error", "project.repo", "repo 为必填项"
            ))
        else:
            # 展开路径并检查
            repo_path = Path(repo).expanduser().resolve()
            if not repo_path.exists():
                self.errors.append(ValidationError(
                    "error", "project.repo", f"仓库路径不存在: {repo_path}"
                ))
            elif not (repo_path / ".git").exists():
                self.errors.append(ValidationError(
                    "error", "project.repo",
                    f"不是 Git 仓库: {repo_path}\n"
                    f"  claude-parallel 依赖 git worktree 隔离任务，请先执行:\n"
                    f"    cd {repo_path} && git init && git add -A && git commit -m 'init'"
                ))

        # 数值范围
        max_workers = proj.get("max_workers", 3)
        if not isinstance(max_workers, int) or max_workers < 1:
            self.errors.append(ValidationError(
                "error", "project.max_workers", "应为正整数 (>= 1)"
            ))
        elif max_workers > 5:
            self.errors.append(ValidationError(
                "warning", "project.max_workers",
                f"max_workers={max_workers} 过高 (建议 <= 5)，可能触发 Claude API 限流或封禁风险"
            ))

        for field in ("default_max_turns",):
            val = proj.get(field)
            if val is not None and (not isinstance(val, int) or val < 1):
                self.errors.append(ValidationError(
                    "error", f"project.{field}", "应为正整数"
                ))

        for field in ("default_max_budget_usd", "total_budget_usd", "retry_backoff"):
            val = proj.get(field)
            if val is not None and (not isinstance(val, (int, float)) or val < 0):
                self.errors.append(ValidationError(
                    "error", f"project.{field}", "应为非负数"
                ))

        retry_count = proj.get("retry_count")
        if retry_count is not None and (not isinstance(retry_count, int) or retry_count < 0):
            self.errors.append(ValidationError(
                "error", "project.retry_count", "应为非负整数"
            ))

    def _check_tasks(self):
        """检查 tasks 配置"""
        if self.data is None:
            return

        tasks = self.data.get("tasks")
        if not tasks:
            self.errors.append(ValidationError(
                "error", "tasks", "缺少 tasks 列表"
            ))
            return

        if not isinstance(tasks, list):
            self.errors.append(ValidationError(
                "error", "tasks", "tasks 应为 list"
            ))
            return

        if len(tasks) == 0:
            self.errors.append(ValidationError(
                "error", "tasks", "tasks 列表为空"
            ))
            return

        # ID 唯一性
        seen_ids = set()
        for i, task in enumerate(tasks):
            prefix = f"tasks[{i}]"

            if not isinstance(task, dict):
                self.errors.append(ValidationError(
                    "error", prefix, "每个 task 应为 dict"
                ))
                continue

            # 必填字段
            task_id = task.get("id", "")
            if not task_id:
                self.errors.append(ValidationError(
                    "error", f"{prefix}.id", "缺少 task id"
                ))
            elif task_id in seen_ids:
                self.errors.append(ValidationError(
                    "error", f"{prefix}.id", f"重复的 task id: {task_id}"
                ))
            else:
                seen_ids.add(task_id)

            # task_id 格式安全校验：只允许 [a-zA-Z0-9_-]，长度 1-64
            if task_id and not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', task_id):
                self.errors.append(ValidationError(
                    "error", f"{prefix}.id",
                    f"task id 包含非法字符: {task_id!r} (仅允许字母、数字、下划线、连字符，长度 1-64)"
                ))

            # description
            desc = task.get("description", "")
            if not desc:
                self.errors.append(ValidationError(
                    "error", f"{prefix}.description",
                    f"任务 {task_id or i} 缺少描述"
                ))
            elif len(desc) < 10:
                self.errors.append(ValidationError(
                    "warning", f"{prefix}.description",
                    f"任务 {task_id} 描述过短 (< 10 字符)，Claude 可能无法理解意图"
                ))

            # allowed_tools
            tools = task.get("allowed_tools", [])
            valid_tools = {
                "Read", "Write", "Edit", "Bash", "MCP",
                "Glob", "Grep", "LS",
                "WebFetch", "WebSearch",
                "NotebookRead", "NotebookEdit",
                "TodoRead", "TodoWrite",
                "Task", "Exit",
            }
            if tools:
                invalid = set(tools) - valid_tools
                if invalid:
                    self.errors.append(ValidationError(
                        "warning", f"{prefix}.allowed_tools",
                        f"任务 {task_id} 含未知工具: {', '.join(invalid)}"
                    ))

            # effort
            effort = task.get("effort", "medium")
            if effort not in ("low", "medium", "high", "max"):
                self.errors.append(ValidationError(
                    "warning", f"{prefix}.effort",
                    f"任务 {task_id} effort 值无效: {effort} (应为 low/medium/high/max)"
                ))

            # 数值范围
            for num_field in ("max_turns",):
                val = task.get(num_field)
                if val is not None and (not isinstance(val, int) or val < 1):
                    self.errors.append(ValidationError(
                        "error", f"{prefix}.{num_field}",
                        f"应为正整数"
                    ))

            for num_field in ("max_budget_usd",):
                val = task.get(num_field)
                if val is not None and (not isinstance(val, (int, float)) or val < 0):
                    self.errors.append(ValidationError(
                        "error", f"{prefix}.{num_field}",
                        f"应为非负数"
                    ))

            # 启发式预警: 带 WebSearch/WebFetch 的研究类任务通常需要更多 turns/budget
            uses_web = bool(set(tools) & {"WebSearch", "WebFetch"})
            if uses_web:
                # 任务显式值 > project 默认值 > 无值
                proj_defaults = self.data.get("project", {}) if self.data else {}
                turns_val = task.get("max_turns")
                if turns_val is None:
                    turns_val = proj_defaults.get("default_max_turns")
                budget_val = task.get("max_budget_usd")
                if budget_val is None:
                    budget_val = proj_defaults.get("default_max_budget_usd")

                if turns_val is not None and isinstance(turns_val, (int, float)) and turns_val < 10:
                    source = "显式" if task.get("max_turns") is not None else "继承自 project 默认值"
                    self.errors.append(ValidationError(
                        "warning", f"{prefix}.max_turns",
                        f"任务 {task_id} 使用 WebSearch/WebFetch，但 max_turns={turns_val} ({source}) 偏低，建议 >= 10"
                    ))
                if budget_val is not None and isinstance(budget_val, (int, float)) and budget_val < 1.0:
                    source = "显式" if task.get("max_budget_usd") is not None else "继承自 project 默认值"
                    self.errors.append(ValidationError(
                        "warning", f"{prefix}.max_budget_usd",
                        f"任务 {task_id} 使用 WebSearch/WebFetch，但 max_budget_usd={budget_val} ({source}) 偏低，建议 >= 1.0"
                    ))

    def _check_dependencies(self):
        """检查依赖关系 (无环、无悬空引用)"""
        if self.data is None:
            return

        tasks = self.data.get("tasks", [])
        if not isinstance(tasks, list):
            return

        # 收集所有 ID
        all_ids = set()
        deps_map = {}
        for task in tasks:
            if not isinstance(task, dict):
                continue
            tid = task.get("id", "")
            if tid:
                all_ids.add(tid)
                deps = task.get("depends_on", [])
                if isinstance(deps, list):
                    deps_map[tid] = deps

        # 悬空引用
        for tid, deps in deps_map.items():
            for dep in deps:
                if dep not in all_ids:
                    self.errors.append(ValidationError(
                        "error", f"tasks.{tid}.depends_on",
                        f"引用了不存在的任务: {dep}"
                    ))

        # 环检测 (DFS)
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {tid: WHITE for tid in all_ids}

        def dfs(node: str) -> bool:
            color[node] = GRAY
            for dep in deps_map.get(node, []):
                if dep not in color:
                    continue
                if color[dep] == GRAY:
                    return True  # 环
                if color[dep] == WHITE and dfs(dep):
                    return True
            color[node] = BLACK
            return False

        for tid in all_ids:
            if color[tid] == WHITE:
                if dfs(tid):
                    self.errors.append(ValidationError(
                        "error", "tasks", "任务依赖存在循环 (circular dependency)"
                    ))
                    break

    def _check_tool_availability(self):
        """检查 Claude CLI 和 Git 是否可用"""
        import shutil

        if not shutil.which("claude"):
            self.errors.append(ValidationError(
                "warning", "环境",
                "claude CLI 未找到，请确认已安装: npm install -g @anthropic-ai/claude-code"
            ))

        if not shutil.which("git"):
            self.errors.append(ValidationError(
                "error", "环境", "git 未安装"
            ))

    def _check_perf_prerequisites(self):
        """P2: perf 前置校验 (xctrace/device/attach/syslog)"""
        import shutil
        import subprocess

        if not shutil.which("xcrun"):
            self.errors.append(ValidationError(
                "error", "perf.xcrun", "启用 --with-perf 时必须安装 Xcode CLI (xcrun)"
            ))
            return

        # xctrace template 可用性
        proc = subprocess.run(
            ["xcrun", "xctrace", "list", "templates"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode != 0:
            self.errors.append(ValidationError(
                "error", "perf.xctrace", f"xctrace 不可用: {(proc.stderr or proc.stdout)[:200]}"
            ))
            return

        if "Power Profiler" not in (proc.stdout or ""):
            self.errors.append(ValidationError(
                "warning", "perf.template", "未检测到 Power Profiler 模板，可能无法做功耗采集"
            ))

        # 设备检查
        if self.perf_device:
            dev = subprocess.run(
                ["xcrun", "xctrace", "list", "devices"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if dev.returncode != 0:
                self.errors.append(ValidationError(
                    "error", "perf.device", f"无法列出设备: {(dev.stderr or dev.stdout)[:200]}"
                ))
            elif self.perf_device not in (dev.stdout or ""):
                self.errors.append(ValidationError(
                    "error", "perf.device", f"指定 UDID 不在 xctrace 设备列表中: {self.perf_device}"
                ))

        # attach 进程检查 (best effort)
        if self.perf_device and self.perf_attach:
            proc = subprocess.run(
                ["xcrun", "devicectl", "device", "info", "processes", "--device", self.perf_device],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if proc.returncode == 0:
                if self.perf_attach not in (proc.stdout or ""):
                    self.errors.append(ValidationError(
                        "warning", "perf.attach",
                        f"未在设备进程列表发现 attach 目标 '{self.perf_attach}'，录制可能失败"
                    ))
            else:
                self.errors.append(ValidationError(
                    "warning", "perf.attach",
                    f"无法校验 attach 进程（跳过）: {(proc.stderr or proc.stdout)[:120]}"
                ))

        if not shutil.which("idevicesyslog"):
            self.errors.append(ValidationError(
                "warning", "perf.syslog", "未找到 idevicesyslog；将退化为 trace-only 分析"
            ))
