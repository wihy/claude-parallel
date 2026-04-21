import asyncio
import inspect
import sys
import time
from pathlib import Path


OPS_USAGE = (
    "  用法: cpar "
    "<clean|logs|dashboard> ..."
)


def register_ops_subcommands(subparsers):
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
    clean_parser.add_argument(
        "--force", action="store_true",
        help="跳过其他活动 cpar 实例的占用检查 (危险)",
    )

    logs_parser = subparsers.add_parser("logs", help="查看任务日志")
    logs_parser.add_argument("repo", help="项目仓库路径")
    logs_parser.add_argument("--task", "-t", help="指定任务 ID")
    logs_parser.add_argument("--tail", "-n", type=int, default=50, help="最后 N 行")

    dash_parser = subparsers.add_parser("dashboard", help="启动 Web Dashboard 浏览实时调度+功耗")
    dash_parser.add_argument("--repo", default="", help="项目仓库路径 (默认当前目录)")
    dash_parser.add_argument("--tag", default="perf", help="perf 会话标签 (默认 perf)")
    dash_parser.add_argument("--port", type=int, default=8765, help="HTTP 端口 (默认 8765)")
    dash_parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1)")
    dash_parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    dash_parser.add_argument(
        "--source", action="append", default=[],
        help="源码仓库 NAME=PATH，可重复指定 (如: --source soul=~/SoulApp --source pods=~/Pods)",
    )
    dash_parser.add_argument(
        "--linkmap-project", default="Soul_New",
        help="LinkMap 工程名 (DerivedData 下匹配, 默认 Soul_New)",
    )


def dispatch_ops_command(args, handlers):
    handler = handlers.get(getattr(args, "command", None))
    if handler is None:
        return False
    if inspect.iscoroutinefunction(handler):
        asyncio.run(handler(args))
    else:
        handler(args)
    return True


def cmd_dashboard(args):
    """独立 Web Dashboard 模式 — 不需要 orchestrator，纯 perf 监控。

    适用场景: 已经在跑 cpar perf start，想用浏览器看实时电池/CPU/网络/告警。
    """
    from src.infrastructure.dashboard.server import DashboardServer, collect_perf_state

    repo = Path(args.repo).expanduser().resolve() if args.repo else Path.cwd().resolve()
    coord_dir = ".claude-parallel"
    perf_root = repo / coord_dir / "perf" / args.tag
    if not perf_root.exists():
        print(f"  [dashboard] perf 会话目录不存在: {perf_root}")
        print(f"  [dashboard] 提示: 先用 'cpar perf start --tag {args.tag}' 启动采集")
        sys.exit(1)

    sources_dict = {}
    for spec in (getattr(args, "source", None) or []):
        if "=" in spec:
            name, path = spec.split("=", 1)
            sources_dict[name.strip()] = path.strip()
        else:
            p = Path(spec).expanduser()
            sources_dict[p.name or "default"] = str(p)

    try:
        from src.perf.linkmap import MultiLinkMap, find_linkmaps
        from src.infrastructure.dashboard.server import set_global_linkmap

        project = getattr(args, "linkmap_project", "Soul_New")
        files = find_linkmaps(project_name=project, arch="arm64")
        if files:
            print(f"  ▶ 加载 {len(files)} 个 LinkMap (project={project})...")
            mlm = MultiLinkMap.warm_all_from_derived_data(project_name=project, max_workers=4)
            set_global_linkmap(mlm)
            stats = mlm.stats()
            print(
                f"  ✓ LinkMap 就绪: {stats['total_symbols']:,} 符号 "
                f"(业务 {stats['biz_symbols']:,} OC {stats['objc_symbols']:,})"
            )
        else:
            print(f"  · 未找到 LinkMap (project={project}), hotspots 不做二次符号化")
    except Exception as e:
        print(f"  ⚠ LinkMap 加载失败: {e}")

    srv = DashboardServer(
        port=args.port,
        host=args.host,
        orch_provider=lambda: {"enabled": False},
        perf_provider=lambda: collect_perf_state(repo, coord_dir, args.tag),
        title=f"cpar Dashboard — perf:{args.tag}",
        sources=sources_dict,
    )
    try:
        url = srv.start()
    except OSError as e:
        print(f"  [dashboard] 启动失败 (端口 {args.port}): {e}")
        sys.exit(1)
    print(f"\n  ╔══════════════════════════════════════════════════════╗")
    print(f"  ║  Web Dashboard 已启动                                ║")
    print(f"  ║  URL: {url:<48}║")
    print(f"  ║  Perf 会话: {args.tag:<42}║")
    if srv.sources:
        print(f"  ║  源码定位: {len(srv.sources)} 个 repo: {', '.join(srv.sources.keys())[:38]:<38}║")
    print(f"  ║  Ctrl+C 退出                                         ║")
    print(f"  ╚══════════════════════════════════════════════════════╝\n")

    if not getattr(args, "no_open", False):
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n  [dashboard] 收到中断，关闭服务...")
    finally:
        srv.stop()
    print("  [dashboard] 已关闭")


async def cmd_clean(args):
    """清理所有 worktree + 对应的 cp-* 分支 + 协调目录

    --prune-logs 模式下只轮转老日志，保留 worktree 和最新结果。
    """
    import shutil
    import subprocess
    import time as _time

    from src.infrastructure.storage.atomic import list_active_locks

    repo_path = Path(args.repo).expanduser().resolve()
    coord_root = repo_path / ".claude-parallel"

    if getattr(args, "prune_logs", False):
        if not coord_root.exists():
            print("  无协调目录，无需轮转")
            return

        keep_days = max(1, int(getattr(args, "keep_days", 7)))
        keep_last = max(1, int(getattr(args, "keep_last", 20)))
        cutoff = _time.time() - keep_days * 86400
        removed = 0

        for sub in ("logs", "context"):
            sub_dir = coord_root / sub
            if not sub_dir.exists():
                continue
            for f in sub_dir.iterdir():
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    removed += 1

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

        perf_dir = coord_root / "perf"
        if perf_dir.exists():
            for session_dir in perf_dir.iterdir():
                if session_dir.is_dir() and session_dir.stat().st_mtime < cutoff:
                    shutil.rmtree(session_dir, ignore_errors=True)
                    removed += 1

        print(f"  轮转完成: 清理 {removed} 个过期文件/目录 (保留近 {keep_days} 天 / results 保留 {keep_last} 份)")
        return

    locks_dir = coord_root / ".locks"
    active = list_active_locks(locks_dir, exclude_self=True)
    if active and not getattr(args, "force", False):
        print(f"  [clean] 拒绝清理: 检测到 {len(active)} 个其他 cpar 实例正在运行 (PID: {active})")
        print(f"  [clean] 锁目录: {locks_dir}")
        print(f"  [clean] 如确认无冲突，加 --force 绕过检查")
        sys.exit(2)

    def _delete_cp_branch(name: str):
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
    current_path = ""
    current_branch = ""
    for line in proc.stdout.splitlines() + [""]:
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
                cleaned_branches.add(wt.name)

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
        return

    logs = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        print("  无日志文件")
        return
    print(f"  可用日志 ({len(logs)}):")
    for lf in logs:
        size = lf.stat().st_size
        print(f"    {lf.stem}.log  ({size:,} bytes)")
