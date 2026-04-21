#!/usr/bin/env python3
"""
perf 子命令实现 — 真机性能采集、报告、分析。

从 src/app/cli.py 提取的独立模块。argparse 注册在 register_perf_subcommands()，
命令实现在各个 cmd_perf_* 函数中，调度入口 dispatch_perf_command()。
"""

import asyncio
import inspect
import json
import sys
import time
from pathlib import Path

PERF_USAGE = (
    "  用法: cpar <perf|analyze> "
    "<start|stop|tail|report|devices|config|live|rules|stream|snapshot|"
    "callstack|hotspots|metrics|battery|templates|symbolicate|time-sync|"
    "deep-export|power-attr|ai-diag> ..."
)

from src.perf import PerfConfig, PerfSessionManager
from src.perf.perf_defaults import PerfDefaults

def register_perf_subcommands(subparsers, command_name: str = "perf", help_text: str = "真机性能采集与报告"):
    perf_parser = subparsers.add_parser(command_name, help=help_text)
    perf_sub = perf_parser.add_subparsers(dest="perf_cmd")

    perf_start = perf_sub.add_parser("start", help="启动 perf 采集")
    perf_start.add_argument("--repo", default="", help="项目仓库路径 (未指定则使用 config 默认)")
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
    perf_start.add_argument("--attach-webcontent", action="store_true", help="自动发现并采集 WebContent 进程 (JS/WebKit)")
    perf_start.add_argument("--metrics-source", default="auto", choices=["auto", "device", "xctrace"], help="指标采集源")
    perf_start.add_argument("--metrics-interval", type=int, default=1000, help="per-process 采样间隔(ms)")
    perf_start.add_argument("--battery-interval", type=int, default=10, help="电池轮询间隔(s)")
    perf_start.add_argument(
        "--composite", default="auto",
        help='Composite 模式: auto|full|webperf|power_cpu|gpu_full|memory|power+time+network|""',
    )
    perf_start.add_argument("--no-tunneld", action="store_true", help="跳过自动启动 tunneld (iOS 17+ DVT 通道将不可用)")
    # resolver 激活参数: 同时接受 --xxx 和 --perf-xxx 两种前缀 (与 run --with-perf 对齐)
    # --binary: iOS Debug build 需指向 .app/<AppName>.debug.dylib (launcher stub 几乎无符号)
    perf_start.add_argument(
        "--binary", "--perf-binary", dest="binary", default="",
        help=(
            "主 binary 路径 (用于 atos 符号化)。"
            "iOS Debug build: 传入 .app 目录会自动用内部 .debug.dylib (launcher stub 无符号)。"
            "iOS Release build: 传入 .app/<AppName> 主 binary。"
        ),
    )
    perf_start.add_argument(
        "--linkmap", "--perf-linkmap", dest="linkmap", action="append", default=[],
        help="LinkMap 文件路径 (业务符号解析, 可多次指定叠加)",
    )
    perf_start.add_argument(
        "--dsym", "--perf-dsym", dest="dsym", action="append", default=[],
        help="dSYM 路径 (可多次指定)",
    )

    perf_stop = perf_sub.add_parser("stop", help="停止 perf 采集")
    perf_stop.add_argument("--repo", default="", help="项目仓库路径 (未指定则使用 config 默认)")
    perf_stop.add_argument("--tag", default="perf", help="会话标签")
    perf_stop.add_argument("--clean", action="store_true", help="停止后直接清理本次 session 数据 (跳过询问)")
    perf_stop.add_argument("--no-clean", action="store_true", help="停止后不清理 (跳过询问，保留数据)")
    perf_stop.add_argument("--keep-report", action="store_true", help="清理时保留 report.html / report.json (清理 logs/traces)")

    perf_tail = perf_sub.add_parser("tail", help="查看实时 syslog")
    perf_tail.add_argument("--repo", default="", help="项目仓库路径 (未指定则使用 config 默认)")
    perf_tail.add_argument("--tag", default="perf", help="会话标签")
    perf_tail.add_argument("--lines", type=int, default=80, help="最后 N 行")

    perf_report = perf_sub.add_parser("report", help="生成 perf 报告")
    perf_report.add_argument("--repo", default="", help="项目仓库路径 (未指定则使用 config 默认)")
    perf_report.add_argument("--tag", default="perf", help="会话标签")
    perf_report.add_argument("--baseline", default="", help="baseline tag")
    perf_report.add_argument("--threshold-pct", type=float, default=0.0, help="阈值(%%)")
    perf_report.add_argument("--with-callstack", action="store_true", help="包含 Time Profiler 调用栈分析")
    perf_report.add_argument("--callstack-top", type=int, default=20, help="调用栈热点 Top N")
    perf_report.add_argument("--json", action="store_true", help="JSON 格式输出")
    perf_report.add_argument("--html", action="store_true", help="生成自包含 HTML 报告 (chart.js)")
    perf_report.add_argument("--html-output", default="", help="HTML 输出路径 (默认 <session>/report.html)")
    perf_report.add_argument("--clean", action="store_true", help="生成报告后清理 logs/traces (默认会询问)")
    perf_report.add_argument("--no-clean", action="store_true", help="生成报告后不询问清理")
    perf_report.add_argument("--keep-report", action="store_true", default=True, help="清理时保留 report.html/json (默认 True)")

    perf_sub.add_parser("devices", help="列出 xctrace 设备")

    perf_linkmap = perf_sub.add_parser("linkmap", help="LinkMap 解析: find/parse/lookup/search 业务符号")
    perf_linkmap.add_argument("lm_action", nargs="?", default="find", choices=["find", "parse", "lookup", "search", "warm", "bench"], help="操作类型 (warm=预热缓存, bench=性能基准)")
    perf_linkmap.add_argument("lm_args", nargs="*", help="子命令参数")
    perf_linkmap.add_argument("--project", default="Soul_New", help="find: 工程名 (默认 Soul_New)")
    perf_linkmap.add_argument("--arch", default="arm64", help="find: 架构 (默认 arm64)")
    perf_linkmap.add_argument("--max", type=int, default=20, help="search: 最大返回数")
    perf_linkmap.add_argument("--json", action="store_true", help="parse: JSON 输出")

    perf_tunneld = perf_sub.add_parser("tunneld", help="管理 pymobiledevice3 RemoteXPC tunneld (iOS 17+ DVT 必需，需 sudo)")
    perf_tunneld.add_argument("action", nargs="?", default="status", choices=["start", "stop", "status", "ensure"], help="status=查看 / start=启动 / stop=停止 / ensure=不存在则启动 (默认 status)")
    perf_tunneld.add_argument("--no-prompt", action="store_true", help="不弹 osascript 密码框 (start 时若无 sudo 缓存则失败)")

    perf_config = perf_sub.add_parser("config", help="查看/修改 perf 默认配置")
    perf_config_sub = perf_config.add_subparsers(dest="config_action")
    perf_config_sub.add_parser("show", help="查看当前默认配置")
    perf_config_set = perf_config_sub.add_parser("set", help="设置默认值")
    perf_config_set.add_argument("field", help="字段名 (repo/attach/tag/templates/...)")
    perf_config_set.add_argument("value", help="字段值")
    perf_config_unset = perf_config_sub.add_parser("unset", help="清除默认值")
    perf_config_unset.add_argument("field", help="字段名")

    perf_live = perf_sub.add_parser("live", help="实时 syslog 告警分析")
    perf_live.add_argument("--device", "-d", default="", help="设备 UDID (空=自动检测)")
    perf_live.add_argument("--rules", "-r", default="", help="自定义规则文件 (YAML/JSON)")
    perf_live.add_argument("--buffer", type=int, default=200, help="缓冲行数")
    perf_live.add_argument("--interval", type=float, default=5.0, help="状态刷新间隔(秒)")
    perf_live.add_argument("--tag", default="live", help="perf 会话标签")

    perf_rules = perf_sub.add_parser("rules", help="列出/管理告警规则")
    perf_rules.add_argument("--list", action="store_true", help="列出所有内置规则")
    perf_rules.add_argument("--export", default="", help="导出内置规则到文件")
    perf_rules.add_argument("--test", default="", help="测试规则 (输入日志文本)")

    perf_stream = perf_sub.add_parser("stream", help="实时 xctrace 指标流")
    perf_stream.add_argument("trace", help="xctrace trace 文件路径")
    perf_stream.add_argument("--interval", type=float, default=10.0, help="导出间隔(秒)")
    perf_stream.add_argument("--window", type=int, default=30, help="滚动窗口快照数")

    perf_snap = perf_sub.add_parser("snapshot", help="立即导出指标快照")
    perf_snap.add_argument("trace", help="xctrace trace 文件路径")
    perf_snap.add_argument("--json", action="store_true", help="JSON 格式输出")

    perf_wc = perf_sub.add_parser("webcontent", help="WebContent 进程 JS/WebKit 热点")
    perf_wc.add_argument("--repo", default="", help="项目仓库路径 (未指定则使用 config 默认)")
    perf_wc.add_argument("--tag", default="perf", help="会话标签")
    perf_wc.add_argument("--top", type=int, default=15, help="Top N 热点")
    perf_wc.add_argument("--last", type=int, default=0, help="最近 N 个 cycle")
    perf_wc.add_argument("--json", action="store_true", help="JSON 格式输出")

    perf_dash = perf_sub.add_parser("dashboard", help="全指标统一仪表盘 (时序表+汇总)")
    perf_dash.add_argument("--repo", default="", help="项目仓库路径 (未指定则使用 config 默认)")
    perf_dash.add_argument("--tag", default="perf", help="会话标签")
    perf_dash.add_argument("--last", type=int, default=0, help="最近 N 个快照 (0=全部)")
    perf_dash.add_argument("--json", action="store_true", help="JSON 格式输出")
    perf_dash.add_argument("--csv", action="store_true", help="CSV 格式输出")

    perf_metrics = perf_sub.add_parser("metrics", help="Per-process CPU/内存指标")
    perf_metrics.add_argument("--repo", default="", help="项目仓库路径 (未指定则使用 config 默认)")
    perf_metrics.add_argument("--tag", default="perf", help="会话标签")
    perf_metrics.add_argument("--last", type=int, default=10, help="最近 N 条")
    perf_metrics.add_argument("--json", action="store_true", help="JSON 格式输出")

    perf_battery = perf_sub.add_parser("battery", help="电池功耗趋势")
    perf_battery.add_argument("--repo", default="", help="项目仓库路径 (未指定则使用 config 默认)")
    perf_battery.add_argument("--tag", default="perf", help="会话标签")
    perf_battery.add_argument("--last", type=int, default=10, help="最近 N 条")
    perf_battery.add_argument("--json", action="store_true", help="JSON 格式输出")

    perf_hotspots = perf_sub.add_parser("hotspots", help="运行时热点函数查看")
    perf_hotspots.add_argument("--repo", default="", help="项目仓库路径 (未指定则使用 config 默认)")
    perf_hotspots.add_argument("--tag", default="perf", help="会话标签")
    perf_hotspots.add_argument("--follow", "-f", action="store_true", help="实时追踪 (tail -f 式)")
    perf_hotspots.add_argument("--top", type=int, default=10, help="Top N 热点")
    perf_hotspots.add_argument("--last", type=int, default=0, help="最近 N 个 cycle (0=全部)")
    perf_hotspots.add_argument("--aggregate", action="store_true", help="全会话聚合")
    perf_hotspots.add_argument("--json", action="store_true", help="JSON 格式输出")

    perf_cs = perf_sub.add_parser("callstack", help="Time Profiler 调用栈分析")
    perf_cs.add_argument("--repo", default="", help="项目仓库路径 (未指定则使用 config 默认)")
    perf_cs.add_argument("--tag", default="perf", help="会话标签")
    perf_cs.add_argument("--top", type=int, default=20, help="热点函数 Top N")
    perf_cs.add_argument("--min-weight", type=float, default=0.5, help="最小权重百分比")
    perf_cs.add_argument("--max-depth", type=int, default=8, help="调用路径最大显示深度")
    perf_cs.add_argument("--no-flatten", action="store_true", help="不聚合函数(保留完整路径)")
    perf_cs.add_argument("--full-stack", action="store_true", help="保留完整调用链（含所有 frame）")
    perf_cs.add_argument("--from", dest="time_from", type=float, default=0, help="时间切片起点（秒）")
    perf_cs.add_argument("--to", dest="time_to", type=float, default=0, help="时间切片终点（秒）")
    perf_cs.add_argument("--json", action="store_true", help="JSON 格式输出")

    perf_tpl = perf_sub.add_parser("templates", help="Instruments 模板管理")
    perf_tpl.add_argument("--list", action="store_true", help="列出内置模板")
    perf_tpl.add_argument("--available", action="store_true", help="列出 xctrace 可用模板")
    perf_tpl.add_argument("--devices", action="store_true", help="列出已连接设备")
    perf_tpl.add_argument("--build-cmd", default="", help="构建录制命令 (模板别名)")
    perf_tpl.add_argument("--device", default="", help="设备 UDID (配合 --build-cmd)")
    perf_tpl.add_argument("--attach", default="", help="进程名 (配合 --build-cmd)")
    perf_tpl.add_argument("--duration", type=int, default=0, help="录制时长(秒) (配合 --build-cmd)")

    perf_sym = perf_sub.add_parser("symbolicate", help="dSYM 符号化调用栈地址")
    perf_sym.add_argument("--repo", default="", help="项目仓库路径 (未指定则使用 config 默认)")
    perf_sym.add_argument("--tag", default="perf", help="会话标签")
    perf_sym.add_argument("--app-id", default="", help="App Bundle ID (用于查找 dSYM)")
    perf_sym.add_argument("--dsym", default="", help="指定 dSYM 路径")
    perf_sym.add_argument("--uuid", default="", help="dSYM UUID (Spotlight 搜索)")
    perf_sym.add_argument("--arch", default="arm64", help="架构 (默认 arm64)")
    perf_sym.add_argument("--top", type=int, default=20, help="符号化 Top N 热点")
    perf_sym.add_argument("--json", action="store_true", help="JSON 格式输出")

    perf_ts = perf_sub.add_parser("time-sync", help="syslog-xctrace 时序对齐 + 事件归因")
    perf_ts.add_argument("--repo", default="", help="项目仓库路径 (未指定则使用 config 默认)")
    perf_ts.add_argument("--tag", default="perf", help="会话标签")
    perf_ts.add_argument("--syslog", default="", help="syslog 文件路径 (空=自动查找)")
    perf_ts.add_argument("--window", type=int, default=5, help="事件关联窗口(秒)")
    perf_ts.add_argument("--json", action="store_true", help="JSON 格式输出")

    perf_de = perf_sub.add_parser("deep-export", help="深度 Schema 采集 (GPU/Network/VM/Metal)")
    perf_de.add_argument("--repo", default="", help="项目仓库路径 (未指定则使用 config 默认)")
    perf_de.add_argument("--tag", default="perf", help="会话标签")
    perf_de.add_argument("--schemas", default="all", help="Schema 列表 (逗号分隔: gpu,network,vm,metal 或 all)")
    perf_de.add_argument("--json", action="store_true", help="JSON 格式输出")

    perf_pa = perf_sub.add_parser("power-attr", help="进程级功耗归因分析")
    perf_pa.add_argument("--repo", default="", help="项目仓库路径 (未指定则使用 config 默认)")
    perf_pa.add_argument("--tag", default="perf", help="会话标签")
    perf_pa.add_argument("--json", action="store_true", help="JSON 格式输出")

    perf_ai = perf_sub.add_parser("ai-diag", help="AI 辅助性能诊断")
    perf_ai.add_argument("--repo", default="", help="项目仓库路径 (未指定则使用 config 默认)")
    perf_ai.add_argument("--tag", default="perf", help="会话标签")
    perf_ai.add_argument("--focus", default="general", choices=["general", "webkit", "power", "memory", "gpu"], help="分析重点")
    perf_ai.add_argument("--baseline-tag", default="", help="基线标签 (对比分析)")
    perf_ai.add_argument("--offline", action="store_true", help="离线模式 (只生成 prompt)")
    perf_ai.add_argument("--model", default="", help="LLM 模型 (覆盖环境变量)")
    perf_ai.add_argument("--json", action="store_true", help="JSON 格式输出")


def dispatch_perf_command(args, handlers):
    handler = handlers.get(getattr(args, "perf_cmd", None))
    if handler is None:
        print(PERF_USAGE)
        return
    if inspect.iscoroutinefunction(handler):
        asyncio.run(handler(args))
    else:
        handler(args)

# ── perf 命令实现 (从 cli.py 提取) ──────────────────────────────

def _resolve_perf_repo_tag(args, require_repo=True):
    """统一解析 perf 子命令的 repo 和 tag 参数。

    优先级: CLI 显式参数 > 环境变量 > ~/.cpar/perf_defaults.json > 硬编码默认值

    Returns: (repo_path: Path, tag: str, defaults: PerfDefaults) 或 (None, None, defaults)
    """
    defaults = PerfDefaults.load()
    repo_str = defaults.resolve("repo", getattr(args, "repo", None))
    if require_repo and not repo_str:
        print("  错误: 未指定项目仓库路径。")
        print("  用法: cpar perf <cmd> --repo /path/to/project")
        print("  或先设置默认: cpar perf config set repo /path/to/project")
        return None, None, defaults
    tag = defaults.resolve("tag", getattr(args, "tag", None), "perf")
    repo = Path(repo_str).expanduser().resolve() if repo_str else None
    return repo, tag, defaults


async def cmd_perf_start(args):
    repo, tag, defaults = _resolve_perf_repo_tag(args)
    if not repo:
        return

    # iOS 17+ DVT 通道需要 tunneld；自动检测/启动
    _ensure_tunneld_for_perf(args)

    attach = defaults.resolve("attach", getattr(args, "attach", None), "")
    cfg = PerfConfig(
        enabled=True,
        tag=tag,
        device=args.device or "",
        attach=attach,
        duration_sec=int(defaults.resolve("duration", getattr(args, "duration", None), 1800)),
        templates=defaults.resolve("templates", getattr(args, "templates", None), "power"),
        baseline_tag=defaults.resolve("baseline", getattr(args, "baseline", None), ""),
        threshold_pct=float(defaults.resolve("threshold_pct", getattr(args, "threshold_pct", None), 0.0)),
        sampling_enabled=getattr(args, "sampling", False),
        sampling_interval_sec=int(defaults.resolve("sampling_interval", getattr(args, "sampling_interval", None), 10)),
        sampling_top_n=int(defaults.resolve("sampling_top", getattr(args, "sampling_top", None), 10)),
        sampling_retention=int(getattr(args, "sampling_retention", 30) or 30),
        metrics_source=defaults.resolve("metrics_source", getattr(args, "metrics_source", None), "auto"),
        metrics_interval_ms=int(defaults.resolve("metrics_interval", getattr(args, "metrics_interval", None), 1000)),
        battery_interval_sec=int(defaults.resolve("battery_interval", getattr(args, "battery_interval", None), 10)),
        attach_webcontent=defaults.resolve_bool("attach_webcontent", getattr(args, "attach_webcontent", None), False),
        composite=defaults.resolve("composite", getattr(args, "composite", None), "auto"),
        binary_path=getattr(args, "binary", "") or "",
        linkmap_paths=list(getattr(args, "linkmap", []) or []),
        dsym_paths=list(getattr(args, "dsym", []) or []),
    )
    perf = PerfSessionManager(str(repo), ".claude-parallel", cfg)
    meta = perf.start()

    # 首次使用时自动保存关键参数
    defaults.update_from_args(args)
    print(json.dumps(meta, ensure_ascii=False, indent=2))


async def cmd_perf_stop(args):
    repo, tag, _ = _resolve_perf_repo_tag(args)
    if not repo:
        return
    cfg = PerfConfig(enabled=True, tag=tag)
    perf = PerfSessionManager(str(repo), ".claude-parallel", cfg)
    meta = perf.stop()
    print(json.dumps(meta, ensure_ascii=False, indent=2))

    # 询问/执行清理
    session_dir = Path(repo) / ".claude-parallel" / "perf" / tag
    _maybe_clean_session(
        session_dir,
        force_clean=getattr(args, "clean", False),
        force_keep=getattr(args, "no_clean", False),
        keep_report=getattr(args, "keep_report", False),
    )


def _format_size(bytes_val: int) -> str:
    """字节数转人类可读"""
    for unit in ("B", "KB", "MB", "GB"):
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} TB"


def _dir_size(path: Path) -> int:
    """递归计算目录字节数"""
    if not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _maybe_clean_session(
    session_dir: Path,
    force_clean: bool = False,
    force_keep: bool = False,
    keep_report: bool = False,
) -> None:
    """停止/结束分析后询问清理。

    force_clean: --clean → 直接清，不问
    force_keep:  --no-clean → 直接保留，不问
    其它情况：交互式询问 (非 TTY 默认保留以防误删)
    keep_report: 清理时保留 report.html/report.json，只删 logs/traces
    """
    import shutil
    if not session_dir.exists():
        return

    size = _dir_size(session_dir)
    size_human = _format_size(size)

    # 决定是否清理
    do_clean = False
    if force_clean and force_keep:
        print(f"\n  [clean] --clean 与 --no-clean 同时指定，按保留处理")
        do_clean = False
    elif force_clean:
        do_clean = True
    elif force_keep:
        do_clean = False
    else:
        # 交互模式
        if not sys.stdin.isatty():
            print(f"\n  [clean] 非交互环境，默认保留 session 数据 ({size_human})")
            print(f"  [clean] 路径: {session_dir}")
            print(f"  [clean] 如需清理请加 --clean")
            return
        print(f"\n  ┌──────────────────────────────────────────")
        print(f"  │  本次会话占用: {size_human}")
        print(f"  │  路径: {session_dir}")
        print(f"  └──────────────────────────────────────────")
        try:
            ans = input(
                "  ▸ 是否清理本次采集数据? [y]es / [N]o / [r] 仅清理 logs+traces 保留 report: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  [clean] 已取消，保留数据")
            return
        if ans in ("y", "yes"):
            do_clean = True
            keep_report = False
        elif ans in ("r", "report"):
            do_clean = True
            keep_report = True
        else:
            print(f"  [clean] 保留: {session_dir}")
            return

    # 执行清理
    if not do_clean:
        return
    if keep_report:
        # 只清 logs/ + traces/ + exports/，保留 report.html/.json + meta.json
        removed = 0
        for sub in ("logs", "traces", "exports"):
            sub_dir = session_dir / sub
            if sub_dir.exists():
                shutil.rmtree(sub_dir, ignore_errors=True)
                removed += 1
        new_size = _dir_size(session_dir)
        freed = size - new_size
        print(f"  ✓ 已清理 {removed} 个子目录 (logs/traces/exports)，释放 {_format_size(freed)}")
        print(f"  ✓ 保留: report.html / report.json / meta.json (共 {_format_size(new_size)})")
    else:
        shutil.rmtree(session_dir, ignore_errors=True)
        print(f"  ✓ 已清理 session: {session_dir}  (释放 {size_human})")


async def cmd_perf_tail(args):
    repo, tag, _ = _resolve_perf_repo_tag(args)
    if not repo:
        return
    cfg = PerfConfig(enabled=True, tag=tag)
    perf = PerfSessionManager(str(repo), ".claude-parallel", cfg)
    print(perf.tail_syslog(lines=args.lines))


async def cmd_perf_report(args):
    repo, tag, defaults = _resolve_perf_repo_tag(args)
    if not repo:
        return
    baseline = defaults.resolve("baseline", getattr(args, "baseline", None), "")
    threshold = float(defaults.resolve("threshold_pct", getattr(args, "threshold_pct", None), 0.0))
    cfg = PerfConfig(
        enabled=True,
        tag=tag,
        baseline_tag=baseline,
        threshold_pct=threshold,
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

    # HTML 报告生成
    if getattr(args, "html", False):
        from .perf.report_html import generate_html_report

        session_dir = perf.root
        meta_path = session_dir / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

        out_arg = getattr(args, "html_output", "")
        out_path = Path(out_arg) if out_arg else None

        html_path = generate_html_report(rep, meta, session_dir, output_path=out_path)
        print(f"  HTML report: {html_path}")

    # 报告生成完成，询问是否清理
    _maybe_clean_session(
        perf.root,
        force_clean=getattr(args, "clean", False),
        force_keep=getattr(args, "no_clean", False),
        keep_report=getattr(args, "keep_report", True),  # report 命令默认保留 report 文件
    )


# ── tunneld 守护 (iOS 17+ DVT 必需) ──────────────────────────────

TUNNELD_PID_FILE = Path.home() / ".cpar-tunneld.pid"
TUNNELD_LOG_FILE = Path.home() / ".cpar-tunneld.log"


def _tunneld_running() -> tuple[bool, int, bool]:
    """检查 tunneld 状态。

    返回 (process_alive, pid, devices_visible)
      process_alive: pgrep 找到 tunneld 进程
      devices_visible: 通过 API 真的能列出设备（即 tunneld 服务正常）
    """
    import subprocess
    pid = 0
    alive = False
    try:
        out = subprocess.run(
            ["pgrep", "-f", "pymobiledevice3 remote tunneld"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            pids = [int(p) for p in out.stdout.split() if p.isdigit()]
            # 取最年轻的 (sudo wrapper 通常较老)
            pid = pids[-1] if pids else 0
            alive = pid > 0
    except Exception:
        pass

    if not alive:
        return False, 0, False

    # API 验证: 真的能从 tunneld 拿到设备
    visible = False
    try:
        import asyncio as _a
        async def _check():
            try:
                from pymobiledevice3.tunneld.api import get_tunneld_devices, TUNNELD_DEFAULT_ADDRESS
                devs = await _a.wait_for(
                    get_tunneld_devices(TUNNELD_DEFAULT_ADDRESS), timeout=2.0
                )
                return len(devs) > 0
            except Exception:
                return False
        visible = _a.run(_check())
    except Exception:
        visible = False
    return alive, pid, visible


def _tunneld_start(prompt_password: bool = True) -> tuple[bool, str]:
    """启动 tunneld 守护进程。

    macOS 优先用 osascript 弹原生密码框，避免依赖 TTY。
    把 sudo 子进程的 PID 记录到 ~/.cpar-tunneld.pid。
    """
    import subprocess
    import shutil

    pmd3 = shutil.which("pymobiledevice3")
    if not pmd3:
        return False, "pymobiledevice3 未安装 (pip install pymobiledevice3)"

    # 1) 先试 sudo -n (无密码缓存或 sudoers NOPASSWD)
    try:
        proc = subprocess.Popen(
            ["sudo", "-n", pmd3, "remote", "tunneld"],
            stdout=open(TUNNELD_LOG_FILE, "a"),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        # 给它 2s 看是否能启动，未失败即 OK
        time.sleep(2)
        if proc.poll() is None:
            TUNNELD_PID_FILE.write_text(str(proc.pid))
            return True, f"已启动 (sudo 缓存有效) PID={proc.pid}"
        # 立即退出说明 sudo 要密码，下沉到 osascript
    except Exception:
        pass

    if not prompt_password:
        return False, "sudo 需密码且 --no-prompt 已禁用密码框"

    # 2) 用 osascript 弹密码框（macOS 原生）
    if sys.platform != "darwin":
        return False, "非 macOS 系统不支持 osascript 弹密码框"

    # osascript "do shell script" 默认走 sh -c，无需 /bin/sh 包裹
    # 用单引号包裹路径避免双引号嵌套解析错误
    cmd_inner = f"'{pmd3}' remote tunneld >> '{TUNNELD_LOG_FILE}' 2>&1 &"
    # AppleScript 字符串中需要把双引号转义为 \"
    cmd_inner_escaped = cmd_inner.replace('"', '\\"')
    osa = (
        f'do shell script "{cmd_inner_escaped}" '
        f'with administrator privileges'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", osa],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "用户长时间未输入密码 (osascript 超时)"
    except Exception as e:
        return False, f"osascript 调用失败: {e}"

    if result.returncode != 0:
        return False, f"osascript 失败: {result.stderr.strip()[:200]}"

    # 等 3 秒让 tunneld 起来
    time.sleep(3)
    alive, pid, visible = _tunneld_running()
    if alive:
        TUNNELD_PID_FILE.write_text(str(pid))
        return True, f"已启动 (osascript) PID={pid}, 可见设备={visible}"
    return False, "osascript 返回成功但 tunneld 进程未检测到"


def _tunneld_stop() -> tuple[bool, str]:
    """停止 tunneld。需要 sudo 权限。"""
    import subprocess
    alive, pid, _ = _tunneld_running()
    if not alive:
        TUNNELD_PID_FILE.unlink(missing_ok=True)
        return True, "tunneld 未运行"
    # tunneld 是 root 进程，需要 sudo kill
    try:
        result = subprocess.run(
            ["sudo", "-n", "pkill", "-f", "pymobiledevice3 remote tunneld"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            TUNNELD_PID_FILE.unlink(missing_ok=True)
            return True, "已停止"
    except Exception:
        pass

    # 降级 osascript
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["osascript", "-e",
                 'do shell script "pkill -f \'pymobiledevice3 remote tunneld\'" '
                 'with administrator privileges'],
                capture_output=True, timeout=60,
            )
            time.sleep(1)
            alive2, _, _ = _tunneld_running()
            if not alive2:
                TUNNELD_PID_FILE.unlink(missing_ok=True)
                return True, "已停止 (osascript)"
        except Exception as e:
            return False, f"osascript stop 失败: {e}"
    return False, f"无法停止 PID={pid}"


def cmd_perf_tunneld(args):
    """管理 tunneld。"""
    action = getattr(args, "action", "status") or "status"

    if action == "status":
        alive, pid, visible = _tunneld_running()
        if alive:
            print(f"  ✓ tunneld 运行中 PID={pid}")
            print(f"  ✓ 设备可见: {visible}")
            print(f"    日志: {TUNNELD_LOG_FILE}")
        else:
            print("  ✗ tunneld 未运行")
            print(f"    启动: cpar perf tunneld start")
        return

    if action == "ensure":
        alive, pid, visible = _tunneld_running()
        if alive and visible:
            print(f"  ✓ tunneld 已运行且设备可见 (PID={pid})")
            return
        if alive and not visible:
            print(f"  ⚠ tunneld 进程在但未发现设备，重启...")
            _tunneld_stop()
        # 落到 start 流程
        action = "start"

    if action == "start":
        alive, pid, _ = _tunneld_running()
        if alive:
            print(f"  · tunneld 已运行 PID={pid}，跳过启动")
            return
        prompt = not getattr(args, "no_prompt", False)
        print("  ▶ 正在启动 tunneld..." + (" (将弹出密码框)" if prompt else " (sudo 缓存)"))
        ok, msg = _tunneld_start(prompt_password=prompt)
        prefix = "  ✓" if ok else "  ✗"
        print(f"{prefix} {msg}")
        if ok:
            # 验证设备可见
            time.sleep(1)
            _, _, visible = _tunneld_running()
            print(f"    设备可见: {visible}")
        else:
            sys.exit(1)
        return

    if action == "stop":
        ok, msg = _tunneld_stop()
        print(("  ✓ " if ok else "  ✗ ") + msg)
        if not ok:
            sys.exit(1)
        return


def _ensure_tunneld_for_perf(args):
    """perf start 前自动确保 tunneld 可用 (iOS 17+ DVT 通道需要)。

    如果用户加 --no-tunneld 跳过；否则:
      已运行 → 跳过
      未运行 → 弹密码框启动 (osascript)
      启动失败 → 仅警告，不阻止 (battery + sampling 仍可工作)
    """
    if getattr(args, "no_tunneld", False):
        return
    alive, pid, visible = _tunneld_running()
    if alive and visible:
        print(f"  ✓ tunneld 已运行 PID={pid} (DVT 通道就绪)")
        return
    if alive and not visible:
        print(f"  ⚠ tunneld 进程在但未发现设备，重启...")
        _tunneld_stop()
        time.sleep(1)
    print("  ▶ tunneld 未运行 (iOS 17+ DVT 必需)，自动启动...")
    print("    📱 macOS 将弹出系统密码框，请输入登录密码")
    ok, msg = _tunneld_start(prompt_password=True)
    if ok:
        print(f"  ✓ tunneld {msg}")
    else:
        print(f"  ⚠ tunneld 启动失败: {msg}")
        print(f"  ⚠ DVT 通道 (GPU/Network/Process 进程指标) 不可用")
        print(f"  → battery + sampling 热点 仍会正常采集")
        print(f"  → 手动启动: cpar perf tunneld start")


def cmd_perf_linkmap(args):
    """LinkMap 解析与符号查询，转发到 src.perf.locate.linkmap.main()"""
    from src.perf.locate.linkmap import LinkMap, MultiLinkMap, find_linkmaps, CACHE_DIR
    from datetime import datetime
    import json as _json
    import time as _time

    action = getattr(args, "lm_action", "find")
    sub_args = getattr(args, "lm_args", []) or []

    if action == "warm":
        # 并发预热所有 LinkMap, 全部缓存
        print(f"  ▶ 扫描 DerivedData (project={args.project}, arch={args.arch})...")
        t0 = _time.perf_counter()
        files = find_linkmaps(project_name=args.project, arch=args.arch)
        if not files:
            print(f"  ✗ 未找到 LinkMap")
            print(f"  → 在 Xcode Build Settings 开启: Write Link Map File: Yes")
            return
        print(f"  发现 {len(files)} 个 LinkMap, 4 线程并发预热...")
        mlm = MultiLinkMap.warm_all_from_derived_data(
            project_name=args.project, arch=args.arch, max_workers=4,
        )
        elapsed = _time.perf_counter() - t0
        stats = mlm.stats()
        print(f"  ✓ 完成 ({elapsed:.2f}s)")
        print(f"    LinkMaps:        {stats['linkmaps']}")
        print(f"    总符号:          {stats['total_symbols']:,}")
        print(f"    OC 方法:         {stats['objc_symbols']:,}")
        print(f"    C++:             {stats['cpp_symbols']:,}")
        print(f"    Soul 业务前缀:   {stats['biz_symbols']:,}")
        # 缓存目录占用
        cache_size = sum(p.stat().st_size for p in CACHE_DIR.glob("*.pkl") if p.is_file())
        print(f"    缓存目录:        {CACHE_DIR}")
        print(f"    缓存占用:        {cache_size / 1024:.1f} KB")
        return

    if action == "bench":
        # 解析性能基准 (含/不含缓存对比)
        files = find_linkmaps(project_name=args.project, arch=args.arch)
        if not files:
            print(f"  ✗ 未找到 LinkMap")
            return
        # 取最大的一个测
        biggest = max(files, key=lambda f: f.stat().st_size)
        size_mb = biggest.stat().st_size / 1024 / 1024
        print(f"  benchmark 文件: {biggest.name}")
        print(f"    大小: {size_mb:.2f} MB")

        # 第一次 (无缓存)
        # 先清掉它的缓存
        cache_p = LinkMap._cache_path_for(str(biggest.resolve()))
        if cache_p.exists():
            cache_p.unlink()
        t0 = _time.perf_counter()
        lm = LinkMap.load(str(biggest), use_cache=True)
        first_load = _time.perf_counter() - t0
        symbols = len(lm.symbols)
        print(f"    首次解析:    {first_load*1000:.1f}ms ({symbols:,} 符号)")
        print(f"    解析速度:    {size_mb / first_load:.2f} MB/s")
        print(f"    每符号开销:  {first_load * 1e6 / max(symbols,1):.2f}μs")

        # 第二次 (命中缓存)
        t0 = _time.perf_counter()
        lm2 = LinkMap.load(str(biggest), use_cache=True)
        second_load = _time.perf_counter() - t0
        print(f"    命中缓存:    {second_load*1000:.2f}ms ({first_load/max(second_load, 0.0001):.0f}× 加速)")

        # 1000 次随机 lookup
        if symbols > 100:
            import random
            test_addrs = [random.choice(lm.symbols).addr for _ in range(1000)]
            t0 = _time.perf_counter()
            for a in test_addrs:
                lm.lookup(a)
            qps_dur = _time.perf_counter() - t0
            print(f"    1000 次 lookup: {qps_dur*1000:.2f}ms ({1000/qps_dur:.0f} QPS)")
        return

    if action == "find":
        files = find_linkmaps(project_name=args.project, arch=args.arch)
        if not files:
            print(f"  ✗ 未找到 LinkMap (project={args.project}, arch={args.arch})")
            print(f"  → 在 Xcode Build Settings 开启: Write Link Map File: Yes")
            return
        print(f"  共 {len(files)} 个 LinkMap (按 mtime 降序):")
        for f in files:
            mtime = f.stat().st_mtime
            size_mb = f.stat().st_size / 1024 / 1024
            print(f"    {datetime.fromtimestamp(mtime).strftime('%m-%d %H:%M')}  "
                  f"{size_mb:>6.2f}MB  {f}")
        return

    if not sub_args:
        print(f"  ✗ {action} 需要 LinkMap 文件路径作为第一个参数")
        sys.exit(1)
    file_path = sub_args[0]

    if action == "parse":
        lm = LinkMap.load(file_path)
        stats = lm.stats()
        if args.json:
            print(_json.dumps({**stats, "binary": lm.binary, "arch": lm.arch,
                               "parse_seconds": lm.parse_seconds}, indent=2))
        else:
            print(f"  Binary:        {lm.binary}")
            print(f"  Arch:          {lm.arch}")
            print(f"  Symbols:       {stats['total_symbols']:,}")
            print(f"    OC 方法:     {stats['objc_symbols']:,}")
            print(f"    C++:         {stats['cpp_symbols']:,}")
            print(f"    SO 业务前缀: {stats['biz_symbols']:,}")
            print(f"  Object files:  {stats['total_object_files']:,}")
            print(f"  Address range: 0x{stats['addr_min']:x} ~ 0x{stats['addr_max']:x}")
            print(f"  解析耗时:      {lm.parse_seconds*1000:.1f}ms"
                  f"{' (命中缓存)' if lm.parse_seconds == 0 else ''}")
        return

    if action == "lookup":
        if len(sub_args) < 2:
            print("  ✗ lookup 需要地址参数: cpar perf linkmap lookup <file> 0xADDR")
            sys.exit(1)
        lm = LinkMap.load(file_path)
        addr = int(sub_args[1], 0)
        sym = lm.lookup(addr)
        if sym:
            print(f"  地址:    0x{addr:x}")
            print(f"  符号:    {sym.name}")
            print(f"  范围:    0x{sym.addr:x} - 0x{sym.end_addr:x} (size={sym.size})")
            print(f"  Object:  {sym.file_path}")
            print(f"  内偏移:  +0x{addr - sym.addr:x}")
        else:
            print(f"  地址 0x{addr:x} 未找到对应符号")
        return

    if action == "search":
        if len(sub_args) < 2:
            print("  ✗ search 需要 pattern: cpar perf linkmap search <file> <pattern>")
            sys.exit(1)
        lm = LinkMap.load(file_path)
        results = lm.search_by_name(sub_args[1], max_results=args.max)
        print(f"  搜 '{sub_args[1]}' 命中 {len(results)} (上限 {args.max}):")
        for s in results:
            print(f"    0x{s.addr:>10x}  size=0x{s.size:>5x}  {s.name[:80]}")


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


async def cmd_perf_config(args):
    """查看/修改 perf 默认配置"""
    defaults = PerfDefaults.load()
    action = getattr(args, "config_action", "show")

    if action == "show" or not action:
        print(defaults.show())

    elif action == "set":
        field = args.field
        value = args.value
        try:
            defaults.set(field, value)
            print(f"  已设置: {field} = {value}")
        except KeyError as e:
            print(f"  错误: {e}")

    elif action == "unset":
        field = args.field
        defaults.unset(field)
        print(f"  已清除: {field}")

    else:
        print(f"  未知操作: {action}")
        print("  用法: cpar perf config [show|set|unset]")


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
            # 兼容历史变量名，避免后续维护误改导致 NameError
            current_snap_count = current_snaps
            latest = summary.get("latest")

            if current_snap_count > prev_snap_count and latest:
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
                prev_snap_count = current_snaps

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
    from src.perf.capture.live_metrics import build_snapshot_from_exports

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
    repo, tag, _ = _resolve_perf_repo_tag(args)
    if not repo:
        return
    cfg = PerfConfig(enabled=True, tag=tag)
    perf = PerfSessionManager(str(repo), ".claude-parallel", cfg)
    data = perf.callstack(
        top_n=args.top,
        min_weight=args.min_weight,
        flatten=not args.no_flatten,
        full_stack=getattr(args, "full_stack", False),
        time_from=getattr(args, "time_from", 0) or 0,
        time_to=getattr(args, "time_to", 0) or 0,
    )

    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        text = perf.format_callstack_text(data, max_depth=args.max_depth)
        print(text)


async def cmd_perf_metrics(args):
    """Per-process 指标查看"""
    from src.perf.protocol.device import read_process_metrics_jsonl, format_process_metrics_text

    repo, tag, _ = _resolve_perf_repo_tag(args)
    if not repo:
        return
    jsonl = repo / ".claude-parallel" / "perf" / tag / "logs" / "process_metrics.jsonl"

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
    from src.perf.protocol.device import read_battery_jsonl, format_battery_text

    repo, tag, _ = _resolve_perf_repo_tag(args)
    if not repo:
        return
    jsonl = repo / ".claude-parallel" / "perf" / tag / "logs" / "battery.jsonl"

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
    from src.perf.protocol.device import read_battery_jsonl

    repo, tag, _ = _resolve_perf_repo_tag(args)
    if not repo:
        return
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


async def cmd_perf_webcontent(args):
    """WebContent 进程热点查看"""
    from src.perf.capture.webcontent import read_webcontent_hotspots, format_webcontent_hotspots

    repo, tag, _ = _resolve_perf_repo_tag(args)
    if not repo:
        return
    hotspots_file = repo / ".claude-parallel" / "perf" / tag / "logs" / "webcontent_hotspots.jsonl"

    if not hotspots_file.exists():
        print(f"  [perf] 未找到 WebContent 热点: {hotspots_file}")
        print(f"  提示: 启动时加 --attach-webcontent")
        return

    last_n = getattr(args, "last", 0)
    snaps = read_webcontent_hotspots(hotspots_file, last_n=last_n)

    if getattr(args, "json", False):
        print(json.dumps(snaps, ensure_ascii=False, indent=2))
    else:
        text = format_webcontent_hotspots(snaps, top_n=args.top)
        print(text)


async def cmd_perf_hotspots(args):
    """运行时热点函数查看"""
    from src.perf.capture.sampling import read_hotspots_jsonl, format_hotspots_text

    repo, tag, _ = _resolve_perf_repo_tag(args)
    if not repo:
        return
    hotspots_file = repo / ".claude-parallel" / "perf" / tag / "logs" / "hotspots.jsonl"

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
    from src.perf.decode.templates import (
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


async def cmd_perf_symbolicate(args):
    """dSYM 符号化"""
    from pathlib import Path
    from src.perf.locate.dsym import (
        find_dsym, find_dsym_by_uuid, auto_symbolicate,
    )
    from src.perf.capture.sampling import read_hotspots_jsonl

    repo, tag, _ = _resolve_perf_repo_tag(args)
    if not repo:
        return
    logs_dir = repo / ".claude-parallel" / "perf" / tag / "logs"

    # 查找 dSYM
    dsym_path = args.dsym
    if not dsym_path and args.uuid:
        print(f"  [symbolicate] 通过 UUID 搜索: {args.uuid}")
        dsym_path = find_dsym_by_uuid(args.uuid)
        if dsym_path:
            print(f"  找到: {dsym_path}")
    if not dsym_path and args.app_id:
        print(f"  [symbolicate] 通过 Bundle ID 搜索: {args.app_id}")
        dsym_path = find_dsym(args.app_id)
        if dsym_path:
            print(f"  找到: {dsym_path}")

    if not dsym_path:
        print("  [symbolicate] 未找到 dSYM。请用 --dsym 或 --app-id 或 --uuid 指定")
        return

    # 读取热点
    hotspots_file = logs_dir / "hotspots.jsonl"
    if not hotspots_file.exists():
        print(f"  [symbolicate] 未找到热点数据: {hotspots_file}")
        print(f"  提示: 先运行 perf callstack 或 perf hotspots")
        return

    snapshots = read_hotspots_jsonl(hotspots_file)
    if not snapshots:
        print("  (无热点数据)")
        return

    # 符号化
    all_hotspots = []
    for snap in snapshots:
        all_hotspots.extend(snap.get("top", []))

    print(f"\n  符号化 {len(all_hotspots)} 个热点函数...")
    result = auto_symbolicate(
        hotspots=all_hotspots,
        dsym_paths=[dsym_path] if isinstance(dsym_path, str) else [str(dsym_path)],
        arch=args.arch,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for i, item in enumerate(result[:args.top], 1):
            sym = item.get("symbol", "?")
            if item.get("demangled"):
                sym = item["demangled"]
            pct = item.get("pct", 0)
            samples = item.get("samples", 0)
            bar = "█" * int(pct / 2)
            print(f"  {i:2d}. {sym[:70]:<70s} {pct:5.1f}% ({samples}) {bar}")


async def cmd_perf_time_sync(args):
    """syslog-xctrace 时序对齐"""
    from pathlib import Path
    from src.perf.decode.time_sync import (
        parse_syslog_timestamps, parse_xctrace_timeline,
        align_timelines, correlate_events, format_event_report,
        run_time_sync,
    )

    repo, tag, _ = _resolve_perf_repo_tag(args)
    if not repo:
        return
    session_dir = repo / ".claude-parallel" / "perf" / tag

    # 查找 syslog
    syslog_path = args.syslog
    if not syslog_path:
        for candidate in [
            session_dir / "logs" / "syslog.log",
            session_dir / "logs" / "full_syslog.log",
        ]:
            if candidate.exists():
                syslog_path = str(candidate)
                break

    if not syslog_path:
        print(f"  [time-sync] 未找到 syslog 文件")
        print(f"  提示: 用 --syslog 指定路径")
        return

    print(f"  [time-sync] syslog: {syslog_path}")
    print(f"  [time-sync] session: {session_dir}")

    result = run_time_sync(
        session_dir=str(session_dir),
        syslog_path=syslog_path,
        window_seconds=args.window,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        text = format_event_report(result)
        print(text)


async def cmd_perf_deep_export(args):
    """深度 Schema 采集"""
    from pathlib import Path
    from src.perf.decode.deep_export import (
        deep_export_all, format_deep_report, probe_trace_schemas,
    )

    repo, tag, _ = _resolve_perf_repo_tag(args)
    if not repo:
        return
    session_dir = repo / ".claude-parallel" / "perf" / tag
    traces_dir = session_dir / "traces"
    exports_dir = session_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)

    # 查找 trace 文件
    trace_files = list(traces_dir.glob("*.trace"))
    if not trace_files:
        print(f"  [deep-export] 未找到 trace 文件: {traces_dir}")
        return

    # 先探测可用 schema
    print(f"  [deep-export] 探测 {trace_files[0].name} 可用 schema...")
    available = probe_trace_schemas(trace_files[0])
    if available:
        print(f"  可用: {', '.join(available[:10])}{'...' if len(available) > 10 else ''}")
    else:
        print("  (探测失败，将尝试全部)")

    # 解析 schemas 参数
    schema_arg = args.schemas
    if schema_arg == "all":
        schemas = None
    else:
        schemas = [s.strip() for s in schema_arg.split(",")]

    print(f"\n  [deep-export] 批量导出...")
    data = deep_export_all(trace_files[0], exports_dir, schemas=schemas)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        for schema_name, schema_data in data.items():
            if schema_data:
                text = format_deep_report(schema_data, schema_name)
                print(text)
                print()
            else:
                print(f"  [{schema_name}] (无数据)\n")


async def cmd_perf_power_attr(args):
    """进程级功耗归因"""
    from pathlib import Path
    from src.perf.analyze.power_attribution import (
        parse_system_power, parse_process_cpu,
        attribute_power, format_attribution_report,
    )

    repo, tag, _ = _resolve_perf_repo_tag(args)
    if not repo:
        return
    session_dir = repo / ".claude-parallel" / "perf" / tag
    exports_dir = session_dir / "exports"
    logs_dir = session_dir / "logs"

    # 查找功耗数据
    power_xml = None
    for candidate in exports_dir.glob("*SystemPowerLevel*"):
        power_xml = candidate
        break

    if not power_xml:
        print("  [power-attr] 未找到 SystemPowerLevel 数据")
        print("  提示: 需要 Power Profiler trace + export")
        return

    # 查找进程 CPU 数据
    process_cpu_path = logs_dir / "process_metrics.jsonl"
    if not process_cpu_path.exists():
        # 尝试 time-profile XML
        for candidate in exports_dir.glob("*time-profile*"):
            process_cpu_path = candidate
            break

    print(f"  [power-attr] 功耗数据: {power_xml.name}")
    print(f"  [power-attr] 进程数据: {process_cpu_path.name if hasattr(process_cpu_path, 'name') else process_cpu_path}")

    power_samples = parse_system_power(power_xml)
    process_cpu = parse_process_cpu(process_cpu_path)

    if not power_samples:
        print("  (功耗数据解析失败)")
        return
    if not process_cpu:
        print("  (进程 CPU 数据解析失败)")
        return

    attribution = attribute_power(power_samples, process_cpu)

    if args.json:
        print(json.dumps(attribution, ensure_ascii=False, indent=2, default=str))
    else:
        text = format_attribution_report(attribution, anomalies=[], power_samples=power_samples)
        print(text)


async def cmd_perf_ai_diag(args):
    """AI 辅助诊断"""
    from pathlib import Path
    from src.perf.analyze.ai_diagnosis import (
        collect_diagnosis_context, build_diagnosis_prompt,
        call_llm, parse_diagnosis_response, format_diagnosis_report,
        run_diagnosis, generate_regression_analysis, generate_webkit_report,
    )

    repo, tag, _ = _resolve_perf_repo_tag(args)
    if not repo:
        return
    session_dir = repo / ".claude-parallel" / "perf" / tag

    print(f"  [ai-diag] 收集诊断上下文...")
    context = collect_diagnosis_context(str(session_dir))

    if args.offline:
        # 离线模式: 只输出 prompt
        prompt = build_diagnosis_prompt(context, focus_area=args.focus)
        print(f"\n{'='*60}")
        print(f"  AI 诊断 Prompt (离线模式)")
        print(f"{'='*60}\n")
        print(prompt)
        print(f"\n  提示: 设置 OPENAI_API_KEY 环境变量后可自动调用 LLM")
        return

    # 在线模式
    if args.focus == "webkit":
        print(f"  [ai-diag] 生成 WebKit 专项报告...")
        result = generate_webkit_report(str(session_dir))
        if result:
            text = format_diagnosis_report(result)
            print(text)
        else:
            print("  (WebKit 数据不足)")
        return

    if args.baseline_tag:
        # 回归分析
        baseline_dir = repo / ".claude-parallel" / "perf" / args.baseline_tag
        print(f"  [ai-diag] 回归分析: {args.baseline_tag} vs {tag}...")
        result = generate_regression_analysis(str(baseline_dir), str(session_dir))
        if result:
            text = format_diagnosis_report(result)
            print(text)
        return

    # 通用诊断
    print(f"  [ai-diag] 生成诊断 (focus: {args.focus})...")
    result = run_diagnosis(
        session_dir=str(session_dir),
        focus_area=args.focus,
        model=args.model or None,
    )

    if args.json:
        # 输出结构化 JSON
        out = {
            "problems": result.problems,
            "recommendations": result.recommendations,
            "priority": result.priority,
            "offline": result.offline,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        text = format_diagnosis_report(result)
        print(text)


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
