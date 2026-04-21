"""
Worker 进程管理器 — 启动/监控/停止/重试 单个 Claude Code 实例。

Phase 2 增强:
- 流式 stderr 日志实时写入文件
- WorkerLogger 日志管理
- retry_worker 失败自动重试机制
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .infrastructure.storage.atomic import atomic_write_json, atomic_write_text
from .domain.tasks import Task, ProjectConfig


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


class WorkerLogger:
    """Worker 日志管理器 — 带时间戳写入日志文件"""

    def __init__(self, task_id: str, log_dir: Path):
        self.task_id = task_id
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = log_dir / f"{task_id}.log"

    def _write(self, level: str, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{level}] {msg}\n"
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(line)

    def info(self, msg: str):
        self._write("INFO", msg)

    def warning(self, msg: str):
        self._write("WARN", msg)

    def error(self, msg: str):
        self._write("ERROR", msg)

    def section(self, title: str):
        sep = "=" * 50
        ts = datetime.now().strftime("%H:%M:%S")
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(f"\n[{ts}] {sep}\n[{ts}]   {title}\n[{ts}] {sep}\n")


class Worker:
    """单个 Claude Code Worker 进程管理"""

    def __init__(self, task: Task, config: ProjectConfig, coord_dir: Path):
        self.task = task
        self.config = config
        self.coord_dir = coord_dir
        self.process: Optional[asyncio.subprocess.Process] = None
        self.result: Optional[WorkerResult] = None
        self.start_time: float = 0
        self._killed = False
        self.logger = WorkerLogger(task.id, coord_dir / "logs")
        self._stderr_lines: list[str] = []

    def _build_command(self, dependency_context: str = "") -> list[str]:
        """构建 claude 命令行"""
        cmd = ["claude"]

        # Print 模式
        cmd.append("-p")

        # 构建 prompt
        prompt_parts = [
            f"## 任务: {self.task.description}",
            "",
            "### 涉及文件",
        ]
        if self.task.files:
            prompt_parts.append(", ".join(f"`{f}`" for f in self.task.files))
        else:
            prompt_parts.append("(根据任务需要自行确定)")

        if dependency_context:
            prompt_parts.extend([
                "",
                "### 上游任务的产出 (你必须基于这些接口/约定来工作)",
                dependency_context,
            ])

        if self.task.extra_prompt:
            prompt_parts.extend(["", "### 额外指令", self.task.extra_prompt])

        prompt_parts.extend([
            "",
            "### 要求",
            "- 完成任务后，列出你创建或修改的所有文件及关键变更摘要",
            "- 如果创建了新的 API 或接口，明确列出签名",
            "- 确保代码可以直接运行，不要留 TODO 或占位符",
        ])

        prompt = "\n".join(prompt_parts)
        cmd.append(prompt)

        # Worktree 隔离
        cmd.extend(["-w", f"cp-{self.task.id}"])

        # 工具限制
        if self.task.allowed_tools:
            cmd.extend(["--allowedTools", ",".join(self.task.allowed_tools)])

        # Turn 限制
        cmd.extend(["--max-turns", str(self.task.max_turns)])

        # 预算限制
        cmd.extend(["--max-budget-usd", str(self.task.max_budget_usd)])

        # Effort
        cmd.extend(["--effort", self.task.effort])

        # 模型
        if self.task.model:
            cmd.extend(["--model", self.task.model])

        # JSON 输出
        cmd.extend(["--output-format", "json"])

        # 跳过权限确认 (自动化必须)
        # 注意: 不使用 --bare，因为需要 OAuth 认证 (Max 账号)
        cmd.append("--dangerously-skip-permissions")

        return cmd

    def _build_env(self) -> dict:
        """构建子进程环境变量"""
        env = os.environ.copy()
        env["LANG"] = "en_US.UTF-8"
        env["LC_ALL"] = "en_US.UTF-8"
        return env

    async def start(self, dependency_context: str = "") -> None:
        """启动 Worker 进程"""
        cmd = self._build_command(dependency_context)
        env = self._build_env()

        self.start_time = time.time()
        self.task.status = "running"
        self._stderr_lines = []

        self.logger.section(f"启动 Worker: {self.task.id}")
        self.logger.info(f"命令: {' '.join(cmd[:6])}... (prompt truncated)")
        self.logger.info(f"工作目录: {self.config.repo}")

        # 写入启动标记
        self._write_status_file("STARTED")

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.config.repo,
            env=env,
        )

        # 启动后台任务实时读取 stderr
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self):
        """持续读取 stderr 流，实时写入日志文件。

        使用单一持久 file handle 避免每行都 open/close（FD 抖动）。
        异常不再静默吞掉：CancelledError 上抛，其他异常落日志。
        """
        if not self.process or not self.process.stderr:
            return
        try:
            # buffering=1 = 行缓冲；保证 tail -f 可见
            with open(self.logger.log_file, "a", encoding="utf-8", buffering=1) as f:
                while True:
                    line = await self.process.stderr.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        self._stderr_lines.append(text)
                        f.write(f"  [stderr] {text}\n")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 不能再吞掉异常 — 至少留下痕迹
            try:
                self.logger.warning(f"_drain_stderr 异常 ({type(e).__name__}): {e}")
            except Exception:
                pass

    # stdout 上限 — 防止子进程日志爆炸导致 OOM
    MAX_STDOUT_BYTES = 50 * 1024 * 1024  # 50 MB

    async def wait(self, timeout: float = 600) -> WorkerResult:
        """等待 Worker 完成，返回结果"""
        if not self.process:
            raise RuntimeError(f"Worker {self.task.id} 未启动")

        stdout_chunks: list[bytes] = []
        bytes_read = 0
        truncated = False

        async def _read_stdout():
            nonlocal bytes_read, truncated
            while True:
                chunk = await self.process.stdout.read(64 * 1024)
                if not chunk:
                    break
                bytes_read += len(chunk)
                if bytes_read > self.MAX_STDOUT_BYTES:
                    if not truncated:
                        truncated = True
                        self.logger.warning(
                            f"stdout 超过 {self.MAX_STDOUT_BYTES // (1024*1024)}MB，已截断"
                        )
                    # 继续 drain 避免子进程因管道写满阻塞
                    continue
                stdout_chunks.append(chunk)

        try:
            # 只读 stdout; stderr 由 _drain_stderr 负责读取
            # 不能用 communicate()，因为它会同时读 stderr 造成冲突
            await asyncio.wait_for(_read_stdout(), timeout=timeout)
            # 等待进程退出
            await asyncio.wait_for(self.process.wait(), timeout=10)
        except asyncio.TimeoutError:
            await self._kill()
            if hasattr(self, "_stderr_task"):
                self._stderr_task.cancel()
                try:
                    await self._stderr_task
                except (asyncio.CancelledError, Exception):
                    pass
            self.logger.error(f"超时 ({timeout}s)")
            self.result = WorkerResult(
                task_id=self.task.id,
                success=False,
                error=f"超时 ({timeout}s)",
                duration_s=time.time() - self.start_time,
            )
            self.task.status = "failed"
            self._write_status_file("TIMEOUT")
            return self.result

        # 等待 stderr drain 完成 — 子进程已退出，drain 应快速 EOF
        # 给较大的兜底超时 (30s)，避免极端情况下永久阻塞
        if hasattr(self, "_stderr_task"):
            try:
                await asyncio.wait_for(self._stderr_task, timeout=30)
            except asyncio.TimeoutError:
                self.logger.warning("stderr drain 30s 内未完成，强制取消")
                self._stderr_task.cancel()
                try:
                    await self._stderr_task
                except (asyncio.CancelledError, Exception):
                    pass
            except asyncio.CancelledError:
                pass

        duration = time.time() - self.start_time

        # 拼接 stdout (可能被截断)
        stdout = b"".join(stdout_chunks)
        # 解析 JSON 输出
        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = "\n".join(self._stderr_lines)

        result = WorkerResult(
            task_id=self.task.id,
            success=False,
            duration_s=duration,
        )

        if stdout_text:
            try:
                data = json.loads(stdout_text)
                result.json_raw = data
                result.success = data.get("subtype") == "success"
                result.output = data.get("result", "")
                result.session_id = data.get("session_id", "")
                result.cost_usd = data.get("total_cost_usd", 0.0)
                result.num_turns = data.get("num_turns", 0)
                result.stop_reason = data.get("stop_reason", "")

                # 提取错误信息: subtype 不是 success 时记录原因
                if not result.success:
                    error_parts = []
                    subtype = data.get("subtype", "")
                    if subtype:
                        error_parts.append(f"subtype={subtype}")
                    is_error = data.get("is_error", False)
                    if is_error:
                        error_parts.append("is_error=true")
                    # 有些错误在 result 字段里
                    result_text = data.get("result", "")
                    if result_text:
                        error_parts.append(result_text[:500])
                    if error_parts:
                        result.error = " | ".join(error_parts)[:2000]

                # 提取使用的模型
                model_usage = data.get("modelUsage", {})
                if model_usage:
                    result.model_used = list(model_usage.keys())[0]

                # 提取 worktree 路径
                if result.success:
                    wt = Path(self.config.repo) / ".claude" / "worktrees" / f"cp-{self.task.id}"
                    result.worktree_path = str(wt) if wt.exists() else ""

                self.logger.info(f"结果: success={result.success}, cost=${result.cost_usd:.3f}, "
                                 f"turns={result.num_turns}, duration={duration:.1f}s")

            except json.JSONDecodeError:
                result.output = stdout_text
                result.success = self.process.returncode == 0
                self.logger.warning(f"JSON 解析失败, returncode={self.process.returncode}")

        if not result.success and stderr_text:
            result.error = stderr_text[:2000]
            self.logger.error(f"stderr (最后 500 字符): {stderr_text[-500:]}")

        self.result = result
        self.task.status = "done" if result.success else "failed"

        # 写入结果文件
        self._write_result_file(result)
        self._write_status_file("DONE" if result.success else "FAILED")

        return result

    async def _kill(self, grace_seconds: float = 5.0):
        """强制终止 Worker (terminate → 等 grace_seconds → kill -9)"""
        if not self.process or self.process.returncode is not None:
            return
        self._killed = True
        try:
            self.process.terminate()
            self.logger.warning("Worker 被 terminate")
        except ProcessLookupError:
            return
        except Exception as e:
            self.logger.warning(f"terminate 失败: {e}")

        try:
            await asyncio.wait_for(self.process.wait(), timeout=grace_seconds)
            return
        except asyncio.TimeoutError:
            pass

        try:
            self.process.kill()
            self.logger.warning(f"Worker 未在 {grace_seconds}s 内退出，已 SIGKILL")
        except ProcessLookupError:
            return
        except Exception as e:
            self.logger.error(f"SIGKILL 失败: {e}")
            return

        try:
            await asyncio.wait_for(self.process.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            self.logger.error("Worker 在 SIGKILL 后仍未回收")

    async def cleanup_worktree(self):
        """清理当前 Worker 的 worktree + 同名 cp-* 分支（重试前调用）"""
        wt_path = Path(self.config.repo) / ".claude" / "worktrees" / f"cp-{self.task.id}"
        branch_name = f"cp-{self.task.id}"
        if wt_path.exists():
            proc = await asyncio.create_subprocess_exec(
                "git", "worktree", "remove", str(wt_path), "--force",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.config.repo,
            )
            await proc.communicate()
            self.logger.info(f"已清理 worktree: {wt_path}")

        # 删除占位分支，防止重试时 "branch already exists" 冲突
        proc = await asyncio.create_subprocess_exec(
            "git", "branch", "-D", branch_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.config.repo,
        )
        await proc.communicate()

    def _write_status_file(self, status: str):
        """写入状态标记文件（原子写）"""
        status_file = self.coord_dir / "coord" / f"{self.task.id}.STATUS"
        atomic_write_text(status_file, f"{status}\n{time.time()}\n")

    def _write_result_file(self, result: WorkerResult):
        """写入结果摘要文件（原子写）"""
        result_file = self.coord_dir / "coord" / f"{self.task.id}.result"

        summary = {
            "task_id": result.task_id,
            "success": result.success,
            "status": self.task.status,
            "task_hash": getattr(self.task, "signature", ""),
            "cost_usd": result.cost_usd,
            "duration_s": round(result.duration_s, 1),
            "num_turns": result.num_turns,
            "model_used": result.model_used,
            "worktree_path": result.worktree_path,
            "output_summary": result.output[:3000] if result.output else "",
            "error": result.error[:1000] if result.error else "",
            "retry_attempt": result.retry_attempt,
        }
        atomic_write_json(result_file, summary)

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @property
    def elapsed(self) -> float:
        if self.start_time == 0:
            return 0
        return time.time() - self.start_time


# ── 可重试的错误模式 ──
RETRYABLE_ERRORS = [
    "rate_limit",
    "overloaded",
    "timeout",
    "429",
    "503",
    "connection",
    "ECONNRESET",
    "ECONNREFUSED",
    "temporary",
    "Internal server error",
    "too many requests",
    "quota",
    "capacity",
]

# Rate limit 类错误使用更长的退避
RATE_LIMIT_ERRORS = ["rate_limit", "429", "too many requests", "capacity"]


def is_retryable_error(result: WorkerResult) -> bool:
    """判断失败是否值得重试"""
    if result.success:
        return False
    error = (result.error or "").lower()
    output = (result.output or "").lower()
    combined = error + " " + output
    return any(pat.lower() in combined for pat in RETRYABLE_ERRORS)


async def retry_worker(
    task: Task,
    config: ProjectConfig,
    coord_dir: Path,
    dep_ctx: str = "",
    max_retries: int = 2,
    base_backoff: float = 5.0,
) -> WorkerResult:
    """
    带重试的 Worker 执行。

    如果失败且错误可重试，自动清理 worktree 并重试。
    使用指数退避: base_backoff * 2^attempt
    """
    logger = WorkerLogger(task.id, coord_dir / "logs")
    last_result = None

    for attempt in range(max_retries + 1):
        if attempt > 0:
            # 指数退避 — rate limit 错误使用更长退避
            is_rate_limit = any(
                pat.lower() in (last_result.error or "").lower()
                for pat in RATE_LIMIT_ERRORS
            )
            if is_rate_limit:
                backoff = max(base_backoff * 8, 30.0) * (2 ** (attempt - 1))
                backoff = min(backoff, 300.0)  # 最大 5 分钟
            else:
                backoff = base_backoff * (2 ** (attempt - 1))
            logger.section(f"重试 #{attempt} (等待 {backoff:.0f}s)")
            logger.info(f"上次失败原因: {last_result.error[:200] if last_result and last_result.error else 'unknown'}")
            await asyncio.sleep(backoff)

            # 清理旧 worktree + 同名分支（否则新 Worker 的 -w cp-{task.id} 会失败）
            old_wt = Path(config.repo) / ".claude" / "worktrees" / f"cp-{task.id}"
            if old_wt.exists():
                proc = await asyncio.create_subprocess_exec(
                    "git", "worktree", "remove", str(old_wt), "--force",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=config.repo,
                )
                await proc.communicate()
                logger.info("已清理旧 worktree")
            proc = await asyncio.create_subprocess_exec(
                "git", "branch", "-D", f"cp-{task.id}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=config.repo,
            )
            await proc.communicate()

        task.status = "pending"
        worker = Worker(task, config, coord_dir)

        if attempt > 0:
            task.status = "retrying"
            logger.info(f"启动重试 Worker #{attempt}")

        await worker.start(dep_ctx)
        timeout = max(task.max_turns * 60, 300)
        result = await worker.wait(timeout=timeout)
        result.retry_attempt = attempt

        if result.success:
            if attempt > 0:
                logger.info(f"重试 #{attempt} 成功!")
            return result

        last_result = result

        # 检查是否可重试
        if attempt < max_retries and is_retryable_error(result):
            logger.warning(f"可重试错误，准备第 {attempt + 1} 次重试")
            continue
        else:
            if attempt < max_retries:
                logger.error(f"不可重试的错误: {result.error[:200]}")
            break

    # 所有重试耗尽
    if last_result:
        last_result.retry_attempt = max_retries
        return last_result

    return WorkerResult(
        task_id=task.id,
        success=False,
        error="所有重试耗尽",
    )
