"""
自动 Code Review 模块 — 在所有任务完成后，启动 Claude Code 实例 review 变更。

功能:
- 收集每个成功 worktree 的 git diff
- 构造 review prompt 调用 Claude 进行代码审查
- 解析审查结果，统计问题数量
- 格式化输出审查报告
"""

import asyncio
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .domain.tasks import ProjectConfig, Task
from .worker import WorkerResult


@dataclass
class ReviewResult:
    """单个任务的 Review 结果"""
    task_id: str
    success: bool
    review_text: str = ""
    issues_found: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0


# 用于统计 issues 的关键词
ISSUE_KEYWORDS = ["BUG", "FIXME", "ISSUE", "问题", "缺陷"]

# 用于单词边界匹配的正则，避免 "BUG" 匹配 "DEBUG" 等
import re as _re
_ISSUE_KW_PATTERNS = [
    (_re.compile(r'\b' + _re.escape(kw) + r'\b'), kw) if kw.isascii() else (None, kw)
    for kw in ISSUE_KEYWORDS
]


class CodeReviewer:
    """自动代码审查器 — 使用 Claude Code 对任务变更进行 review"""

    def __init__(
        self,
        config: ProjectConfig,
        coord_dir: Path,
        tasks: List[Task],
        results: Dict[str, WorkerResult],
    ):
        self.config = config
        self.coord_dir = coord_dir
        self.tasks = tasks
        self.results = results
        self._task_map: Dict[str, Task] = {t.id: t for t in tasks}

        # review 结果保存目录
        self.reviews_dir = coord_dir / "reviews"
        self.reviews_dir.mkdir(parents=True, exist_ok=True)

    # ── 公开接口 ──────────────────────────────────────────

    async def review_all(self, max_budget: float = 1.0) -> List[ReviewResult]:
        """
        遍历所有成功的 worktree，对每个进行 code review。

        Args:
            max_budget: 总预算上限 (USD)，按任务均分

        Returns:
            所有 review 结果列表
        """
        # 筛选成功的任务
        successful_tasks = [
            t for t in self.tasks
            if t.id in self.results and self.results[t.id].success
        ]

        if not successful_tasks:
            print("[reviewer] 没有成功的任务可供 review")
            return []

        num_tasks = len(successful_tasks)
        per_task_budget = max_budget / num_tasks if num_tasks > 0 else 0.0
        print(f"[reviewer] 共 {num_tasks} 个任务需要 review，"
              f"每任务预算 ${per_task_budget:.3f}")

        reviews: List[ReviewResult] = []

        for task in successful_tasks:
            result = self.results[task.id]

            # 收集 diff
            diff_text = await self._collect_diff(result.worktree_path, task.id)
            if not diff_text:
                print(f"[reviewer] {task.id}: 无法获取 diff，跳过")
                reviews.append(ReviewResult(
                    task_id=task.id,
                    success=False,
                    review_text="(无法获取 diff)",
                ))
                continue

            print(f"[reviewer] {task.id}: 开始 review (diff {len(diff_text)} chars)")

            # 执行 review
            review = await self.review_task(
                task=task,
                diff_text=diff_text,
                max_budget=per_task_budget,
            )
            reviews.append(review)

            # 保存单个 review 结果
            self._save_review(review)

            status = "OK" if review.success else "FAILED"
            print(f"[reviewer] {task.id}: review {status}, "
                  f"issues={review.issues_found}, "
                  f"cost=${review.cost_usd:.3f}, "
                  f"time={review.duration_s:.1f}s")

        return reviews

    async def review_task(
        self,
        task: Task,
        diff_text: str,
        max_budget: float = 0.3,
    ) -> ReviewResult:
        """
        对单个任务进行 code review。

        Args:
            task: 要审查的任务
            diff_text: git diff 输出
            max_budget: 单任务 review 预算上限

        Returns:
            ReviewResult 审查结果
        """
        start_time = time.time()

        # 截断过长的 diff，保留前 8000 字符
        if len(diff_text) > 8000:
            truncated = diff_text[:8000]
            truncated += f"\n\n... (diff 已截断，原始长度 {len(diff_text)} 字符)"
            diff_text = truncated

        # 构造 review prompt
        prompt = self._build_review_prompt(task, diff_text)

        # 调用 Claude
        success, output_text, cost = await self._run_claude_review(
            prompt, max_budget
        )

        duration = time.time() - start_time

        if not success:
            return ReviewResult(
                task_id=task.id,
                success=False,
                review_text=output_text or "(review 调用失败)",
                cost_usd=cost,
                duration_s=duration,
            )

        # 统计 issues
        issues_count = self._count_issues(output_text)

        review = ReviewResult(
            task_id=task.id,
            success=True,
            review_text=output_text,
            issues_found=issues_count,
            cost_usd=cost,
            duration_s=duration,
        )

        return review

    def format_reviews(self, reviews: List[ReviewResult]) -> str:
        """
        格式化所有 review 结果为可读文本。

        包含汇总统计和每个任务的详细结果。
        """
        if not reviews:
            return "（无 review 结果）"

        lines: List[str] = []
        lines.append("=" * 60)
        lines.append("  Code Review 报告")
        lines.append("=" * 60)
        lines.append("")

        # 统计
        total_issues = sum(r.issues_found for r in reviews)
        successful = [r for r in reviews if r.success]
        failed = [r for r in reviews if not r.success]
        total_cost = sum(r.cost_usd for r in reviews)
        total_duration = sum(r.duration_s for r in reviews)

        lines.append(f"  总任务数: {len(reviews)}")
        lines.append(f"  成功: {len(successful)}  失败: {len(failed)}")
        lines.append(f"  总问题数: {total_issues}")
        lines.append(f"  总费用: ${total_cost:.3f}")
        lines.append(f"  总耗时: {total_duration:.1f}s")
        lines.append("")

        # 提取评分 (从 review_text 中搜索)
        task_scores: Dict[str, Optional[int]] = {}
        for r in successful:
            score = self._extract_score(r.review_text)
            task_scores[r.task_id] = score

        scored_tasks = {
            tid: s for tid, s in task_scores.items() if s is not None
        }
        if scored_tasks:
            best_task = max(scored_tasks, key=scored_tasks.get)  # type: ignore[arg-type]
            worst_task = min(scored_tasks, key=scored_tasks.get)  # type: ignore[arg-type]
            lines.append(f"  最高评分: {best_task} ({scored_tasks[best_task]}/10)")
            lines.append(f"  最低评分: {worst_task} ({scored_tasks[worst_task]}/10)")
            lines.append("")

        # 每个任务的详情
        lines.append("-" * 60)
        for r in reviews:
            status_tag = "OK" if r.success else "FAIL"
            score_str = ""
            if r.task_id in task_scores and task_scores[r.task_id] is not None:
                score_str = f" 评分={task_scores[r.task_id]}/10"

            lines.append(f"  [{status_tag}] {r.task_id}{score_str}")
            lines.append(f"       issues={r.issues_found}  "
                         f"cost=${r.cost_usd:.3f}  "
                         f"time={r.duration_s:.1f}s")

            # 显示 review 摘要 (前 500 字符)
            if r.review_text:
                summary = r.review_text[:500]
                if len(r.review_text) > 500:
                    summary += "..."
                # 缩进
                for text_line in summary.split("\n"):
                    lines.append(f"       {text_line}")
            lines.append("")

        lines.append("=" * 60)

        return "\n".join(lines)

    # ── 内部方法 ──────────────────────────────────────────

    def _build_review_prompt(self, task: Task, diff_text: str) -> str:
        """构造 review prompt"""
        prompt_parts = [
            "## 代码审查任务",
            "",
            f"### 任务描述",
            task.description,
            "",
            "### 变更内容 (git diff)",
            "```diff",
            diff_text,
            "```",
            "",
            "### 审查要求",
            "请对以上代码变更进行全面审查，输出以下内容:",
            "",
            "1. **代码质量评分 (1-10)**: 整体代码质量评分",
            "2. **发现的问题列表**: 列出所有 bug、逻辑错误、潜在问题",
            "3. **改进建议**: 如何改进代码质量、可读性、性能",
            "4. **安全风险**: 如有安全漏洞或不安全做法请指出",
            "",
            "格式要求:",
            "- 用 SCORE: X/10 标注评分",
            "- 用 BUG/FIXME/ISSUE 标记每个具体问题",
            "- 改进建议单独列出",
            "- 安全风险单独列出 (如无风险请说明)",
        ]

        if task.files:
            prompt_parts.extend([
                "",
                "### 涉及文件",
                ", ".join(task.files),
            ])

        return "\n".join(prompt_parts)

    async def _collect_diff(
        self, worktree_path: str, task_id: str
    ) -> str:
        """
        收集 worktree 的 git diff。

        优先在 worktree 中执行 git diff HEAD~1，
        如果失败则尝试在主仓库中获取分支差异。
        """
        # 尝试在 worktree 中获取 diff
        if worktree_path:
            diff = await self._git_diff_in_dir(worktree_path, "HEAD~1", "HEAD")
            if diff:
                return diff

        # 备选: 在主仓库中获取 worktree 分支的 diff
        branch_name = f"cp-{task_id}"
        diff = await self._git_diff_in_dir(
            self.config.repo, f"main..{branch_name}"
        )
        if diff:
            return diff

        # 再尝试 HEAD~1
        diff = await self._git_diff_in_dir(self.config.repo, "HEAD~1", "HEAD")
        return diff or ""

    async def _git_diff_in_dir(
        self, directory: str, *args: str
    ) -> Optional[str]:
        """在指定目录执行 git diff 命令"""
        cmd = ["git", "diff"] + list(args)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=directory,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30
            )
            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="replace").strip()
                return output if output else None
        except (asyncio.TimeoutError, Exception) as e:
            print(f"[reviewer] git diff 失败 ({directory}): {e}")
        return None

    async def _run_claude_review(
        self, prompt: str, max_budget: float
    ) -> tuple:
        """
        调用 claude CLI 执行 review。

        Returns:
            (success, output_text, cost_usd)
        """
        cmd = [
            "claude",
            "-p", prompt,
            "--max-budget-usd", str(max_budget),
            "--output-format", "json",
            "--dangerously-skip-permissions",
        ]

        env = os.environ.copy()
        env["LANG"] = "en_US.UTF-8"
        env["LC_ALL"] = "en_US.UTF-8"

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.config.repo,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=300
            )

            stdout_text = stdout.decode("utf-8", errors="replace").strip()

            if not stdout_text:
                err = stderr.decode("utf-8", errors="replace").strip()
                return (False, f"(无输出, stderr: {err[:500]})", 0.0)

            # 解析 JSON 输出
            try:
                data = json.loads(stdout_text)
                output = data.get("result", "")
                cost = data.get("total_cost_usd", 0.0)
                success = data.get("subtype") == "success"
                return (success, output, cost)
            except json.JSONDecodeError:
                # 非 JSON 也可能是有用的文本
                success = proc.returncode == 0
                return (success, stdout_text, 0.0)

        except asyncio.TimeoutError:
            return (False, "(review 超时)", 0.0)
        except Exception as e:
            return (False, f"(review 异常: {e})", 0.0)

    def _count_issues(self, text: str) -> int:
        """统计 review 文本中的 issue 关键词出现次数（使用单词边界避免误匹配）"""
        count = 0
        for pattern, kw in _ISSUE_KW_PATTERNS:
            if pattern is not None:
                count += len(pattern.findall(text))
            else:
                count += text.count(kw)
        return count

    def _extract_score(self, text: str) -> Optional[int]:
        """从 review 文本中提取评分 (1-10)"""
        # 匹配 SCORE: X/10 或 评分: X/10 等模式
        patterns = [
            r"SCORE\s*[:：]\s*(\d+)\s*/\s*10",
            r"评分\s*[:：]\s*(\d+)\s*/\s*10",
            r"(\d+)\s*/\s*10",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                score = int(match.group(1))
                if 1 <= score <= 10:
                    return score
        return None

    def _save_review(self, review: ReviewResult) -> None:
        """保存单个 review 结果到 JSON 文件"""
        filepath = self.reviews_dir / f"{review.task_id}.json"
        try:
            data = asdict(review)
            filepath.write_text(
                json.dumps(data, ensure_ascii=False, indent=2)
            )
        except Exception as e:
            print(f"[reviewer] 保存 review 失败 ({review.task_id}): {e}")
