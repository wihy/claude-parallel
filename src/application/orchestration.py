"""
Orchestrator 调度器 — 协调多个 Claude Code Worker 的并行执行。

Phase 2 增强:
- Rich Live 实时进度监控
- 失败自动重试 (指数退避)
- 智能上下文提取 (从 Claude 输出解析 API/函数签名)
- 总预算控制
- Ctrl+C 优雅退出 + resume 恢复能力
- 流式日志管理
"""

import asyncio
import json
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.infrastructure.storage.atomic import (
    atomic_write_json, atomic_write_text, safe_read_json,
    acquire_pid_lock, release_pid_lock, list_active_locks,
)
from src.domain.tasks import (
    Task, ProjectConfig, parse_task_file,
    topological_levels, get_task_map,
)
from src.application.worker import Worker, WorkerResult, retry_worker
from src.infrastructure.monitoring.rich_monitor import Monitor
from src.application.merge import WorktreeMerger, MergeReport
from src.context_extractor import extract_context_for_downstream
from src.perf import PerfConfig, PerfSessionManager, PerfIntegrator


@dataclass
class RunStats:
    """运行统计"""
    total_tasks: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    retried: int = 0
    total_cost_usd: float = 0.0
    total_duration_s: float = 0.0
    start_time: float = 0.0
    end_time: float = 0.0


class BudgetExceeded(Exception):
    """总预算超限"""
    pass


class Orchestrator:
    """并行 Claude Code 任务调度器"""

    def __init__(
        self,
        task_file: str,
        dry_run: bool = False,
        max_retries: Optional[int] = None,
        total_budget: Optional[float] = None,
        verbose: bool = False,
        perf_config: Optional[PerfConfig] = None,
    ):
        self.task_file = task_file
        self.dry_run = dry_run
        self.max_retries_override = max_retries
        self.total_budget_override = total_budget
        self.verbose = verbose
        self.perf_config = perf_config or PerfConfig(enabled=False)
        self.perf_integrator: Optional[PerfIntegrator] = None
        self.perf_report: Optional[dict] = None
        self.config: Optional[ProjectConfig] = None
        self.tasks: list[Task] = []
        self.levels: list[list[Task]] = []
        self.task_map: dict[str, Task] = {}
        self.workers: dict[str, Worker] = {}
        self.results: dict[str, WorkerResult] = {}
        self.coord_dir: Optional[Path] = None
        self.stats = RunStats()
        self.monitor: Optional[Monitor] = None
        self._cancelled = False
        self._current_level_idx = 0

        # 封禁防护: 全局 Claude CLI 调用计数
        self._claude_call_count = 0
        self._claude_call_timestamps: list[float] = []
        self._claude_call_limit_per_minute = 10  # 每分钟最多启动 10 个 Claude 进程

    def load(self):
        """加载和解析任务文件"""
        self.config, self.tasks = parse_task_file(self.task_file)
        self.levels = topological_levels(self.tasks)
        self.task_map = get_task_map(self.tasks)
        self.coord_dir = Path(self.config.repo) / self.config.coordination_dir
        self.stats.total_tasks = len(self.tasks)

        # 应用覆盖值
        if self.max_retries_override is not None:
            self.config.retry_count = self.max_retries_override
        if self.total_budget_override is not None:
            self.config.total_budget_usd = self.total_budget_override

        # 封禁防护: max_workers 硬上限
        if self.config.max_workers > 5:
            print(f"  [警告] max_workers={self.config.max_workers} 过高，已限制为 5")
            self.config.max_workers = 5

        # 创建协调目录
        self.coord_dir.mkdir(parents=True, exist_ok=True)
        (self.coord_dir / "coord").mkdir(parents=True, exist_ok=True)
        (self.coord_dir / "context").mkdir(parents=True, exist_ok=True)
        (self.coord_dir / "results").mkdir(parents=True, exist_ok=True)
        (self.coord_dir / "logs").mkdir(parents=True, exist_ok=True)

        # 保存执行计划快照 (用于 resume)
        self._save_plan_snapshot()

        # Perf 集成管理器
        if self.perf_config.enabled:
            self.perf_integrator = PerfIntegrator(
                config=self.perf_config,
                repo=self.config.repo,
                coordination_dir=self.config.coordination_dir,
            )

    def _save_plan_snapshot(self):
        """保存执行计划快照，支持 resume"""
        snapshot = {
            "task_file": str(Path(self.task_file).resolve()),
            "timestamp": time.time(),
            "tasks": [
                {
                    "id": t.id,
                    "status": t.status,
                    "depends_on": t.depends_on,
                }
                for t in self.tasks
            ],
            "levels": [
                [t.id for t in level]
                for level in self.levels
            ],
        }
        snapshot_file = self.coord_dir / "plan-snapshot.json"
        atomic_write_json(snapshot_file, snapshot)

    # ── 智能上下文提取 ──

    def _extract_context(self, task: Task, result: WorkerResult):
        """
        使用增强的上下文提取器，从 Claude 输出中提取接口/API 定义。
        支持 Python/JS/Go/Rust/Java 多语言。
        """
        if not result.success or not result.output:
            return

        context = extract_context_for_downstream(
            result.output, task.id, task.files
        )

        ctx_file = self.coord_dir / "context" / f"{task.id}.md"
        atomic_write_text(ctx_file, context)

    def _build_dependency_context(self, task: Task) -> str:
        """从上游任务结果中构建依赖上下文注入 prompt"""
        if not task.depends_on:
            return ""

        context_parts = []
        for dep_id in task.depends_on:
            dep_result = self.results.get(dep_id)
            if not dep_result or not dep_result.success:
                context_parts.append(f"[上游任务 {dep_id} 未成功完成，请谨慎处理]")
                continue

            ctx = f"--- 上游任务: {dep_id} ---\n"
            if dep_result.output:
                output = dep_result.output
                if len(output) > 3000:
                    output = output[:3000] + "\n... (已截断)"
                ctx += f"产出:\n{output}\n"

            # 读取智能提取的上下文
            ctx_file = self.coord_dir / "context" / f"{dep_id}.md"
            if ctx_file.exists():
                ctx_data = ctx_file.read_text()
                if ctx_data and ctx_data != dep_result.output[:3000]:
                    ctx += f"\n接口/代码摘要:\n{ctx_data}\n"

            context_parts.append(ctx)

        return "\n\n".join(context_parts)

    # ── 核心执行逻辑 ──

    async def run(self) -> dict:
        """执行所有任务，返回结果汇总"""
        if not self.config:
            self.load()

        self.stats.start_time = time.time()
        self._cancelled = False

        if self.dry_run:
            return self._dry_run()

        if self.perf_integrator:
            self.perf_integrator.on_run_start()

        # 注册 Ctrl+C 处理
        loop = asyncio.get_event_loop()
        original_sigint = signal.getsignal(signal.SIGINT)

        def _sigint_handler(sig, frame):
            self._cancelled = True
            if self.verbose:
                print("\n  [中断] 正在优雅停止... 等待当前任务完成")

        signal.signal(signal.SIGINT, _sigint_handler)

        # 注册 PID 锁，让 cleanup 类操作能识别本实例正在使用
        lock_file = acquire_pid_lock(self.coord_dir / ".locks")

        try:
            # 启动监控面板
            self.monitor = Monitor(self)
            self.monitor.start()

            # 按层级执行
            for level_idx, level in enumerate(self.levels):
                if self._cancelled:
                    self._cancel_remaining()
                    break

                # 检查预算
                if self._budget_exceeded():
                    print(f"\n  [预算] 总预算 ${self.config.total_budget_usd:.2f} 已耗尽")
                    self._cancel_remaining()
                    break

                self._current_level_idx = level_idx
                if self.monitor:
                    self.monitor.set_current_level(level_idx)
                if self.perf_integrator:
                    self.perf_integrator.on_level_start(level_idx, [t.id for t in level])

                await self._run_level(level)

                if self.perf_integrator:
                    self.perf_integrator.on_level_end(level_idx, [t.id for t in level])

            self.stats.end_time = time.time()
            self.stats.total_duration_s = self.stats.end_time - self.stats.start_time

        finally:
            # 恢复信号处理
            signal.signal(signal.SIGINT, original_sigint)
            # 停止监控
            if self.monitor:
                self.monitor.stop()
            if self.perf_integrator:
                self.perf_report = self.perf_integrator.on_run_end()
            # 释放 PID 锁
            release_pid_lock(lock_file)

        # 汇总报告
        report = self._generate_report()
        self._save_report(report)
        return report

    async def _run_level(self, level: list[Task]):
        """并行执行一个层级的所有任务"""
        semaphore = asyncio.Semaphore(self.config.max_workers)

        # 封禁防护: 同层任务启动间随机延迟 (2-5s)，避免同时冲击 API
        import random
        stagger_delay_range = (2.0, 5.0)

        async def run_with_semaphore(task: Task, stagger_index: int = 0):
            # 随机延迟: 第一个立即开始，后续错开
            if stagger_index > 0:
                delay = random.uniform(*stagger_delay_range) * (1 + stagger_index * 0.3)
                delay = min(delay, 15.0)  # 最大延迟 15s
                if self.verbose:
                    print(f"  [防冲] 任务 {task.id} 延迟 {delay:.1f}s 启动")
                await asyncio.sleep(delay)

            async with semaphore:
                if self._cancelled:
                    self._cancel_task(task, "用户中断")
                    return

                # 封禁防护: 调用速率检查
                await self._rate_limit_check()

                # 检查上游是否全部成功；区分 cancelled 与 failed 给出更准确的原因
                if task.depends_on:
                    bad_deps = []
                    for dep_id in task.depends_on:
                        dep_res = self.results.get(dep_id)
                        if not dep_res or not dep_res.success:
                            dep_task = self.task_map.get(dep_id)
                            dep_status = (dep_task.status if dep_task else "unknown")
                            bad_deps.append(f"{dep_id}({dep_status})")
                    if bad_deps:
                        # 任一上游为 cancelled → 当前任务也属于级联取消，而非"失败"
                        cascade_cancelled = any(
                            self.task_map.get(dep_id)
                            and self.task_map[dep_id].status == "cancelled"
                            for dep_id in task.depends_on
                        )
                        prefix = "上游级联取消" if cascade_cancelled else "上游依赖任务失败"
                        self._cancel_task(task, f"{prefix}: {', '.join(bad_deps)}")
                        return

                # 检查预算
                if self._budget_exceeded():
                    self._cancel_task(task, "总预算耗尽")
                    return

                # 构建依赖上下文
                dep_ctx = self._build_dependency_context(task)

                # 使用 retry_worker 执行 (带重试)
                result = await retry_worker(
                    task=task,
                    config=self.config,
                    coord_dir=self.coord_dir,
                    dep_ctx=dep_ctx,
                    max_retries=self.config.retry_count,
                    base_backoff=self.config.retry_backoff,
                )

                self.results[task.id] = result

                # 更新统计
                if result.success:
                    self.stats.completed += 1
                else:
                    self.stats.failed += 1
                # 无论成功失败都累加成本
                self.stats.total_cost_usd += result.cost_usd

                if result.retry_attempt > 0:
                    self.stats.retried += 1

                # 更新监控
                if self.monitor:
                    self.monitor.update()

                # 提取上下文供下游使用
                self._extract_context(task, result)

        # 并行执行 (带启动延迟防冲)
        await asyncio.gather(*[
            run_with_semaphore(t, stagger_index=i)
            for i, t in enumerate(level)
        ])

    def _cancel_task(self, task: Task, reason: str):
        """取消单个任务，并持久化 cancelled 状态供 resume/merge/report 使用。"""
        result = WorkerResult(
            task_id=task.id,
            success=False,
            error=reason,
        )
        self.results[task.id] = result
        task.status = "cancelled"
        self.stats.skipped += 1

        # 持久化 cancelled 结果，避免 resume 后语义漂移成 failed
        if self.coord_dir:
            result_file = self.coord_dir / "coord" / f"{task.id}.result"
            summary = {
                "task_id": task.id,
                "success": False,
                "status": "cancelled",
                "task_hash": getattr(task, "signature", ""),
                "cost_usd": 0.0,
                "duration_s": 0.0,
                "num_turns": 0,
                "model_used": "",
                "worktree_path": "",
                "output_summary": "",
                "error": reason[:1000] if reason else "",
                "retry_attempt": 0,
            }
            atomic_write_json(result_file, summary)

            status_file = self.coord_dir / "coord" / f"{task.id}.STATUS"
            atomic_write_text(status_file, f"CANCELLED\n{time.time()}\n")

    def _cancel_remaining(self):
        """取消所有剩余任务"""
        for task in self.tasks:
            if task.status in ("pending", "running"):
                self._cancel_task(task, "用户中断")

    def _budget_exceeded(self) -> bool:
        """检查是否超出总预算"""
        if self.config.total_budget_usd <= 0:
            return False
        return self.stats.total_cost_usd >= self.config.total_budget_usd

    async def _rate_limit_check(self):
        """封禁防护: 检查 Claude CLI 调用速率，超限则等待"""
        now = time.time()
        # 清理 60s 前的记录
        self._claude_call_timestamps = [
            t for t in self._claude_call_timestamps
            if now - t < 60
        ]
        if len(self._claude_call_timestamps) >= self._claude_call_limit_per_minute:
            # 等到最早的调用超过 60s 前
            oldest = self._claude_call_timestamps[0]
            wait_time = 60 - (now - oldest) + 1
            print(f"  [防封] 近 60s 已调用 {len(self._claude_call_timestamps)} 次，"
                  f"等待 {wait_time:.0f}s")
            await asyncio.sleep(wait_time)

        self._claude_call_count += 1
        self._claude_call_timestamps.append(time.time())

    # ── Resume 恢复 ──

    async def resume(self) -> dict:
        """
        从上次中断处恢复执行。
        读取已有结果，跳过已完成/失败的任务，继续未完成的。
        """
        if not self.config:
            self.load()

        # 加载已有结果（签名不匹配 / 文件损坏 → 忽略，重新执行）
        stale_count = 0
        for task in self.tasks:
            result_file = self.coord_dir / "coord" / f"{task.id}.result"
            data = safe_read_json(result_file)
            if not data:
                continue
            try:
                stored_hash = data.get("task_hash", "")
                if stored_hash and task.signature and stored_hash != task.signature:
                    # YAML 被修改：丢弃旧结果，强制重跑
                    stale_count += 1
                    print(f"  [resume] 任务 '{task.id}' 签名不匹配，将重新执行")
                    continue
                self.results[task.id] = WorkerResult(
                    task_id=data["task_id"],
                    success=data["success"],
                    cost_usd=data.get("cost_usd", 0),
                    duration_s=data.get("duration_s", 0),
                    num_turns=data.get("num_turns", 0),
                    model_used=data.get("model_used", ""),
                    worktree_path=data.get("worktree_path", ""),
                    output=data.get("output_summary", ""),
                    error=data.get("error", ""),
                )
                persisted_status = data.get("status", "")
                if data["success"]:
                    task.status = "done"
                    self.stats.completed += 1
                elif persisted_status == "cancelled":
                    task.status = "cancelled"
                    self.stats.skipped += 1
                else:
                    task.status = "failed"
                    self.stats.failed += 1
                # 无论成功失败都累加成本
                self.stats.total_cost_usd += data.get("cost_usd", 0)
            except KeyError:
                pass

        if stale_count:
            print(f"  [resume] 共 {stale_count} 个任务因定义变更需重新执行")

        # 重新过滤需要执行的任务
        remaining = [t for t in self.tasks if t.status == "pending"]
        if not remaining:
            print("  所有任务已完成，无需恢复")
            report = self._generate_report()
            self._save_report(report)
            return report

        print(f"  恢复执行: {len(remaining)} 个待执行任务")

        # 重建层级 (只包含有 pending 任务的层级)
        # 简单策略: 直接执行所有 pending 任务 (依赖已完成的自动获得上下文)
        self._cancelled = False
        self.stats.start_time = time.time()

        if self.perf_integrator:
            self.perf_integrator.on_run_start()

        original_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, lambda s, f: setattr(self, '_cancelled', True))

        # PID 锁: resume 期间也持有，防止并发 cleanup
        lock_file = acquire_pid_lock(self.coord_dir / ".locks")

        try:
            self.monitor = Monitor(self)
            self.monitor.start()

            # 使用原有层级，但跳过已完成的任务
            for level_idx, level in enumerate(self.levels):
                if self._cancelled:
                    self._cancel_remaining()
                    break

                pending_in_level = [t for t in level if t.status == "pending"]
                if not pending_in_level:
                    continue

                self._current_level_idx = level_idx
                if self.monitor:
                    self.monitor.set_current_level(level_idx)
                if self.perf_integrator:
                    self.perf_integrator.on_level_start(level_idx, [t.id for t in pending_in_level])

                await self._run_level(pending_in_level)

                if self.perf_integrator:
                    self.perf_integrator.on_level_end(level_idx, [t.id for t in pending_in_level])

        finally:
            signal.signal(signal.SIGINT, original_sigint)
            if self.monitor:
                self.monitor.stop()
            if self.perf_integrator:
                self.perf_report = self.perf_integrator.on_run_end()
            release_pid_lock(lock_file)

        self.stats.end_time = time.time()
        self.stats.total_duration_s = self.stats.end_time - self.stats.start_time

        report = self._generate_report()
        self._save_report(report)
        return report

    # ── Dry Run ──

    def _dry_run(self) -> dict:
        """干跑: 只展示执行计划"""
        print(f"\n{'='*60}")
        print(f"  Claude Parallel — DRY RUN")
        print(f"{'='*60}\n")

        for level_idx, level in enumerate(self.levels):
            print(f"  层级 {level_idx}:")
            for task in level:
                deps = f" (依赖: {', '.join(task.depends_on)})" if task.depends_on else ""
                files = ", ".join(task.files[:3]) if task.files else "(自动)"
                print(
                    f"    {task.id}: {task.description[:50]}... "
                    f"[{files}]{deps}"
                )
                print(
                    f"      tools={','.join(task.allowed_tools)} "
                    f"turns={task.max_turns} budget=${task.max_budget_usd}"
                )
            print()

        print(f"  总任务: {len(self.tasks)}, 层级: {len(self.levels)}")
        print(f"  重试: {self.config.retry_count} 次, 退避: {self.config.retry_backoff}s")
        if self.config.total_budget_usd > 0:
            print(f"  总预算上限: ${self.config.total_budget_usd:.2f}")
        return {"dry_run": True, "total_tasks": len(self.tasks)}

    # ── 报告 ──

    def _generate_report(self) -> dict:
        """生成执行报告"""
        report = {
            "summary": {
                "total_tasks": self.stats.total_tasks,
                "completed": self.stats.completed,
                "failed": self.stats.failed,
                "skipped": self.stats.skipped,
                "retried": self.stats.retried,
                "total_cost_usd": round(self.stats.total_cost_usd, 3),
                "total_duration_s": round(self.stats.total_duration_s, 1),
                "success_rate": (
                    f"{self.stats.completed}/{self.stats.total_tasks}"
                    f" ({100*self.stats.completed/self.stats.total_tasks:.0f}%)"
                    if self.stats.total_tasks > 0 else "N/A"
                ),
            },
            "tasks": [],
        }

        for task_id, result in self.results.items():
            report["tasks"].append({
                "id": task_id,
                "success": result.success,
                "status": next((t.status for t in self.tasks if t.id == task_id), "unknown"),
                "cost_usd": result.cost_usd,
                "duration_s": round(result.duration_s, 1),
                "num_turns": result.num_turns,
                "model": result.model_used,
                "worktree": result.worktree_path,
                "error": result.error[:200] if result.error else "",
                "retries": result.retry_attempt,
            })

        if self.perf_report:
            report["perf"] = self.perf_report

        return report

    def _save_report(self, report: dict):
        """保存报告到文件"""
        report_file = self.coord_dir / "results" / f"report-{int(time.time())}.json"
        atomic_write_json(report_file, report)

    # ── Worktree 管理 ──

    async def merge_worktrees(self) -> bool:
        """
        将所有成功的 worktree 合并回主分支。
        支持冲突检测和自动报告。
        """
        if not self.config:
            return False

        print(f"\n{'='*60}")
        print(f"  合并 Worktrees")
        print(f"{'='*60}\n")

        successful = []
        for level in self.levels:
            for task in level:
                result = self.results.get(task.id)
                if result and result.success and result.worktree_path:
                    successful.append((task, result))

        if not successful:
            print("  没有需要合并的 worktree")
            return True

        # 按层级顺序逐个合并
        merged = []
        conflicts = []

        for task, result in successful:
            wt_path = result.worktree_path

            # 获取 worktree 中的所有新 commit
            proc = await asyncio.create_subprocess_exec(
                "git", "log", "HEAD", "--not", "--remotes", "--oneline",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=wt_path,
            )
            stdout, _ = await proc.communicate()
            commits = stdout.decode().strip()

            if not commits:
                # 没有新 commit (Claude 可能没有做修改)
                # 检查 worktree 中的 diff
                proc = await asyncio.create_subprocess_exec(
                    "git", "diff", "--name-only", "HEAD",
                    stdout=asyncio.subprocess.PIPE,
                    cwd=wt_path,
                )
                stdout, _ = await proc.communicate()
                changed_files = stdout.decode().strip()

                if not changed_files:
                    print(f"  [跳过] {task.id} — 无变更")
                    continue

                # 有未提交的变更，先 commit
                proc = await asyncio.create_subprocess_exec(
                    "git", "add", "-A",
                    cwd=wt_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()

                proc = await asyncio.create_subprocess_exec(
                    "git", "commit", "-m", f"feat: {task.id} - {task.description[:50]}",
                    cwd=wt_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()

            # 获取 worktree 的 HEAD commit
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "HEAD",
                stdout=asyncio.subprocess.PIPE,
                cwd=wt_path,
            )
            stdout, _ = await proc.communicate()
            head_commit = stdout.decode().strip()

            if not head_commit:
                print(f"  [跳过] {task.id} — 无法获取 HEAD")
                continue

            # 在主仓库中 cherry-pick
            proc = await asyncio.create_subprocess_exec(
                "git", "cherry-pick", head_commit, "--no-commit",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.config.repo,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                # 检查是否有实际变更被 apply
                proc = await asyncio.create_subprocess_exec(
                    "git", "diff", "--cached", "--name-only",
                    stdout=asyncio.subprocess.PIPE,
                    cwd=self.config.repo,
                )
                stdout, _ = await proc.communicate()
                if stdout.decode().strip():
                    proc = await asyncio.create_subprocess_exec(
                        "git", "commit", "-m",
                        f"merge: {task.id} - {task.description[:50]}",
                        cwd=self.config.repo,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await proc.communicate()
                    print(f"  [✓] {task.id} → 已合并")
                else:
                    # cherry-pick 成功但无变更（可能是空变更）
                    proc = await asyncio.create_subprocess_exec(
                        "git", "cherry-pick", "--abort",
                        cwd=self.config.repo,
                        stdout=asyncio.subprocess.PIPE,
                    )
                    await proc.communicate()
                    print(f"  [跳过] {task.id} — cherry-pick 无变更")

                merged.append(task.id)
            else:
                err = stderr.decode(errors="replace")
                print(f"  [✗] {task.id} → 合并冲突: {err[:100]}")
                conflicts.append((task.id, err[:200]))
                # 中止
                proc = await asyncio.create_subprocess_exec(
                    "git", "cherry-pick", "--abort",
                    cwd=self.config.repo,
                    stdout=asyncio.subprocess.PIPE,
                )
                await proc.communicate()

        if conflicts:
            print(f"\n  冲突汇总:")
            for tid, err in conflicts:
                print(f"    {tid}: {err[:100]}")

        return len(merged) == len(successful)

    async def cleanup_worktrees(self, force: bool = False):
        """清理所有 worktree。

        默认会检查 coord_dir/.locks/ 下是否有其他活动 cpar 实例；存在则拒绝清理。
        force=True 可绕过检查（仅在确认无人使用时使用）。
        """
        if not self.config:
            return

        # 占用检查: 防止并行实例的 worktree 被误删
        active = list_active_locks(self.coord_dir / ".locks", exclude_self=True)
        if active and not force:
            print(f"  [cleanup] 拒绝清理: 检测到 {len(active)} 个其他 cpar 实例正在运行 (PID: {active})")
            print(f"  [cleanup] 如确认无冲突，使用 force=True 绕过检查")
            return

        for task_id in self.task_map:
            wt_path = Path(self.config.repo) / ".claude" / "worktrees" / f"cp-{task_id}"
            if wt_path.exists():
                proc = await asyncio.create_subprocess_exec(
                    "git", "worktree", "remove", str(wt_path), "--force",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.config.repo,
                )
                await proc.communicate()

        print("  Worktree 清理完成")
