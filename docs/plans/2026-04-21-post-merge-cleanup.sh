#!/bin/bash
# PR merged 后清理 worktree + 本地分支
#
# 使用:
#   bash docs/plans/2026-04-21-post-merge-cleanup.sh
#
# 前置: 确保 feature/arch-layering 已 merged 到 main

set -euo pipefail

cd ~/claude-parallel

# 验证主分支已有合流 commit
git fetch origin
if ! git log origin/main --oneline | head -20 | grep -q "架构分层整理 + perf 实时方法定位"; then
  echo "❌ origin/main 没看到 PR 合流 commit, 退出"
  exit 1
fi

echo "✓ 检测到 PR 已合流到 main"

# 1. 清理 worktree (先切回主仓 main)
git checkout main
git pull origin main

echo "--- 删 worktree ---"
git worktree remove .worktrees/arch-layering
git worktree remove .worktrees/perf-locate
git worktree prune

echo "--- 删本地分支 ---"
git branch -D feature/arch-layering || true
git branch -D feature/perf-locate || true

echo "--- 删远程分支 (可选) ---"
echo "  若远程分支已被 merge 自动删除,下面两条会报 \"remote ref does not exist\", 忽略即可"
git push origin --delete feature/arch-layering 2>/dev/null || echo "  (远程 feature/arch-layering 已删或不存在)"
git push origin --delete feature/perf-locate 2>/dev/null || echo "  (远程 feature/perf-locate 已删或不存在)"

echo ""
echo "✓ 清理完成"
echo "  .worktrees/ 目录:"
ls -la .worktrees/ 2>/dev/null || echo "    (已空)"
echo "  本地分支:"
git branch
