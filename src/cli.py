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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.orchestrator import Orchestrator
from src.validator import TaskValidator
from src.perf import PerfConfig, PerfSessionManager

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
    )


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
            return
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

    report = await orch.run()

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

    report = await orch.resume()
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

    # 加载已有结果
    from src.worker import WorkerResult
    coord_dir = orch.coord_dir
    for task in orch.tasks:
        result_file = coord_dir / "coord" / f"{task.id}.result"
        if result_file.exists():
            try:
                data = json.loads(result_file.read_text())
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
            except (json.JSONDecodeError, KeyError):
                pass

    await cmd_merge_impl(args, orch)


async def cmd_diff(args):
    """预览所有 worktree 的变更"""
    orch = Orchestrator(task_file=args.task_file)
    orch.load()

    # 加载已有结果
    from src.worker import WorkerResult
    coord_dir = orch.coord_dir
    for task in orch.tasks:
        result_file = coord_dir / "coord" / f"{task.id}.result"
        if result_file.exists():
            try:
                data = json.loads(result_file.read_text())
                orch.results[task.id] = WorkerResult(
                    task_id=data["task_id"],
                    success=data["success"],
                    worktree_path=data.get("worktree_path", ""),
                )
            except (json.JSONDecodeError, KeyError):
                pass

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

    # 加载已有结果
    from src.worker import WorkerResult
    coord_dir = orch.coord_dir
    for task in orch.tasks:
        result_file = coord_dir / "coord" / f"{task.id}.result"
        if result_file.exists():
            try:
                data = json.loads(result_file.read_text())
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
            except (json.JSONDecodeError, KeyError):
                pass

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


async def cmd_clean(args):
    """清理所有 worktree + 对应的 cp-* 分支 + 协调目录

    --prune-logs 模式下只轮转老日志，保留 worktree 和最新结果。
    """
    import subprocess
    import shutil
    import time as _time

    repo_path = Path(args.repo).expanduser().resolve()
    coord_root = repo_path / ".claude-parallel"

    # ── 分支一: 仅轮转日志/上下文/报告 ──
    if getattr(args, "prune_logs", False):
        if not coord_root.exists():
            print("  无协调目录，无需轮转")
            return

        keep_days = max(1, int(getattr(args, "keep_days", 7)))
        keep_last = max(1, int(getattr(args, "keep_last", 20)))
        cutoff = _time.time() - keep_days * 86400
        removed = 0

        # logs/ 与 context/ 按 mtime 删除
        for sub in ("logs", "context"):
            sub_dir = coord_root / sub
            if not sub_dir.exists():
                continue
            for f in sub_dir.iterdir():
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    removed += 1

        # results/ 按 mtime 保留最新 N 份
        results_dir = coord_root / "results"
        if results_dir.exists():
            reports = sorted(
                [f for f in results_dir.iterdir() if f.is_file()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for f in reports[keep_last:]:
                f.unlink(missing_ok=True)
                removed += 1

        # perf/ 下旧会话目录 (按 tag 子目录 mtime)
        perf_dir = coord_root / "perf"
        if perf_dir.exists():
            for session_dir in perf_dir.iterdir():
                if session_dir.is_dir() and session_dir.stat().st_mtime < cutoff:
                    shutil.rmtree(session_dir, ignore_errors=True)
                    removed += 1

        print(f"  轮转完成: 清理 {removed} 个过期文件/目录 (保留近 {keep_days} 天 / results 保留 {keep_last} 份)")
        return

    def _delete_cp_branch(name: str):
        """删除 cp-* 分支；失败（分支不存在或被占用）静默跳过"""
        if not name or not name.startswith("cp-"):
            return
        res = subprocess.run(
            ["git", "branch", "-D", name],
            cwd=args.repo, capture_output=True, text=True,
        )
        if res.returncode == 0:
            print(f"  删除分支: {name}")

    cleaned_branches: set[str] = set()

    proc = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True, text=True, cwd=args.repo,
    )
    # porcelain 格式: worktree <path>\nHEAD <sha>\nbranch refs/heads/<name>
    current_path = ""
    current_branch = ""
    for line in proc.stdout.splitlines() + [""]:  # 末尾空行触发 flush
        if line.startswith("worktree "):
            if current_path and "/cp-" in current_path:
                subprocess.run(
                    ["git", "worktree", "remove", current_path, "--force"],
                    cwd=args.repo,
                )
                print(f"  清理 worktree: {current_path}")
                if current_branch:
                    cleaned_branches.add(current_branch)
            current_path = line.split(" ", 1)[1]
            current_branch = ""
        elif line.startswith("branch refs/heads/"):
            current_branch = line.split("refs/heads/", 1)[1]
        elif line == "" and current_path:
            if "/cp-" in current_path:
                subprocess.run(
                    ["git", "worktree", "remove", current_path, "--force"],
                    cwd=args.repo,
                )
                print(f"  清理 worktree: {current_path}")
                if current_branch:
                    cleaned_branches.add(current_branch)
            current_path = ""
            current_branch = ""

    claude_dir = Path(args.repo) / ".claude" / "worktrees"
    if claude_dir.exists():
        for wt in claude_dir.iterdir():
            if wt.name.startswith("cp-"):
                subprocess.run(
                    ["git", "worktree", "remove", str(wt), "--force"],
                    cwd=args.repo, capture_output=True,
                )
                print(f"  清理: {wt}")
                cleaned_branches.add(wt.name)  # 按约定分支名=目录名

    # 兜底：扫所有 cp-* 本地分支一并删除
    br = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/cp-*"],
        capture_output=True, text=True, cwd=args.repo,
    )
    for name in br.stdout.splitlines():
        cleaned_branches.add(name.strip())

    for name in sorted(cleaned_branches):
        _delete_cp_branch(name)

    coord_dir = Path(args.repo) / ".claude-parallel"
    if coord_dir.exists():
        shutil.rmtree(coord_dir)
        print(f"  清理协调目录: {coord_dir}")

    print("  清理完成")


async def cmd_logs(args):
    """查看任务日志"""
    repo = Path(args.repo).expanduser().resolve()
    log_dir = repo / ".claude-parallel" / "logs"

    if not log_dir.exists():
        print("  未找到日志目录")
        return

    if args.task:
        log_file = log_dir / f"{args.task}.log"
        if not log_file.exists():
            print(f"  未找到任务 {args.task} 的日志")
            return
        lines = log_file.read_text().splitlines()
        tail = args.tail or len(lines)
        for line in lines[-tail:]:
            print(line)
    else:
        logs = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
        if not logs:
            print("  无日志文件")
            return
        print(f"  可用日志 ({len(logs)}):")
        for lf in logs:
            size = lf.stat().st_size
            print(f"    {lf.stem}.log  ({size:,} bytes)")


async def cmd_perf_start(args):
    repo = Path(args.repo).expanduser().resolve()
    cfg = PerfConfig(
        enabled=True,
        tag=args.tag,
        device=args.device or "",
        attach=args.attach or "",
        duration_sec=args.duration,
        templates=args.templates,
        baseline_tag=args.baseline or "",
        threshold_pct=args.threshold_pct or 0.0,
        sampling_enabled=getattr(args, "sampling", False),
        sampling_interval_sec=int(getattr(args, "sampling_interval", 10) or 10),
        sampling_top_n=int(getattr(args, "sampling_top", 10) or 10),
        sampling_retention=int(getattr(args, "sampling_retention", 30) or 30),
        metrics_source=getattr(args, "metrics_source", "auto") or "auto",
        metrics_interval_ms=int(getattr(args, "metrics_interval", 1000) or 1000),
        battery_interval_sec=int(getattr(args, "battery_interval", 10) or 10),
    )
    perf = PerfSessionManager(str(repo), ".claude-parallel", cfg)
    meta = perf.start()
    print(json.dumps(meta, ensure_ascii=False, indent=2))


async def cmd_perf_stop(args):
    repo = Path(args.repo).expanduser().resolve()
    cfg = PerfConfig(enabled=True, tag=args.tag)
    perf = PerfSessionManager(str(repo), ".claude-parallel", cfg)
    meta = perf.stop()
    print(json.dumps(meta, ensure_ascii=False, indent=2))


async def cmd_perf_tail(args):
    repo = Path(args.repo).expanduser().resolve()
    cfg = PerfConfig(enabled=True, tag=args.tag)
    perf = PerfSessionManager(str(repo), ".claude-parallel", cfg)
    print(perf.tail_syslog(lines=args.lines))


async def cmd_perf_report(args):
    repo = Path(args.repo).expanduser().resolve()
    cfg = PerfConfig(
        enabled=True,
        tag=args.tag,
        baseline_tag=args.baseline or "",
        threshold_pct=args.threshold_pct or 0.0,
    )
    perf = PerfSessionManager(str(repo), ".claude-parallel", cfg)
    with_cs = getattr(args, "with_callstack", False)
    cs_top_n = getattr(args, "callstack_top", 20)
    rep = perf.report(with_callstack=with_cs, callstack_top_n=cs_top_n)

    if with_cs and "callstack" in rep:
        text = perf.format_callstack_text(rep["callstack"])
        print(text)
        print()

    if getattr(args, "json", False):
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        # 简洁文本输出
        print(f"  标签: {rep.get('tag')}")
        print(f"  状态: {rep.get('status')}")
        metrics = rep.get("metrics", {})
        if metrics.get("source") != "none":
            for k in ("display_avg", "cpu_avg", "networking_avg"):
                v = metrics.get(k)
                if v is not None:
                    print(f"  {k}: {v}")
        gate = rep.get("gate", {})
        if gate.get("checked"):
            status = "PASS" if gate.get("passed") else "FAIL"
            print(f"  gate: {status} ({gate.get('reason', '')})")


async def cmd_perf_devices(args):
    import subprocess
    proc = subprocess.run(
        ["xcrun", "xctrace", "list", "devices"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        print(proc.stderr.strip() or "xctrace list devices failed")
        return
    print(proc.stdout)


async def cmd_perf_live(args):
    """实时 syslog 告警分析"""
    from src.perf import LiveLogAnalyzer, LogRule, DEFAULT_RULES

    rules = list(DEFAULT_RULES)
    if args.rules:
        custom = LiveLogAnalyzer.load_rules_from_file(args.rules)
        if custom:
            print(f"  加载自定义规则: {args.rules} ({len(custom)} 条)")
            rules = custom
        else:
            print(f"  [警告] 规则文件 {args.rules} 无有效规则, 使用内置规则")

    analyzer = LiveLogAnalyzer(
        device=args.device,
        rules=rules,
        buffer_lines=args.buffer,
    )
    status = analyzer.start()
    if status.get("status") == "error":
        print(f"  [错误] {status.get('error', 'unknown')}")
        return

    print(f"  实时 syslog 分析已启动")
    print(f"  设备: {status.get('device', 'auto')}")
    print(f"  规则: {status.get('rules_count', 0)} 条")
    print(f"  PID:  {status.get('pid', 'N/A')}")
    print(f"  按 Ctrl+C 停止\n")

    import signal as sig
    running = True

    def _stop(sig_num, frame):
        nonlocal running
        running = False

    sig.signal(sig.SIGINT, _stop)

    try:
        while running:
            summary = analyzer.get_summary()
            counts = analyzer.get_alert_counts_by_level()
            crit = counts.get("critical", 0)
            err = counts.get("error", 0)
            warn = counts.get("warn", 0)
            info = counts.get("info", 0)
            lines = summary.get("lines_processed", 0)

            status_str = "RUNNING" if analyzer.is_running() else "STOPPED"
            print(f"\r  [{status_str}] 行={lines} | "
                  f"CRITICAL={crit} ERROR={err} WARN={warn} INFO={info}   ",
                  end="", flush=True)

            # 有新告警时打印
            recent = analyzer.get_alerts(limit=1)
            if recent:
                last = recent[-1]
                ts = time.strftime("%H:%M:%S", time.localtime(last["ts"]))
                print(f"\n  [{ts}] [{last['level'].upper()}] {last['rule']}: "
                      f"{last.get('match', '')[:80]}")

            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        summary = analyzer.stop()
        print(f"\n\n  分析结束:")
        print(f"    处理行数: {summary.get('lines_processed', 0)}")
        print(f"    总告警数: {summary.get('total_alerts', 0)}")
        counts = summary.get("alert_counts", {})
        if counts:
            print(f"    告警分布:")
            for name, count in sorted(counts.items(), key=lambda x: -x[1]):
                print(f"      {name}: {count}")


async def cmd_perf_rules(args):
    """规则管理"""
    from src.perf import DEFAULT_RULES

    if args.export:
        # 导出内置规则
        rules_data = {
            "rules": [
                {
                    "name": r.name,
                    "pattern": r.pattern,
                    "level": r.level,
                    "description": r.description,
                    "max_hits": r.max_hits,
                    "window_sec": r.window_sec,
                    "mark_event": r.mark_event,
                    "throttle_sec": r.throttle_sec,
                }
                for r in DEFAULT_RULES
            ]
        }
        p = Path(args.export)
        p.write_text(json.dumps(rules_data, ensure_ascii=False, indent=2))
        print(f"  已导出 {len(DEFAULT_RULES)} 条内置规则到 {args.export}")
        return

    if args.test:
        # 测试规则 (忽略阈值/节流, 只检查 pattern 是否匹配)
        from src.perf import LogRule
        matches = []
        for rule in DEFAULT_RULES:
            # 创建无阈值限制的临时规则用于测试
            test_rule = LogRule(
                name=rule.name,
                pattern=rule.pattern,
                level=rule.level,
                description=rule.description,
                max_hits=0,        # 无阈值
                throttle_sec=0.0,  # 无节流
            )
            alert = test_rule.check(args.test)
            if alert:
                matches.append(alert)

        if matches:
            print(f"  匹配 {len(matches)} 条规则:")
            for m in matches:
                print(f"    [{m['level'].upper()}] {m['rule']}: {m['description']}")
        else:
            print(f"  无匹配规则")
        return

    # 默认: 列出所有规则
    print(f"  内置告警规则 ({len(DEFAULT_RULES)} 条):\n")
    for r in DEFAULT_RULES:
        level_colors = {
            "critical": "!!!",
            "error": "ERR",
            "warn": "WRN",
            "info": "INF",
        }
        tag = level_colors.get(r.level, "???")
        print(f"  [{tag}] {r.name}")
        print(f"       pattern: {r.pattern[:80]}")
        if r.description:
            print(f"       desc: {r.description}")
        print(f"       threshold: {r.max_hits} hits / {r.window_sec}s window")
        print()


async def cmd_perf_stream(args):
    """实时指标流 (从 xctrace trace 增量导出)"""
    from src.perf import LiveMetricsStreamer

    trace_file = Path(args.trace)
    if not trace_file.exists():
        print(f"  [错误] trace 文件不存在: {args.trace}")
        print(f"  提示: 先用 cpar perf start 启动录制")
        return

    exports_dir = trace_file.parent.parent / "exports"
    jsonl_path = str(trace_file.parent.parent / "logs" / "metrics_stream.jsonl")

    streamer = LiveMetricsStreamer(
        trace_file=str(trace_file),
        exports_dir=str(exports_dir),
        interval_sec=args.interval,
        window_size=args.window,
        jsonl_path=jsonl_path,
    )
    status = streamer.start()
    if status.get("status") not in ("running", "waiting"):
        print(f"  [错误] 启动失败: {status}")
        return

    print(f"  实时指标流已启动")
    print(f"  Trace: {args.trace}")
    print(f"  间隔: {args.interval}s, 窗口: {args.window}")
    print(f"  JSONL: {jsonl_path}")
    print(f"  按 Ctrl+C 停止\n")

    import signal as sig
    running = True

    def _stop(sig_num, frame):
        nonlocal running
        running = False

    sig.signal(sig.SIGINT, _stop)

    prev_snap_count = 0
    try:
        while running:
            summary = streamer.get_summary()
            current_snaps = summary.get("snapshots", 0)
            latest = summary.get("latest")

            if current_snaps > prev_snap_count and latest:
                parts = []
                if latest.get("display_mw") is not None:
                    parts.append(f"Display={latest['display_mw']:.0f}mW")
                if latest.get("cpu_mw") is not None:
                    parts.append(f"CPU={latest['cpu_mw']:.0f}mW")
                if latest.get("cpu_pct") is not None:
                    parts.append(f"CPU%={latest['cpu_pct']:.0f}%")
                if latest.get("gpu_fps") is not None:
                    parts.append(f"FPS={latest['gpu_fps']:.0f}")
                if latest.get("mem_mb") is not None:
                    parts.append(f"Mem={latest['mem_mb']:.0f}MB")

                ts = time.strftime("%H:%M:%S", time.localtime(latest.get("ts", 0)))
                print(f"  [{ts}] snap #{current_snaps}: {' | '.join(parts) if parts else 'no data'}")
                prev_snap_count = current_snap_count

            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        summary = streamer.stop()
        print(f"\n\n  指标流结束:")
        print(f"    快照数: {summary.get('snapshots', 0)}")
        print(f"    告警数: {summary.get('alerts', 0)}")
        stats = summary.get("stats", {})
        if stats.get("samples", 0) > 0:
            print(f"    统计:")
            for field_name in ("display_mw", "cpu_mw", "networking_mw", "cpu_pct", "gpu_fps", "mem_mb"):
                fstats = stats.get(field_name, {})
                if fstats.get("avg") is not None:
                    print(f"      {field_name}: avg={fstats['avg']}, peak={fstats['peak']}, jitter={fstats.get('jitter', 0)}")


async def cmd_perf_snapshot(args):
    """立即导出当前指标快照"""
    from src.perf.live_metrics import build_snapshot_from_exports

    trace_file = Path(args.trace)
    if not trace_file.exists():
        print(f"  [错误] trace 文件不存在: {args.trace}")
        return

    exports_dir = trace_file.parent.parent / "exports"
    snap = build_snapshot_from_exports(exports_dir, trace_file)
    if snap is None:
        print(f"  [错误] 无法导出快照")
        return

    print(f"  指标快照 ({time.strftime('%H:%M:%S')}):\n")
    data = snap.to_dict()
    for key, val in data.items():
        if key == "ts":
            continue
        if val is not None:
            print(f"    {key}: {val}")

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))


async def cmd_perf_callstack(args):
    """调用栈分析 (Time Profiler)"""
    repo = Path(args.repo).expanduser().resolve()
    cfg = PerfConfig(enabled=True, tag=args.tag)
    perf = PerfSessionManager(str(repo), ".claude-parallel", cfg)
    data = perf.callstack(
        top_n=args.top,
        min_weight=args.min_weight,
        flatten=not args.no_flatten,
    )

    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        text = perf.format_callstack_text(data, max_depth=args.max_depth)
        print(text)


async def cmd_perf_metrics(args):
    """Per-process 指标查看"""
    from src.perf.device_metrics import read_process_metrics_jsonl, format_process_metrics_text

    repo = Path(args.repo).expanduser().resolve()
    jsonl = repo / ".claude-parallel" / "perf" / args.tag / "logs" / "process_metrics.jsonl"

    if not jsonl.exists():
        print(f"  [perf] 未找到进程指标: {jsonl}")
        print(f"  提示: 需要 --perf-metrics-source device 或 auto + tunneld")
        return

    last_n = getattr(args, "last", 0)
    records = read_process_metrics_jsonl(jsonl, last_n=last_n)

    if getattr(args, "json", False):
        print(json.dumps(records, ensure_ascii=False, indent=2))
    else:
        text = format_process_metrics_text(records)
        print(text)


async def cmd_perf_battery(args):
    """电池趋势查看"""
    from src.perf.device_metrics import read_battery_jsonl, format_battery_text

    repo = Path(args.repo).expanduser().resolve()
    jsonl = repo / ".claude-parallel" / "perf" / args.tag / "logs" / "battery.jsonl"

    if not jsonl.exists():
        print(f"  [perf] 未找到电池数据: {jsonl}")
        print(f"  提示: 需要 --perf-metrics-source device 或 auto")
        return

    last_n = getattr(args, "last", 0)
    records = read_battery_jsonl(jsonl, last_n=last_n)

    if getattr(args, "json", False):
        print(json.dumps(records, ensure_ascii=False, indent=2))
    else:
        text = format_battery_text(records)
        print(text)


async def cmd_perf_dashboard(args):
    """全指标统一仪表盘"""
    import time as _time
    from src.perf.device_metrics import read_battery_jsonl

    repo = Path(args.repo).expanduser().resolve()
    tag = args.tag
    logs_dir = repo / ".claude-parallel" / "perf" / tag / "logs"
    metrics_jsonl = logs_dir / "metrics.jsonl"
    battery_jsonl = logs_dir / "battery.jsonl"

    if not metrics_jsonl.exists():
        print(f"  [perf] 未找到指标数据: {metrics_jsonl}")
        print(f"  提示: 先用 cpar perf start --templates systemtrace 采集")
        return

    # 读取指标快照
    snapshots = []
    for line in metrics_jsonl.read_text(encoding="utf-8").strip().splitlines():
        try:
            snapshots.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not snapshots:
        print("  (无指标数据)")
        return

    last_n = getattr(args, "last", 0)
    if last_n > 0:
        snapshots = snapshots[-last_n:]

    # 读取电池数据并按时间戳对齐
    battery_records = read_battery_jsonl(battery_jsonl)
    battery_by_ts = {}
    for br in battery_records:
        battery_by_ts[int(br.get("ts", 0))] = br.get("level_pct")

    def find_battery(ts):
        if not battery_by_ts:
            return None
        target = int(ts)
        closest = min(battery_by_ts.keys(), key=lambda t: abs(t - target))
        if abs(closest - target) <= 15:
            return battery_by_ts[closest]
        return None

    # 合并电池数据
    for snap in snapshots:
        if snap.get("battery_pct") is None:
            snap["battery_pct"] = find_battery(snap.get("ts", 0))

    if getattr(args, "json", False):
        print(json.dumps(snapshots, ensure_ascii=False, indent=2))
        return

    if getattr(args, "csv", False):
        print("time,display_mw,cpu_mw,network_mw,cpu_pct,fps,mem_mb,battery_pct")
        for s in snapshots:
            ts = _time.strftime("%H:%M:%S", _time.localtime(s.get("ts", 0)))
            vals = [
                ts,
                str(s.get("display_mw") or ""),
                str(s.get("cpu_mw") or ""),
                str(s.get("networking_mw") or ""),
                str(s.get("cpu_pct") or ""),
                str(s.get("gpu_fps") or ""),
                str(s.get("mem_mb") or ""),
                str(s.get("battery_pct") or ""),
            ]
            print(",".join(vals))
        return

    # 文本输出
    fields = [
        ("Time", 10, lambda s: _time.strftime("%H:%M:%S", _time.localtime(s.get("ts", 0)))),
        ("Display", 8, lambda s: f"{s['display_mw']:.0f}" if s.get("display_mw") is not None else "-"),
        ("CPU功耗", 8, lambda s: f"{s['cpu_mw']:.0f}" if s.get("cpu_mw") is not None else "-"),
        ("Network", 8, lambda s: f"{s['networking_mw']:.0f}" if s.get("networking_mw") is not None else "-"),
        ("CPU%", 7, lambda s: f"{s['cpu_pct']:.1f}%" if s.get("cpu_pct") is not None else "-"),
        ("FPS", 5, lambda s: f"{s['gpu_fps']:.0f}" if s.get("gpu_fps") is not None else "-"),
        ("内存MB", 8, lambda s: f"{s['mem_mb']:.1f}" if s.get("mem_mb") is not None else "-"),
        ("电量", 5, lambda s: f"{s['battery_pct']:.0f}%" if s.get("battery_pct") is not None else "-"),
        ("温度", 8, lambda s: s.get("thermal_state", "-") or "-"),
    ]

    # Part 1: 时序表
    header = "  ".join(f"{name:>{width}}" for name, width, _ in fields)
    sep = "  ".join("─" * width for _, width, _ in fields)
    print(f"\n  ── 指标时序 ({len(snapshots)} 个快照) ──\n")
    print(f"  {header}")
    print(f"  {sep}")
    for s in snapshots:
        row = "  ".join(f"{fn(s):>{width}}" for _, width, fn in fields)
        print(f"  {row}")

    # Part 2: 汇总统计
    print(f"\n  ── 汇总统计 ──\n")
    stat_fields = [
        ("Display mW", "display_mw"),
        ("CPU 功耗 mW", "cpu_mw"),
        ("Network mW", "networking_mw"),
        ("CPU%", "cpu_pct"),
        ("FPS", "gpu_fps"),
        ("内存 MB", "mem_mb"),
    ]
    print(f"  {'指标':<14} {'平均':>8} {'峰值':>8} {'最低':>8} {'波动':>8}")
    print(f"  {'─'*14} {'─'*8} {'─'*8} {'─'*8} {'─'*8}")
    for label, key in stat_fields:
        vals = [s.get(key) for s in snapshots if s.get(key) is not None]
        if vals:
            avg = sum(vals) / len(vals)
            peak = max(vals)
            low = min(vals)
            jitter = peak - low
            unit = "%" if "pct" in key else ""
            print(f"  {label:<14} {avg:>7.1f}{unit} {peak:>7.1f}{unit} {low:>7.1f}{unit} {'±'}{jitter:>6.1f}")
        else:
            print(f"  {label:<14} {'-':>8} {'-':>8} {'-':>8} {'-':>8}")

    # 电量趋势
    batt_vals = [s.get("battery_pct") for s in snapshots if s.get("battery_pct") is not None]
    if batt_vals:
        first_b = batt_vals[0]
        last_b = batt_vals[-1]
        delta = last_b - first_b
        print(f"  {'电量 %':<14} {first_b:>7.0f}% → {last_b:.0f}%{'':>14} {delta:>+6.0f}%")

    print()


async def cmd_perf_hotspots(args):
    """运行时热点函数查看"""
    from src.perf.sampling import read_hotspots_jsonl, format_hotspots_text

    repo = Path(args.repo).expanduser().resolve()
    hotspots_file = repo / ".claude-parallel" / "perf" / args.tag / "logs" / "hotspots.jsonl"

    if not hotspots_file.exists():
        print(f"  [perf] 未找到热点数据: {hotspots_file}")
        print(f"  提示: 启动时加 --sampling 开启旁路采集")
        return

    if getattr(args, "follow", False):
        import select

        last_pos = 0
        try:
            while True:
                if hotspots_file.exists():
                    text = hotspots_file.read_text(encoding="utf-8")
                    if len(text) > last_pos:
                        new_lines = text[last_pos:].strip().splitlines()
                        last_pos = len(text)
                        snaps = []
                        for line in new_lines:
                            try:
                                snaps.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
                        if snaps:
                            print("\033[2J\033[H", end="")
                            output = format_hotspots_text(snaps, top_n=args.top)
                            print(output)
                time.sleep(2)
        except KeyboardInterrupt:
            print("\n  [perf] follow 已停止")
        return

    aggregate = getattr(args, "aggregate", False)
    last_n = getattr(args, "last", 0)
    snaps = read_hotspots_jsonl(hotspots_file, last_n=last_n, aggregate=aggregate)

    if getattr(args, "json", False):
        print(json.dumps(snaps, ensure_ascii=False, indent=2))
    else:
        text = format_hotspots_text(snaps, top_n=args.top)
        print(text)


async def cmd_perf_templates(args):
    """模板管理"""
    from src.perf import TemplateLibrary, BUILTIN_TEMPLATES
    from src.perf.templates import (
        list_available_devices,
        list_available_templates as xctrace_templates,
        build_xctrace_record_cmd,
    )

    if args.available:
        # 列出 xctrace 内置模板
        print(f"  xctrace 可用模板:\n")
        tpls = xctrace_templates()
        if not tpls:
            print(f"    (无法获取, 请确认 Xcode 已安装)")
        for t in tpls:
            print(f"    {t['name']}")
        return

    if args.devices:
        devices = list_available_devices()
        if not devices:
            print(f"  (无设备连接)")
        else:
            print(f"  已连接设备:\n")
            for d in devices:
                print(f"    {d['name']}  UDID: {d['udid']}")
        return

    if args.build_cmd:
        # 构建并打印 xctrace 命令
        tpl_lib = TemplateLibrary()
        tpl = tpl_lib.resolve(args.build_cmd)
        if not tpl:
            print(f"  [错误] 未知模板: {args.build_cmd}")
            return
        device = args.device or "DEVICE_UDID"
        attach = args.attach or "PROCESS_NAME"
        cmd = build_xctrace_record_cmd(
            template=tpl,
            device=device,
            attach=attach,
            duration_sec=args.duration or 1800,
        )
        print(f"  模板: {tpl.name} ({tpl.alias})")
        print(f"  Schema: {', '.join(tpl.schemas)}")
        print(f"\n  命令:\n")
        print(f"    {' '.join(cmd)}")
        return

    # 默认: 列出内置模板
    tpl_lib = TemplateLibrary()
    tpls = tpl_lib.list_templates()
    print(f"  cpar 内置模板 ({len(tpls)} 个):\n")
    for t in tpls:
        print(f"  [{t['alias']}] {t['name']}")
        if t['description']:
            print(f"       {t['description']}")
        print(f"       schema: {', '.join(t.get('schemas', []))}")
        print(f"       需要 attach: {'是' if t['requires_attach'] else '否'}")
        print()


def main():
    parser = argparse.ArgumentParser(
        prog="claude-parallel",
        description=f"Claude Parallel v{VERSION} — 多 Claude Code 并行协同执行框架",
    )
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # ── run ──
    run_parser = subparsers.add_parser("run", help="执行任务")
    run_parser.add_argument("task_file", help="YAML 任务文件路径")
    run_parser.add_argument("--dry", action="store_true", help="模拟执行")
    run_parser.add_argument("--merge", action="store_true", help="执行后合并 worktree")
    run_parser.add_argument("--clean", action="store_true", help="执行后清理 worktree")
    run_parser.add_argument("--retry", type=int, default=None, help="失败重试次数")
    run_parser.add_argument("--total-budget", type=float, default=None, help="总预算上限 $")
    run_parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    run_parser.add_argument("--no-validate", action="store_true", help="跳过配置校验")
    run_parser.add_argument("--with-perf", action="store_true", help="启用真机性能 sidecar")
    run_parser.add_argument("--perf-tag", default="perf", help="perf 会话标签")
    run_parser.add_argument("--perf-device", default="", help="xctrace UDID")
    run_parser.add_argument("--perf-attach", default="", help="xctrace attach 进程名")
    run_parser.add_argument("--perf-duration", type=int, default=1800, help="xctrace 录制时长(秒)")
    run_parser.add_argument("--perf-templates", default="power", help="预留: 采集模板列表")
    run_parser.add_argument("--perf-baseline", default="", help="baseline perf tag")
    run_parser.add_argument("--perf-threshold-pct", type=float, default=0.0, help="性能退化阈值(%%)")
    run_parser.add_argument("--strict-perf-gate", action="store_true", help="perf gate 失败时返回非0")
    run_parser.add_argument("--perf-sampling", action="store_true", help="启用 Sampling Profiler 旁路")
    run_parser.add_argument("--perf-sampling-interval", type=int, default=10, help="旁路采样间隔(秒)")
    run_parser.add_argument("--perf-sampling-top", type=int, default=10, help="每 cycle Top N 热点")
    run_parser.add_argument("--perf-sampling-retention", type=int, default=30, help="保留最近 N cycle")
    run_parser.add_argument("--perf-metrics-source", default="auto", choices=["auto", "device", "xctrace"], help="指标采集源")
    run_parser.add_argument("--perf-metrics-interval", type=int, default=1000, help="per-process 采样间隔(ms)")
    run_parser.add_argument("--perf-battery-interval", type=int, default=10, help="电池轮询间隔(s)")

    # ── resume ──
    resume_parser = subparsers.add_parser("resume", help="从中断处恢复执行")
    resume_parser.add_argument("task_file", help="YAML 任务文件路径")
    resume_parser.add_argument("--merge", action="store_true", help="恢复后合并")
    resume_parser.add_argument("--clean", action="store_true", help="恢复后清理")
    resume_parser.add_argument("--retry", type=int, default=None, help="重试次数")
    resume_parser.add_argument("--total-budget", type=float, default=None, help="总预算上限")
    resume_parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    resume_parser.add_argument("--with-perf", action="store_true", help="启用真机性能 sidecar")
    resume_parser.add_argument("--perf-tag", default="perf", help="perf 会话标签")
    resume_parser.add_argument("--perf-device", default="", help="xctrace UDID")
    resume_parser.add_argument("--perf-attach", default="", help="xctrace attach 进程名")
    resume_parser.add_argument("--perf-duration", type=int, default=1800, help="xctrace 录制时长(秒)")
    resume_parser.add_argument("--perf-templates", default="power", help="预留: 采集模板列表")
    resume_parser.add_argument("--perf-baseline", default="", help="baseline perf tag")
    resume_parser.add_argument("--perf-threshold-pct", type=float, default=0.0, help="性能退化阈值(%%)")
    resume_parser.add_argument("--strict-perf-gate", action="store_true", help="perf gate 失败时返回非0")
    resume_parser.add_argument("--perf-sampling", action="store_true", help="启用 Sampling Profiler 旁路")
    resume_parser.add_argument("--perf-sampling-interval", type=int, default=10, help="旁路采样间隔(秒)")
    resume_parser.add_argument("--perf-sampling-top", type=int, default=10, help="每 cycle Top N 热点")
    resume_parser.add_argument("--perf-sampling-retention", type=int, default=30, help="保留最近 N cycle")
    resume_parser.add_argument("--perf-metrics-source", default="auto", choices=["auto", "device", "xctrace"], help="指标采集源")
    resume_parser.add_argument("--perf-metrics-interval", type=int, default=1000, help="per-process 采样间隔(ms)")
    resume_parser.add_argument("--perf-battery-interval", type=int, default=10, help="电池轮询间隔(s)")

    # ── plan ──
    plan_parser = subparsers.add_parser("plan", help="展示执行计划")
    plan_parser.add_argument("task_file", help="YAML 任务文件路径")

    # ── merge ──
    merge_parser = subparsers.add_parser("merge", help="合并 worktree (支持冲突自动解决)")
    merge_parser.add_argument("task_file", help="YAML 任务文件路径")

    # ── diff ──
    diff_parser = subparsers.add_parser("diff", help="预览所有 worktree 变更")
    diff_parser.add_argument("task_file", help="YAML 任务文件路径")

    # ── review ──
    review_parser = subparsers.add_parser("review", help="对所有变更执行 Code Review")
    review_parser.add_argument("task_file", help="YAML 任务文件路径")
    review_parser.add_argument("--budget", type=float, default=1.0, help="Review 总预算 $")

    # ── validate ──
    validate_parser = subparsers.add_parser("validate", help="校验 YAML 任务文件")
    validate_parser.add_argument("task_file", help="YAML 任务文件路径")
    validate_parser.add_argument("--with-perf", action="store_true", help="同时校验 perf 前置条件")
    validate_parser.add_argument("--perf-device", default="", help="xctrace UDID")
    validate_parser.add_argument("--perf-attach", default="", help="xctrace attach 进程名")

    # ── clean ──
    clean_parser = subparsers.add_parser("clean", help="清理 worktree 和协调文件")
    clean_parser.add_argument("repo", help="项目仓库路径")
    clean_parser.add_argument(
        "--prune-logs", action="store_true",
        help="仅轮转 logs/context/results 老文件，保留 worktree 和最新结果",
    )
    clean_parser.add_argument(
        "--keep-days", type=int, default=7,
        help="保留最近 N 天的日志/上下文 (配合 --prune-logs, 默认 7)",
    )
    clean_parser.add_argument(
        "--keep-last", type=int, default=20,
        help="results/ 目录最多保留最近 N 份报告 (配合 --prune-logs, 默认 20)",
    )

    # ── logs ──
    logs_parser = subparsers.add_parser("logs", help="查看任务日志")
    logs_parser.add_argument("repo", help="项目仓库路径")
    logs_parser.add_argument("--task", "-t", help="指定任务 ID")
    logs_parser.add_argument("--tail", "-n", type=int, default=50, help="最后 N 行")

    # ── perf ──
    perf_parser = subparsers.add_parser("perf", help="真机性能采集与报告")
    perf_sub = perf_parser.add_subparsers(dest="perf_cmd")

    perf_start = perf_sub.add_parser("start", help="启动 perf 采集")
    perf_start.add_argument("--repo", required=True, help="项目仓库路径")
    perf_start.add_argument("--tag", default="perf", help="会话标签")
    perf_start.add_argument("--device", default="", help="xctrace UDID")
    perf_start.add_argument("--attach", default="", help="xctrace attach 进程")
    perf_start.add_argument("--duration", type=int, default=1800, help="录制时长(秒)")
    perf_start.add_argument("--templates", default="power", help="模板列表")
    perf_start.add_argument("--baseline", default="", help="baseline tag")
    perf_start.add_argument("--threshold-pct", type=float, default=0.0, help="阈值(%%)")
    perf_start.add_argument("--sampling", action="store_true", help="启用 Sampling Profiler 旁路")
    perf_start.add_argument("--sampling-interval", type=int, default=10, help="旁路采样间隔(秒, 5-30)")
    perf_start.add_argument("--sampling-top", type=int, default=10, help="每 cycle 记录 Top N 热点")
    perf_start.add_argument("--sampling-retention", type=int, default=30, help="保留最近 N 个 cycle")
    perf_start.add_argument("--metrics-source", default="auto", choices=["auto", "device", "xctrace"], help="指标采集源")
    perf_start.add_argument("--metrics-interval", type=int, default=1000, help="per-process 采样间隔(ms)")
    perf_start.add_argument("--battery-interval", type=int, default=10, help="电池轮询间隔(s)")

    perf_stop = perf_sub.add_parser("stop", help="停止 perf 采集")
    perf_stop.add_argument("--repo", required=True, help="项目仓库路径")
    perf_stop.add_argument("--tag", default="perf", help="会话标签")

    perf_tail = perf_sub.add_parser("tail", help="查看实时 syslog")
    perf_tail.add_argument("--repo", required=True, help="项目仓库路径")
    perf_tail.add_argument("--tag", default="perf", help="会话标签")
    perf_tail.add_argument("--lines", type=int, default=80, help="最后 N 行")

    perf_report = perf_sub.add_parser("report", help="生成 perf 报告")
    perf_report.add_argument("--repo", required=True, help="项目仓库路径")
    perf_report.add_argument("--tag", default="perf", help="会话标签")
    perf_report.add_argument("--baseline", default="", help="baseline tag")
    perf_report.add_argument("--threshold-pct", type=float, default=0.0, help="阈值(%%)")
    perf_report.add_argument("--with-callstack", action="store_true", help="包含 Time Profiler 调用栈分析")
    perf_report.add_argument("--callstack-top", type=int, default=20, help="调用栈热点 Top N")
    perf_report.add_argument("--json", action="store_true", help="JSON 格式输出")

    perf_devices = perf_sub.add_parser("devices", help="列出 xctrace 设备")

    # ── perf live (实时 syslog 分析) ──
    perf_live = perf_sub.add_parser("live", help="实时 syslog 告警分析")
    perf_live.add_argument("--device", "-d", default="", help="设备 UDID (空=自动检测)")
    perf_live.add_argument("--rules", "-r", default="", help="自定义规则文件 (YAML/JSON)")
    perf_live.add_argument("--buffer", type=int, default=200, help="缓冲行数")
    perf_live.add_argument("--interval", type=float, default=5.0, help="状态刷新间隔(秒)")
    perf_live.add_argument("--tag", default="live", help="perf 会话标签")

    # ── perf rules (规则管理) ──
    perf_rules = perf_sub.add_parser("rules", help="列出/管理告警规则")
    perf_rules.add_argument("--list", action="store_true", help="列出所有内置规则")
    perf_rules.add_argument("--export", default="", help="导出内置规则到文件")
    perf_rules.add_argument("--test", default="", help="测试规则 (输入日志文本)")

    # ── perf stream (实时指标流) ──
    perf_stream = perf_sub.add_parser("stream", help="实时 xctrace 指标流")
    perf_stream.add_argument("trace", help="xctrace trace 文件路径")
    perf_stream.add_argument("--interval", type=float, default=10.0, help="导出间隔(秒)")
    perf_stream.add_argument("--window", type=int, default=30, help="滚动窗口快照数")

    # ── perf snapshot (一次性快照) ──
    perf_snap = perf_sub.add_parser("snapshot", help="立即导出指标快照")
    perf_snap.add_argument("trace", help="xctrace trace 文件路径")
    perf_snap.add_argument("--json", action="store_true", help="JSON 格式输出")

    # ── perf dashboard (全指标仪表盘) ──
    perf_dash = perf_sub.add_parser("dashboard", help="全指标统一仪表盘 (时序表+汇总)")
    perf_dash.add_argument("--repo", required=True, help="项目仓库路径")
    perf_dash.add_argument("--tag", default="perf", help="会话标签")
    perf_dash.add_argument("--last", type=int, default=0, help="最近 N 个快照 (0=全部)")
    perf_dash.add_argument("--json", action="store_true", help="JSON 格式输出")
    perf_dash.add_argument("--csv", action="store_true", help="CSV 格式输出")

    # ── perf metrics (per-process 指标) ──
    perf_metrics = perf_sub.add_parser("metrics", help="Per-process CPU/内存指标")
    perf_metrics.add_argument("--repo", required=True, help="项目仓库路径")
    perf_metrics.add_argument("--tag", default="perf", help="会话标签")
    perf_metrics.add_argument("--last", type=int, default=10, help="最近 N 条")
    perf_metrics.add_argument("--json", action="store_true", help="JSON 格式输出")

    # ── perf battery (电池趋势) ──
    perf_battery = perf_sub.add_parser("battery", help="电池功耗趋势")
    perf_battery.add_argument("--repo", required=True, help="项目仓库路径")
    perf_battery.add_argument("--tag", default="perf", help="会话标签")
    perf_battery.add_argument("--last", type=int, default=10, help="最近 N 条")
    perf_battery.add_argument("--json", action="store_true", help="JSON 格式输出")

    # ── perf hotspots (运行时热点) ──
    perf_hotspots = perf_sub.add_parser("hotspots", help="运行时热点函数查看")
    perf_hotspots.add_argument("--repo", required=True, help="项目仓库路径")
    perf_hotspots.add_argument("--tag", default="perf", help="会话标签")
    perf_hotspots.add_argument("--follow", "-f", action="store_true", help="实时追踪 (tail -f 式)")
    perf_hotspots.add_argument("--top", type=int, default=10, help="Top N 热点")
    perf_hotspots.add_argument("--last", type=int, default=0, help="最近 N 个 cycle (0=全部)")
    perf_hotspots.add_argument("--aggregate", action="store_true", help="全会话聚合")
    perf_hotspots.add_argument("--json", action="store_true", help="JSON 格式输出")

    # ── perf callstack (调用栈分析) ──
    perf_cs = perf_sub.add_parser("callstack", help="Time Profiler 调用栈分析")
    perf_cs.add_argument("--repo", required=True, help="项目仓库路径")
    perf_cs.add_argument("--tag", default="perf", help="会话标签")
    perf_cs.add_argument("--top", type=int, default=20, help="热点函数 Top N")
    perf_cs.add_argument("--min-weight", type=float, default=0.5, help="最小权重百分比")
    perf_cs.add_argument("--max-depth", type=int, default=8, help="调用路径最大显示深度")
    perf_cs.add_argument("--no-flatten", action="store_true", help="不聚合函数(保留完整路径)")
    perf_cs.add_argument("--json", action="store_true", help="JSON 格式输出")

    # ── perf templates (模板管理) ──
    perf_tpl = perf_sub.add_parser("templates", help="Instruments 模板管理")
    perf_tpl.add_argument("--list", action="store_true", help="列出内置模板")
    perf_tpl.add_argument("--available", action="store_true", help="列出 xctrace 可用模板")
    perf_tpl.add_argument("--devices", action="store_true", help="列出已连接设备")
    perf_tpl.add_argument("--build-cmd", default="", help="构建录制命令 (模板别名)")
    perf_tpl.add_argument("--device", default="", help="设备 UDID (配合 --build-cmd)")
    perf_tpl.add_argument("--attach", default="", help="进程名 (配合 --build-cmd)")
    perf_tpl.add_argument("--duration", type=int, default=0, help="录制时长(秒) (配合 --build-cmd)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command not in ("logs", "validate"):
        print_banner()

    if args.command == "run":
        asyncio.run(cmd_run(args))
    elif args.command == "resume":
        asyncio.run(cmd_resume(args))
    elif args.command == "plan":
        asyncio.run(cmd_plan(args))
    elif args.command == "merge":
        asyncio.run(cmd_merge(args))
    elif args.command == "diff":
        asyncio.run(cmd_diff(args))
    elif args.command == "review":
        asyncio.run(cmd_review(args))
    elif args.command == "validate":
        asyncio.run(cmd_validate(args))
    elif args.command == "clean":
        asyncio.run(cmd_clean(args))
    elif args.command == "logs":
        asyncio.run(cmd_logs(args))
    elif args.command == "perf":
        if args.perf_cmd == "start":
            asyncio.run(cmd_perf_start(args))
        elif args.perf_cmd == "stop":
            asyncio.run(cmd_perf_stop(args))
        elif args.perf_cmd == "tail":
            asyncio.run(cmd_perf_tail(args))
        elif args.perf_cmd == "report":
            asyncio.run(cmd_perf_report(args))
        elif args.perf_cmd == "devices":
            asyncio.run(cmd_perf_devices(args))
        elif args.perf_cmd == "live":
            asyncio.run(cmd_perf_live(args))
        elif args.perf_cmd == "rules":
            asyncio.run(cmd_perf_rules(args))
        elif args.perf_cmd == "stream":
            asyncio.run(cmd_perf_stream(args))
        elif args.perf_cmd == "snapshot":
            asyncio.run(cmd_perf_snapshot(args))
        elif args.perf_cmd == "callstack":
            asyncio.run(cmd_perf_callstack(args))
        elif args.perf_cmd == "hotspots":
            asyncio.run(cmd_perf_hotspots(args))
        elif args.perf_cmd == "dashboard":
            asyncio.run(cmd_perf_dashboard(args))
        elif args.perf_cmd == "metrics":
            asyncio.run(cmd_perf_metrics(args))
        elif args.perf_cmd == "battery":
            asyncio.run(cmd_perf_battery(args))
        elif args.perf_cmd == "templates":
            asyncio.run(cmd_perf_templates(args))
        else:
            print("  用法: cpar perf <start|stop|tail|report|devices|live|rules|stream|snapshot|callstack|hotspots|metrics|battery|templates> ...")


if __name__ == "__main__":
    main()
