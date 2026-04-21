"""
Enhanced Worktree Merger with automatic conflict resolution via Claude.

Merges changes from multiple Claude Code worktrees back into the main branch,
resolving conflicts automatically when possible by invoking Claude.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.infrastructure.claude.client import call_claude_async, strip_code_fences
from src.domain.tasks import Task, ProjectConfig
from src.application.worker import WorkerResult

logger = logging.getLogger(__name__)


@dataclass
class MergeReport:
    """Summary of the merge operation across all worktrees."""

    merged: List[str] = field(default_factory=list)
    conflicts_resolved: List[str] = field(default_factory=list)
    conflicts_unresolved: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            "=== Merge Report ===",
            f"  Merged (clean):       {len(self.merged)}",
            f"  Conflicts resolved:   {len(self.conflicts_resolved)}",
            f"  Conflicts unresolved: {len(self.conflicts_unresolved)}",
            f"  Skipped (no changes): {len(self.skipped)}",
            f"  Errors:               {len(self.errors)}",
        ]
        if self.merged:
            lines.append(f"  Merged tasks: {', '.join(self.merged)}")
        if self.conflicts_resolved:
            lines.append(f"  Auto-resolved: {', '.join(self.conflicts_resolved)}")
        if self.conflicts_unresolved:
            lines.append(f"  Unresolved: {', '.join(self.conflicts_unresolved)}")
        if self.skipped:
            lines.append(f"  Skipped: {', '.join(self.skipped)}")
        if self.errors:
            for tid, err in self.errors.items():
                lines.append(f"  Error [{tid}]: {err}")
        return "\n".join(lines)


class WorktreeMerger:
    """Merges worktree changes back to the main branch with conflict resolution."""

    def __init__(
        self,
        config: ProjectConfig,
        coord_dir: Path,
        tasks: List[Task],
        results: Dict[str, WorkerResult],
    ) -> None:
        self.config = config
        self.coord_dir = coord_dir
        self.tasks = tasks
        self.results = results

        # Build lookup tables
        self._task_by_id: Dict[str, Task] = {t.id: t for t in tasks}

        # Dependency-level buckets for ordered merging
        self._levels: List[List[str]] = self._compute_dependency_levels()

        # Main repo path
        self._main_repo = Path(config.repo).expanduser().resolve()

    # ------------------------------------------------------------------
    # Dependency level computation
    # ------------------------------------------------------------------

    def _compute_dependency_levels(self) -> List[List[str]]:
        """Group task IDs into dependency levels (topological order).

        Level 0 has no dependencies. Level N depends only on tasks in
        levels < N.  Tasks with circular or unknown deps go last.
        """
        resolved: Dict[str, int] = {}
        remaining = list(self._task_by_id.keys())
        levels: List[List[str]] = []

        # Only include tasks that have successful results
        remaining = [t for t in remaining if t in self.results and self.results[t].success]

        max_iterations = len(remaining) + 1
        iteration = 0

        while remaining and iteration < max_iterations:
            iteration += 1
            current_level: List[str] = []
            for tid in list(remaining):
                task = self._task_by_id[tid]
                deps = [d for d in task.depends_on if d in self._task_by_id]
                # Check if all deps are already resolved
                if all(d in resolved for d in deps):
                    level = 0
                    if deps:
                        level = max(resolved[d] for d in deps) + 1
                    current_level.append(tid)
                    resolved[tid] = level

            if not current_level:
                # Break circular / unresolved deps — just append remaining
                for tid in remaining:
                    levels.append([tid])
                break

            remaining = [t for t in remaining if t not in current_level]
            # Group by level number
            level_groups: Dict[int, List[str]] = {}
            for tid in current_level:
                lv = resolved[tid]
                level_groups.setdefault(lv, []).append(tid)
            for lv in sorted(level_groups):
                levels.append(level_groups[lv])

        return levels

    # ------------------------------------------------------------------
    # Git helpers
    # ------------------------------------------------------------------

    async def _run_git(
        self,
        args: List[str],
        cwd: Optional[Path] = None,
        check: bool = True,
    ) -> asyncio.subprocess.Process:
        """Run a git command and return the process (awaited)."""
        cwd = cwd or self._main_repo
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return proc

    async def _git(
        self,
        args: List[str],
        cwd: Optional[Path] = None,
        check: bool = True,
    ) -> tuple:
        """Run git, return (stdout, stderr, returncode)."""
        proc = await self._run_git(args, cwd=cwd, check=check)
        stdout, stderr = await proc.communicate()
        return stdout.decode().strip(), stderr.decode().strip(), proc.returncode

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def auto_commit_worktrees(self) -> None:
        """Commit any uncommitted changes in all worktrees.

        This prevents data loss when Claude finishes work but hasn't
        committed its changes.
        """
        for task_id, result in self.results.items():
            if not result.success:
                continue
            wt_path = self._worktree_path(task_id)
            if not wt_path or not wt_path.exists():
                continue
            await self._auto_commit_one(wt_path, task_id)

    async def _auto_commit_one(self, wt_path: Path, task_id: str) -> None:
        """Commit uncommitted changes in a single worktree."""
        # Check for uncommitted changes
        stdout, _, rc = await self._git(
            ["status", "--porcelain"], cwd=wt_path, check=False
        )
        if rc != 0 or not stdout.strip():
            return

        # Stage everything
        await self._git(["add", "-A"], cwd=wt_path)
        # Commit
        stdout, stderr, rc = await self._git(
            ["commit", "-m", f"[claude-parallel] auto-commit for task {task_id}"],
            cwd=wt_path,
            check=False,
        )
        if rc == 0:
            print(f"  [merger] Auto-committed pending changes in worktree for {task_id}")
        else:
            print(f"  [merger] Warning: could not auto-commit {task_id}: {stderr}")

    async def merge_all(self) -> MergeReport:
        """Merge all successful worktrees back to main in dependency order.

        Returns a MergeReport summarising the outcome.

        Safety guarantees:
        - 预飞行检查: 主分支必须干净，否则拒绝合并避免污染
        - 保存 rollback 起点 (pre-merge HEAD) 到 coord_dir/merge-rollback.txt
        - 任一任务异常后强制 cherry-pick --abort 并校验主仓库干净，否则中止后续合并
        """
        report = MergeReport()

        print("[merger] Starting merge of worktrees into main branch...")

        # 预飞行: 校验主仓库工作区干净
        dirty, dirty_msg = await self._check_main_clean()
        if dirty:
            report.errors["__preflight__"] = dirty_msg
            print(f"  [merger] ABORT: 主仓库工作区不干净，拒绝开始合并以避免污染")
            print(f"  [merger]   {dirty_msg}")
            print(f"  [merger]   请先 commit/stash 后重试")
            return report

        # 记录 pre-merge HEAD 作为 rollback 起点
        pre_head, _, _ = await self._git(
            ["rev-parse", "HEAD"], cwd=self._main_repo, check=False
        )
        if pre_head:
            await self._save_rollback_point(pre_head)
            print(f"  [merger] 已记录 rollback 起点: {pre_head[:12]} → {self.coord_dir}/merge-rollback.txt")

        # Step 1: auto-commit any uncommitted worktree changes
        await self.auto_commit_worktrees()

        # Step 2: iterate through dependency levels
        aborted = False
        for level_idx, level_tasks in enumerate(self._levels):
            if aborted:
                break
            print(f"\n[merger] --- Dependency level {level_idx} ({len(level_tasks)} tasks) ---")

            for task_id in level_tasks:
                try:
                    await self._merge_one(task_id, report)
                except Exception as exc:
                    report.errors[task_id] = str(exc)
                    print(f"  [merger] ERROR merging {task_id}: {exc}")
                    # 异常后强制清理 cherry-pick 中间态
                    await self._git(
                        ["cherry-pick", "--abort"], cwd=self._main_repo, check=False
                    )

                # 每个任务后都验证主仓库依然干净；如不干净说明前次操作残留
                dirty, dirty_msg = await self._check_main_clean()
                if dirty:
                    report.errors.setdefault(task_id, "")
                    report.errors[task_id] += f" | 主仓库残留未清理: {dirty_msg}"
                    print(f"  [merger] ABORT: {task_id} 后主仓库未恢复干净状态，停止后续合并")
                    print(f"  [merger]   如需回滚，执行: git -C {self._main_repo} reset --hard {pre_head[:12] if pre_head else 'HEAD@{1}'}")
                    aborted = True
                    break

        print(f"\n{report.summary()}")
        if pre_head and (report.errors or report.conflicts_unresolved):
            print(f"\n  Rollback 命令: git -C {self._main_repo} reset --hard {pre_head[:12]}")
        return report

    async def _check_main_clean(self) -> Tuple[bool, str]:
        """检查主仓库工作区是否干净。返回 (dirty?, 描述)"""
        stdout, _, rc = await self._git(
            ["status", "--porcelain"], cwd=self._main_repo, check=False
        )
        if rc != 0:
            return True, "git status 失败"
        if stdout.strip():
            preview = stdout.strip().splitlines()[:3]
            return True, "未提交变更: " + " | ".join(preview)
        # 检查 cherry-pick / merge / rebase 中间态
        git_dir = self._main_repo / ".git"
        for marker in ("CHERRY_PICK_HEAD", "MERGE_HEAD", "REBASE_HEAD"):
            if (git_dir / marker).exists():
                return True, f"git 中间态文件存在: {marker}"
        return False, ""

    async def _save_rollback_point(self, commit_hash: str) -> None:
        """保存 pre-merge HEAD 到协调目录，便于事故回滚"""
        try:
            import time as _time
            from .infrastructure.storage.atomic import atomic_write_text
            rb_file = self.coord_dir / "merge-rollback.txt"
            text = (
                f"# Pre-merge HEAD (saved by WorktreeMerger)\n"
                f"# 回滚命令: git -C {self._main_repo} reset --hard {commit_hash}\n"
                f"timestamp={int(_time.time())}\n"
                f"commit={commit_hash}\n"
            )
            atomic_write_text(rb_file, text)
        except Exception as exc:
            print(f"  [merger] 警告: 保存 rollback 点失败: {exc}")

    async def _merge_one(self, task_id: str, report: MergeReport) -> bool:
        """Merge a single worktree's HEAD commit into the main branch.

        Returns True if merged (cleanly or after conflict resolution).
        """
        task = self._task_by_id[task_id]
        wt_path = self._worktree_path(task_id)
        if not wt_path or not wt_path.exists():
            report.errors[task_id] = "Worktree path not found"
            print(f"  [merger] SKIP {task_id}: worktree path missing")
            return False

        # Get the HEAD commit hash of the worktree
        commit_hash, stderr, rc = await self._git(
            ["rev-parse", "HEAD"], cwd=wt_path, check=False
        )
        if rc != 0 or not commit_hash:
            report.errors[task_id] = f"Cannot get HEAD: {stderr}"
            return False

        # Check if this commit has actual changes vs its parent
        diff_stat, _, _ = await self._git(
            ["diff", "--stat", "HEAD~1", "HEAD"], cwd=wt_path, check=False
        )
        if not diff_stat.strip():
            report.skipped.append(task_id)
            print(f"  [merger] SKIP {task_id}: no changes")
            return False

        print(f"  [merger] Cherry-picking {task_id} ({commit_hash[:8]}) ...")

        # Cherry-pick --no-commit into main repo
        _, stderr, rc = await self._git(
            ["cherry-pick", "--no-commit", commit_hash],
            cwd=self._main_repo,
            check=False,
        )

        if rc == 0:
            # Clean merge — commit it
            message = self._merge_commit_message(task, commit_hash)
            await self._git(["add", "-A"], cwd=self._main_repo)
            _, cerr, crc = await self._git(
                ["commit", "-m", message],
                cwd=self._main_repo,
                check=False,
            )
            if crc == 0:
                report.merged.append(task_id)
                print(f"  [merger] OK   {task_id}: merged cleanly")
                return True
            else:
                # Nothing to commit (maybe already identical)
                report.skipped.append(task_id)
                print(f"  [merger] SKIP {task_id}: nothing to commit")
                return False

        # Conflict detected — attempt auto-resolution
        print(f"  [merger] CONFLICT in {task_id}, attempting auto-resolution...")

        # Check if there are unmerged paths
        unmerged, _, _ = await self._git(
            ["diff", "--name-only", "--diff-filter=U"],
            cwd=self._main_repo,
            check=False,
        )

        if not unmerged.strip():
            # No actual unmerged files — maybe the conflict is different
            # Try to just commit what we have
            await self._git(["add", "-A"], cwd=self._main_repo)
            message = self._merge_commit_message(task, commit_hash)
            _, cerr, crc = await self._git(
                ["commit", "-m", message],
                cwd=self._main_repo,
                check=False,
            )
            if crc == 0:
                report.conflicts_resolved.append(task_id)
                print(f"  [merger] OK   {task_id}: resolved without Claude")
                return True
            else:
                await self._git(
                    ["cherry-pick", "--abort"], cwd=self._main_repo, check=False
                )
                report.conflicts_unresolved.append(task_id)
                print(f"  [merger] FAIL {task_id}: could not resolve conflict")
                return False

        resolved = await self._resolve_conflict(task, str(wt_path), commit_hash)
        if resolved:
            report.conflicts_resolved.append(task_id)
            print(f"  [merger] OK   {task_id}: conflicts auto-resolved via Claude")
            return True
        else:
            report.conflicts_unresolved.append(task_id)
            print(f"  [merger] FAIL {task_id}: could not auto-resolve conflicts")
            return False

    # ------------------------------------------------------------------
    # Conflict resolution via Claude
    # ------------------------------------------------------------------

    async def _resolve_conflict(
        self, task: Task, wt_path: str, commit_hash: str
    ) -> bool:
        """Attempt to resolve merge conflicts using Claude.

        Returns True if all conflicts were resolved and committed.
        """
        # Get list of conflicted files
        unmerged_out, _, rc = await self._git(
            ["diff", "--name-only", "--diff-filter=U"],
            cwd=self._main_repo,
            check=False,
        )
        if rc != 0 or not unmerged_out.strip():
            return False

        conflict_files = [f.strip() for f in unmerged_out.strip().splitlines() if f.strip()]
        if not conflict_files:
            return False

        print(f"  [merger]   Conflicted files: {', '.join(conflict_files)}")

        # Resolve each file
        for filepath in conflict_files:
            resolved_content = await self._resolve_file_conflict(filepath)
            if resolved_content is None:
                # Claude could not resolve this file
                print(f"  [merger]   Failed to resolve: {filepath}")
                await self._git(
                    ["cherry-pick", "--abort"], cwd=self._main_repo, check=False
                )
                return False

            # Write resolved content back
            full_path = self._main_repo / filepath
            try:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(resolved_content, encoding="utf-8")
            except OSError as exc:
                print(f"  [merger]   Error writing {filepath}: {exc}")
                await self._git(
                    ["cherry-pick", "--abort"], cwd=self._main_repo, check=False
                )
                return False

            # Stage the resolved file
            await self._git(["add", filepath], cwd=self._main_repo)
            print(f"  [merger]   Resolved: {filepath}")

        # All conflicts resolved — commit
        await self._git(["add", "-A"], cwd=self._main_repo)

        message = self._merge_commit_message(
            task, commit_hash, suffix="(conflicts auto-resolved)"
        )
        _, stderr, rc = await self._git(
            ["commit", "-m", message],
            cwd=self._main_repo,
            check=False,
        )
        if rc != 0:
            # Try cherry-pick --continue as fallback
            env_patch = "true"  # no editor needed
            proc = await asyncio.create_subprocess_exec(
                "git",
                "cherry-pick",
                "--continue",
                "--no-edit",
                cwd=str(self._main_repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"GIT_EDITOR": "true"},
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                print(f"  [merger]   Could not commit resolution: {stderr.decode().strip()}")
                await self._git(
                    ["cherry-pick", "--abort"], cwd=self._main_repo, check=False
                )
                return False

        return True

    async def _resolve_file_conflict(self, filepath: str) -> Optional[str]:
        """Use Claude to resolve conflicts in a single file.

        Returns the resolved file content, or None on failure.
        """
        full_path = self._main_repo / filepath
        try:
            conflicted_content = full_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("Cannot read conflicted file %s: %s", filepath, exc)
            return None

        # Skip binary files (crude heuristic)
        if "\x00" in conflicted_content:
            logger.debug("Skipping binary file: %s", filepath)
            return None

        prompt = (
            "You are an expert at resolving git merge conflicts.\n"
            "The following file has git merge conflict markers (<<<<<<<, =======, >>>>>>>).\n"
            "Resolve ALL conflicts by keeping the useful changes from BOTH sides.\n"
            "Output ONLY the complete resolved file content — no explanation, no markdown fences.\n"
            "If one side clearly should dominate, use that, but try to preserve both sets of changes.\n\n"
            f"File: {filepath}\n\n"
            "--- BEGIN CONFLICTED FILE ---\n"
            f"{conflicted_content}\n"
            "--- END CONFLICTED FILE ---\n"
        )

        # Call Claude CLI via shared async client
        try:
            success, output, err = await call_claude_async(
                prompt,
                timeout=300,
                cwd=str(self._main_repo),
                output_format="text",
            )
        except Exception as exc:
            logger.debug("Claude async call failed for %s: %s", filepath, exc)
            return None

        if not success:
            logger.debug("Claude error for %s: %s", filepath, err)
            return None

        # Strip potential markdown code fences that Claude might add
        resolved = strip_code_fences(output)

        # Verify no conflict markers remain
        if "<<<<<<" in resolved or ">>>>>>" in resolved:
            logger.debug("Claude output still contains conflict markers in %s", filepath)
            return None

        return resolved

    # ------------------------------------------------------------------
    # Diff preview
    # ------------------------------------------------------------------

    async def preview_diff(self) -> str:
        """Collect diff previews from all successful worktrees.

        Returns a formatted string with per-worktree diff stats and
        truncated full diffs.
        """
        sections: List[str] = []
        sections.append("=" * 60)
        sections.append("  DIFF PREVIEW — Worktree Changes")
        sections.append("=" * 60)

        for task_id, result in self.results.items():
            if not result.success:
                continue

            wt_path = self._worktree_path(task_id)
            if not wt_path or not wt_path.exists():
                continue

            task = self._task_by_id.get(task_id)

            sections.append("")
            sections.append(f"--- Task: {task_id} ---")
            if task:
                sections.append(f"    Description: {task.description[:120]}")
            sections.append(f"    Worktree: {wt_path}")

            # Diff stat
            stat_out, _, stat_rc = await self._git(
                ["diff", "HEAD~1", "--stat"],
                cwd=wt_path,
                check=False,
            )
            if stat_rc == 0 and stat_out.strip():
                sections.append("    [stat]")
                for line in stat_out.splitlines():
                    sections.append(f"      {line}")
            else:
                sections.append("    (no diff stat available)")

            # Full diff (truncated)
            diff_out, _, diff_rc = await self._git(
                ["diff", "HEAD~1"],
                cwd=wt_path,
                check=False,
            )
            if diff_rc == 0 and diff_out.strip():
                sections.append("    [diff summary]")
                diff_lines = diff_out.splitlines()
                max_diff_lines = 80
                for line in diff_lines[:max_diff_lines]:
                    sections.append(f"      {line}")
                if len(diff_lines) > max_diff_lines:
                    sections.append(
                        f"      ... ({len(diff_lines) - max_diff_lines} more lines)"
                    )
            else:
                sections.append("    (no diff available)")

        sections.append("")
        sections.append("=" * 60)
        return "\n".join(sections)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _worktree_path(self, task_id: str) -> Optional[Path]:
        """Resolve the filesystem path for a task's worktree."""
        result = self.results.get(task_id)
        if result and result.worktree_path:
            p = Path(result.worktree_path)
            if p.exists():
                return p

        # Fallback: conventional naming under coord_dir
        if self.coord_dir:
            fallback = self.coord_dir / "worktrees" / task_id
            if fallback.exists():
                return fallback

        return None

    @staticmethod
    def _merge_commit_message(
        task: Task, commit_hash: str, suffix: str = ""
    ) -> str:
        """Build a merge commit message."""
        parts = [
            f"[claude-parallel] Merge task {task.id}",
            f"  {task.description[:200]}",
        ]
        if suffix:
            parts.append(f"  {suffix}")
        return "\n".join(parts)
