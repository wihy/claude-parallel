import asyncio
import inspect


EXECUTION_USAGE = (
    "  用法: cpar "
    "<run|resume|plan|merge|diff|review|validate> ..."
)


def register_execution_subcommands(subparsers):
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
    run_parser.add_argument("--perf-attach-webcontent", action="store_true", help="采集 WebContent 进程")
    run_parser.add_argument("--perf-composite", default="auto", help="Composite 模式: auto|full|webperf|power_cpu|gpu_full|memory")
    run_parser.add_argument("--web-dashboard", action="store_true", help="启动浏览器仪表盘 (替代 Rich Live)")
    run_parser.add_argument("--web-port", type=int, default=8765, help="dashboard 端口 (默认 8765)")

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
    resume_parser.add_argument("--perf-attach-webcontent", action="store_true", help="采集 WebContent 进程")
    resume_parser.add_argument("--web-dashboard", action="store_true", help="启动浏览器仪表盘")
    resume_parser.add_argument("--web-port", type=int, default=8765, help="dashboard 端口")

    plan_parser = subparsers.add_parser("plan", help="展示执行计划")
    plan_parser.add_argument("task_file", help="YAML 任务文件路径")

    merge_parser = subparsers.add_parser("merge", help="合并 worktree (支持冲突自动解决)")
    merge_parser.add_argument("task_file", help="YAML 任务文件路径")

    diff_parser = subparsers.add_parser("diff", help="预览所有 worktree 变更")
    diff_parser.add_argument("task_file", help="YAML 任务文件路径")

    review_parser = subparsers.add_parser("review", help="对所有变更执行 Code Review")
    review_parser.add_argument("task_file", help="YAML 任务文件路径")
    review_parser.add_argument("--budget", type=float, default=1.0, help="Review 总预算 $")

    validate_parser = subparsers.add_parser("validate", help="校验 YAML 任务文件")
    validate_parser.add_argument("task_file", help="YAML 任务文件路径")
    validate_parser.add_argument("--with-perf", action="store_true", help="同时校验 perf 前置条件")
    validate_parser.add_argument("--perf-device", default="", help="xctrace UDID")
    validate_parser.add_argument("--perf-attach", default="", help="xctrace attach 进程名")


def dispatch_execution_command(args, handlers):
    handler = handlers.get(getattr(args, "command", None))
    if handler is None:
        return False
    if inspect.iscoroutinefunction(handler):
        asyncio.run(handler(args))
    else:
        handler(args)
    return True
