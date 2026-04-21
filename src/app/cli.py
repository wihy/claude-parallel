#!/usr/bin/env python3
"""
claude-parallel — 多 Claude Code 并行协同执行框架 CLI

Phase 3 增强功能:
- diff 预览命令
- review 命令 (自动 Code Review)
- validate 命令 (YAML 校验)
- 集成 WorktreeMerger (冲突自动解决)
"""

import argparse
import asyncio
import json
import sys
import os
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.orchestrator import Orchestrator
from src.validator import TaskValidator
from src.perf import PerfConfig, PerfSessionManager
from src.perf.perf_defaults import PerfDefaults
from src.infrastructure.storage.atomic import safe_read_json
from .perf_cli import register_perf_subcommands, dispatch_perf_command
from .execution_cli import register_execution_subcommands, dispatch_execution_command
from .ops_cli import (
    register_ops_subcommands,
    dispatch_ops_command,
    cmd_dashboard,
    cmd_clean,
    cmd_logs,
)

VERSION = "0.3.0"


def print_banner():
    print(f"""
  ╔═══════════════════════════════════════════╗
  ║   Claude Parallel v{VERSION}                 ║
  ║   Multi-Claude Code Parallel Executor     ║
  ╚═══════════════════════════════════════════╝
  """)


def print_report(report: dict):
    """打印执行结果报告"""
    summary = report.get("summary", {})

    print(f"\n{'='*60}")
    print(f"  执行结果汇总")
    print(f"{'='*60}")

    print(f"  完成率:   {summary.get('success_rate', 'N/A')}")
    print(f"  成功/失败: {summary.get('completed', 0)} / {summary.get('failed', 0)}")
    if summary.get('skipped', 0) > 0:
        print(f"  跳过:     {summary['skipped']}")
    if summary.get('retried', 0) > 0:
        print(f"  重试:     {summary['retried']}")
    print(f"  总耗时:   {summary.get('total_duration_s', 0):.1f}s")
    print(f"  总成本:   ${summary.get('total_cost_usd', 0):.3f}")

    tasks = report.get("tasks", [])
    if tasks:
        print(f"\n  {'─'*68}")
        print(f"  {'任务':<20} {'状态':<6} {'耗时':>8} {'成本':>8} {'Turns':>6} {'重试':>4}")
        print(f"  {'─'*68}")
        for t in tasks:
            status_raw = t.get("status", "")
            if status_raw in ("cancelled", "skipped"):
                status = "⊝"
            elif t["success"]:
                status = "✓"
            else:
                status = "✗"
            dur = f"{t.get('duration_s', 0):.0f}s"
            cost = f"${t.get('cost_usd', 0):.3f}"
            turns = str(t.get('num_turns', '-'))
            retries = str(t.get('retries', 0))
            print(f"  {t['id']:<20} {status:<6} {dur:>8} {cost:>8} {turns:>6} {retries:>4}")
        print(f"  {'─'*68}")

    failures = [
        t for t in tasks
        if (not t["success"]) and t.get("status", "") not in ("cancelled", "skipped")
    ]
    if failures:
        print(f"\n  失败任务详情:")
        for t in failures:
            err = t.get('error', '') or '未知错误'
            print(f"    {t['id']}: {err[:100]}")

    perf = report.get("perf")
    if perf:
        metrics = perf.get("metrics", {})
        print(f"\n  Perf 摘要:")
        print(f"    display_avg={metrics.get('display_avg')} cpu_avg={metrics.get('cpu_avg')} net_avg={metrics.get('networking_avg')}")
        gate = perf.get("gate", {})
        if gate.get("checked"):
            status = "PASS" if gate.get("passed") else "FAIL"
            print(f"    gate={status} reason={gate.get('reason', '')}")

    print()


def build_perf_config_from_args(args) -> PerfConfig:
    return PerfConfig(
        enabled=bool(getattr(args, "with_perf", False)),
        tag=getattr(args, "perf_tag", "perf") or "perf",
        device=getattr(args, "perf_device", "") or "",
        attach=getattr(args, "perf_attach", "") or "",
        duration_sec=int(getattr(args, "perf_duration", 1800) or 1800),
        templates=getattr(args, "perf_templates", "power") or "power",
        baseline_tag=getattr(args, "perf_baseline", "") or "",
        threshold_pct=float(getattr(args, "perf_threshold_pct", 0.0) or 0.0),
        live_rules_file=getattr(args, "perf_live_rules", "") or "",
        live_alert_log=getattr(args, "perf_live_alert_log", "") or "",
        live_buffer_lines=int(getattr(args, "perf_live_buffer", 200) or 200),
        stream_interval=float(getattr(args, "perf_stream_interval", 10.0) or 10.0),
        stream_window=int(getattr(args, "perf_stream_window", 30) or 30),
        stream_jsonl=getattr(args, "perf_stream_jsonl", "") or "",
        sampling_enabled=bool(getattr(args, "perf_sampling", False)),
        sampling_interval_sec=int(getattr(args, "perf_sampling_interval", 10) or 10),
        sampling_top_n=int(getattr(args, "perf_sampling_top", 10) or 10),
        sampling_retention=int(getattr(args, "perf_sampling_retention", 30) or 30),
        metrics_source=getattr(args, "perf_metrics_source", "auto") or "auto",
        metrics_interval_ms=int(getattr(args, "perf_metrics_interval", 1000) or 1000),
        battery_interval_sec=int(getattr(args, "perf_battery_interval", 10) or 10),
        attach_webcontent=bool(getattr(args, "perf_attach_webcontent", False)),
        composite=getattr(args, "perf_composite", "auto") or "auto",
        binary_path=getattr(args, "perf_binary", "") or "",
        linkmap_path=getattr(args, "perf_linkmap", "") or "",
        dsym_paths=list(getattr(args, "perf_dsym", []) or []),
    )


def _maybe_start_web_dashboard(args, orch, perf_cfg):
    """如启用 --web-dashboard，启动后台 HTTP 仪表盘并返回 server 实例 (否则 None)。"""
    if not getattr(args, "web_dashboard", False):
        return None
    try:
        from src.infrastructure.dashboard.server import (
            DashboardServer, collect_orchestrator_state, collect_perf_state,
        )
    except ImportError as e:
        print(f"  [dashboard] 加载失败: {e}")
        return None

    repo_path = Path(orch.config.repo) if orch.config else Path.cwd()
    coord_dir = orch.config.coordination_dir if orch.config else ".claude-parallel"
    perf_tag = perf_cfg.tag if perf_cfg and perf_cfg.enabled else "perf"

    srv = DashboardServer(
        port=int(getattr(args, "web_port", 8765)),
        orch_provider=lambda: collect_orchestrator_state(orch),
        perf_provider=lambda: collect_perf_state(repo_path, coord_dir, perf_tag),
        title=f"cpar Dashboard — {Path(args.task_file).stem if hasattr(args, 'task_file') else 'run'}",
    )
    try:
        url = srv.start()
        print(f"  [dashboard] Web UI 已启动: {url}  (perf tag: {perf_tag})")
        return srv
    except OSError as e:
        print(f"  [dashboard] 端口 {args.web_port} 启动失败: {e}")
        return None


async def cmd_run(args):
    """执行任务"""
    perf_cfg = build_perf_config_from_args(args)

    # 先校验
    if not args.no_validate:
        validator = TaskValidator(
            args.task_file,
            perf_enabled=perf_cfg.enabled,
            perf_device=perf_cfg.device,
            perf_attach=perf_cfg.attach,
        )
        if not validator.validate():
            validator.print_report()
            print(f"\n  ✗ 配置校验失败，请修复后重试 (跳过校验请加 --no-validate)")
            sys.exit(1)
        validator.print_report()

    orch = Orchestrator(
        task_file=args.task_file,
        dry_run=args.dry,
        max_retries=args.retry,
        total_budget=args.total_budget,
        verbose=args.verbose,
        perf_config=perf_cfg,
    )
    orch.load()

    # 可选: 启动 Web Dashboard (与 Rich Live 共存; orch 数据通过 provider 暴露)
    dash_srv = _maybe_start_web_dashboard(args, orch, perf_cfg)
    try:
        report = await orch.run()
    finally:
        if dash_srv:
            dash_srv.stop()

    if not args.dry:
        print_report(report)

        # Perf gate: 超阈值时视为失败
        perf_gate = report.get("perf", {}).get("gate", {})
        if perf_gate.get("checked") and not perf_gate.get("passed"):
            print(f"  [perf-gate] FAIL: {perf_gate.get('reason', '')}")
            if args.strict_perf_gate:
                raise SystemExit(2)

        if args.merge:
            summary = report.get("summary", {})
            if summary.get("failed", 0) == 0 and summary.get("skipped", 0) == 0:
                await cmd_merge_impl(args, orch)
            else:
                print("  [merge] 检测到存在失败/跳过任务，已跳过自动合并以避免部分结果误入主分支")
                print("  [merge] 如确认只合并成功任务，请手动执行: cpar merge <task-file>")
        if args.clean:
            await orch.cleanup_worktrees()


async def cmd_resume(args):
    """从中断处恢复执行"""
    perf_cfg = build_perf_config_from_args(args)

    orch = Orchestrator(
        task_file=args.task_file,
        max_retries=args.retry,
        total_budget=args.total_budget,
        verbose=args.verbose,
        perf_config=perf_cfg,
    )
    orch.load()

    dash_srv = _maybe_start_web_dashboard(args, orch, perf_cfg)
    try:
        report = await orch.resume()
    finally:
        if dash_srv:
            dash_srv.stop()
    print_report(report)

    perf_gate = report.get("perf", {}).get("gate", {})
    if perf_gate.get("checked") and not perf_gate.get("passed"):
        print(f"  [perf-gate] FAIL: {perf_gate.get('reason', '')}")
        if args.strict_perf_gate:
            raise SystemExit(2)

    if args.merge:
        summary = report.get("summary", {})
        if summary.get("failed", 0) == 0 and summary.get("skipped", 0) == 0:
            await cmd_merge_impl(args, orch)
        else:
            print("  [merge] 检测到存在失败/跳过任务，已跳过自动合并以避免部分结果误入主分支")
            print("  [merge] 如确认只合并成功任务，请手动执行: cpar merge <task-file>")
    if args.clean:
        await orch.cleanup_worktrees()


async def cmd_plan(args):
    """展示执行计划"""
    validator = TaskValidator(args.task_file)
    if not validator.validate():
        validator.print_report()
        return

    orch = Orchestrator(task_file=args.task_file, dry_run=True)
    orch.load()

    print_banner()
    orch._dry_run()

    print(f"  DAG 层级图:")
    for i, level in enumerate(orch.levels):
        deps = set()
        for t in level:
            deps.update(t.depends_on)
        names = ", ".join(t.id for t in level)
        if deps:
            print(f"    Level {i}: [{names}] <- depends on [{', '.join(deps)}]")
        else:
            print(f"    Level {i}: [{names}] <- (no deps, start immediately)")
    print()


async def cmd_merge_impl(args, orch: Orchestrator):
    """使用 WorktreeMerger 合并"""
    from src.merger import WorktreeMerger

    merger = WorktreeMerger(
        config=orch.config,
        coord_dir=orch.coord_dir,
        tasks=orch.tasks,
        results=orch.results,
    )
    merge_report = await merger.merge_all()
    print(merge_report.summary())


async def cmd_merge(args):
    """合并已有的 worktree 结果"""
    orch = Orchestrator(task_file=args.task_file)
    orch.load()

    # 加载已有结果（safe_read_json 可吃掉并发半写产生的损坏文件）
    from src.worker import WorkerResult
    coord_dir = orch.coord_dir
    for task in orch.tasks:
        result_file = coord_dir / "coord" / f"{task.id}.result"
        data = safe_read_json(result_file, None)
        if not data or "task_id" not in data:
            continue
        try:
            orch.results[task.id] = WorkerResult(
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
            if data["success"]:
                task.status = "done"
            elif data.get("status") == "cancelled":
                task.status = "cancelled"
            else:
                task.status = "failed"
        except KeyError:
            continue

    await cmd_merge_impl(args, orch)


async def cmd_diff(args):
    """预览所有 worktree 的变更"""
    orch = Orchestrator(task_file=args.task_file)
    orch.load()

    # 加载已有结果（safe_read_json 防并发半写）
    from src.worker import WorkerResult
    coord_dir = orch.coord_dir
    for task in orch.tasks:
        result_file = coord_dir / "coord" / f"{task.id}.result"
        data = safe_read_json(result_file, None)
        if not data or "task_id" not in data:
            continue
        try:
            orch.results[task.id] = WorkerResult(
                task_id=data["task_id"],
                success=data["success"],
                worktree_path=data.get("worktree_path", ""),
            )
        except KeyError:
            continue

    from src.merger import WorktreeMerger
    merger = WorktreeMerger(
        config=orch.config,
        coord_dir=orch.coord_dir,
        tasks=orch.tasks,
        results=orch.results,
    )
    diff_text = await merger.preview_diff()
    print(diff_text)


async def cmd_review(args):
    """对所有变更执行 Code Review"""
    orch = Orchestrator(task_file=args.task_file)
    orch.load()

    # 加载已有结果（safe_read_json 防并发半写）
    from src.worker import WorkerResult
    coord_dir = orch.coord_dir
    for task in orch.tasks:
        result_file = coord_dir / "coord" / f"{task.id}.result"
        data = safe_read_json(result_file, None)
        if not data or "task_id" not in data:
            continue
        try:
            orch.results[task.id] = WorkerResult(
                task_id=data["task_id"],
                success=data["success"],
                worktree_path=data.get("worktree_path", ""),
                output=data.get("output_summary", ""),
            )
            if data["success"]:
                task.status = "done"
            elif data.get("status") == "cancelled":
                task.status = "cancelled"
            else:
                task.status = "failed"
        except KeyError:
            continue

    from src.reviewer import CodeReviewer
    reviewer = CodeReviewer(
        config=orch.config,
        coord_dir=orch.coord_dir,
        tasks=orch.tasks,
        results=orch.results,
    )

    max_budget = args.budget or 1.0
    print(f"\n  开始 Code Review (预算上限: ${max_budget})...\n")
    reviews = await reviewer.review_all(max_budget=max_budget)
    print(reviewer.format_reviews(reviews))


async def cmd_validate(args):
    """校验任务文件"""
    print_banner()
    validator = TaskValidator(
        args.task_file,
        perf_enabled=bool(getattr(args, "with_perf", False)),
        perf_device=getattr(args, "perf_device", "") or "",
        perf_attach=getattr(args, "perf_attach", "") or "",
    )
    ok = validator.validate()
    validator.print_report()
    if ok:
        print(f"  ✓ {args.task_file} 校验通过，可以执行")
    else:
        print(f"\n  ✗ 请修复以上错误后重试")
        sys.exit(1)




def main():
    parser = argparse.ArgumentParser(
        prog="claude-parallel",
        description=f"Claude Parallel v{VERSION} — 多 Claude Code 并行协同执行框架",
    )
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    register_execution_subcommands(subparsers)
    register_ops_subcommands(subparsers)
    register_perf_subcommands(subparsers)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command not in ("logs", "validate"):
        print_banner()

    if dispatch_execution_command(args, {
        "run": cmd_run,
        "resume": cmd_resume,
        "plan": cmd_plan,
        "merge": cmd_merge,
        "diff": cmd_diff,
        "review": cmd_review,
        "validate": cmd_validate,
    }):
        return
    if dispatch_ops_command(args, {
        "clean": cmd_clean,
        "logs": cmd_logs,
        "dashboard": cmd_dashboard,
    }):
        return
    if args.command == "perf":
        # perf 命令处理函数已迁移到 perf_cli.py，此处延迟导入
        from .perf_cli import (
            cmd_perf_start, cmd_perf_stop, cmd_perf_tail, cmd_perf_report,
            cmd_perf_tunneld, cmd_perf_linkmap, cmd_perf_devices, cmd_perf_config,
            cmd_perf_live, cmd_perf_rules, cmd_perf_stream, cmd_perf_snapshot,
            cmd_perf_callstack, cmd_perf_hotspots, cmd_perf_webcontent,
            cmd_perf_dashboard, cmd_perf_metrics, cmd_perf_battery,
            cmd_perf_templates, cmd_perf_symbolicate, cmd_perf_time_sync,
            cmd_perf_deep_export, cmd_perf_power_attr, cmd_perf_ai_diag,
        )
        dispatch_perf_command(args, {
            "start": cmd_perf_start,
            "stop": cmd_perf_stop,
            "tail": cmd_perf_tail,
            "report": cmd_perf_report,
            "tunneld": cmd_perf_tunneld,
            "linkmap": cmd_perf_linkmap,
            "devices": cmd_perf_devices,
            "config": cmd_perf_config,
            "live": cmd_perf_live,
            "rules": cmd_perf_rules,
            "stream": cmd_perf_stream,
            "snapshot": cmd_perf_snapshot,
            "callstack": cmd_perf_callstack,
            "hotspots": cmd_perf_hotspots,
            "webcontent": cmd_perf_webcontent,
            "dashboard": cmd_perf_dashboard,
            "metrics": cmd_perf_metrics,
            "battery": cmd_perf_battery,
            "templates": cmd_perf_templates,
            "symbolicate": cmd_perf_symbolicate,
            "time-sync": cmd_perf_time_sync,
            "deep-export": cmd_perf_deep_export,
            "power-attr": cmd_perf_power_attr,
            "ai-diag": cmd_perf_ai_diag,
        })
        return
    parser.error(f"未知命令: {args.command}")


if __name__ == "__main__":
    main()
