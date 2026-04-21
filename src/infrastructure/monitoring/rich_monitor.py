"""
实时进度监控面板 — 使用 Rich 库展示多 Claude Code Worker 的并行执行状态。

提供终端实时更新的 Rich 面板，包括总进度条、任务状态表、层级信息等。
在非 TTY 环境下自动降级为简单 print 输出。
"""

import threading
import time
from typing import TYPE_CHECKING, Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from ...orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# 状态图标映射
# ---------------------------------------------------------------------------
STATUS_ICONS: dict[str, str] = {
    "pending":   "⚪",
    "running":   "🔵",
    "done":      "✅",
    "failed":    "❌",
    "cancelled": "⚠️",
    "retrying":  "🔄",
}

STATUS_COLORS: dict[str, str] = {
    "pending":   "dim",
    "running":   "cyan",
    "done":      "green",
    "failed":    "red",
    "cancelled": "yellow",
    "retrying":  "magenta",
}


def _format_duration(seconds: float) -> str:
    """将秒数格式化为可读的时间字符串。"""
    if seconds < 0:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {secs}s"


def _format_cost(cost_usd: float) -> str:
    """将美元成本格式化为可读字符串。"""
    if cost_usd < 0.01:
        return "$0.00"
    return f"${cost_usd:.2f}"


class Monitor:
    """
    实时进度监控面板。

    通过引用 Orchestrator 读取实时状态，使用 Rich Live 在终端
    渲染动态更新的面板。后台线程驱动刷新，Orchestrator 通过
    ``update()`` 触发即时刷新。

    用法::

        monitor = Monitor(orchestrator)
        monitor.start()
        # ... orchestrator 运行 ...
        monitor.stop()

    Parameters
    ----------
    orchestrator : Orchestrator
        调度器实例，用于读取 tasks / workers / results / stats 等状态。
    refresh_interval : float
        非 TTY 模式下的轮询间隔（秒）。默认 2.0。
    """

    def __init__(self, orchestrator: "Orchestrator", refresh_interval: float = 0.5) -> None:
        self._orch = orchestrator
        self._refresh_interval = refresh_interval
        self._console = Console()
        self._is_tty = self._console.is_terminal
        self._live: Optional[Live] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._update_event = threading.Event()
        self._current_level_idx: int = -1
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        启动监控面板。

        在 TTY 环境下启动后台线程运行 Rich Live 显示；
        在非 TTY 环境下启动后台线程定期 print 状态摘要。
        """
        if self._thread is not None and self._thread.is_alive():
            return  # 已在运行

        self._stop_event.clear()
        self._update_event.clear()

        if self._is_tty:
            self._thread = threading.Thread(
                target=self._run_live_display,
                name="monitor-live",
                daemon=True,
            )
        else:
            self._thread = threading.Thread(
                target=self._run_fallback_display,
                name="monitor-fallback",
                daemon=True,
            )
        self._thread.start()

    def stop(self) -> None:
        """
        停止监控面板，等待后台线程结束。
        """
        self._stop_event.set()
        self._update_event.set()  # 唤醒可能正在等待的线程
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._thread = None
        self._live = None

    def update(self) -> None:
        """
        由 Orchestrator 在状态变更时调用，触发面板即时刷新。
        """
        self._update_event.set()

    def set_current_level(self, level_idx: int) -> None:
        """
        设置当前正在执行的层级索引。

        Parameters
        ----------
        level_idx : int
            当前层级索引（从 0 开始）。
        """
        with self._lock:
            self._current_level_idx = level_idx
        self._update_event.set()

    # ------------------------------------------------------------------
    # Rich Live 显示 (TTY)
    # ------------------------------------------------------------------

    def _run_live_display(self) -> None:
        """在后台线程中运行 Rich Live 显示。"""
        try:
            with Live(
                console=self._console,
                refresh_per_second=4,
                screen=True,
                vertical_overflow="visible",
            ) as live:
                self._live = live
                while not self._stop_event.is_set():
                    # 等待更新事件或超时刷新
                    self._update_event.wait(timeout=self._refresh_interval)
                    self._update_event.clear()
                    live.update(self._build_layout())
        except Exception as exc:
            # 静默退出，不中断主流程
            pass
        finally:
            self._live = None

    # ------------------------------------------------------------------
    # 非 TTY fallback
    # ------------------------------------------------------------------

    def _run_fallback_display(self) -> None:
        """非 TTY 环境：定期 print 简单文本摘要。"""
        while not self._stop_event.is_set():
            self._update_event.wait(timeout=self._refresh_interval)
            self._update_event.clear()
            self._print_fallback()

    def _print_fallback(self) -> None:
        """输出简单文本状态摘要（非 TTY 环境）。"""
        stats = self._orch.stats
        elapsed = time.time() - stats.start_time if (stats.start_time or 0) > 0 else 0.0
        active = sum(1 for w in self._orch.workers.values() if w.is_running)

        lines = [
            f"[Monitor] "
            f"Progress: {stats.completed + stats.failed}/{stats.total_tasks} | "
            f"Done: {stats.completed} | Failed: {stats.failed} | "
            f"Skipped: {stats.skipped} | "
            f"Cost: {_format_cost(stats.total_cost_usd)} | "
            f"Elapsed: {_format_duration(elapsed)} | "
            f"Active workers: {active}",
        ]

        for task in self._orch.tasks:
            icon = STATUS_ICONS.get(task.status, "?")
            worker = self._orch.workers.get(task.id)
            result = self._orch.results.get(task.id)

            if result:
                detail = (
                    f"cost={_format_cost(result.cost_usd)} "
                    f"time={_format_duration(result.duration_s)} "
                    f"turns={result.num_turns}"
                )
            elif worker and worker.is_running:
                detail = f"running {_format_duration(worker.elapsed)}"
            else:
                detail = ""

            lines.append(f"  {icon} {task.id}: {task.status} {detail}")

        print("\n".join(lines), flush=True)

    # ------------------------------------------------------------------
    # 布局构建
    # ------------------------------------------------------------------

    def _build_layout(self) -> Layout:
        """
        构建完整的 Rich Layout。

        布局:
          ┌─────────────────────────────────┐
          │  顶部: 项目信息 + 总进度条       │
          ├─────────────────────────────────┤
          │  中间: 任务状态表                │
          ├─────────────────────────────────┤
          │  底部: 层级信息 + 活跃 Worker    │
          └─────────────────────────────────┘
        """
        layout = Layout()
        layout.split(
            Layout(self._build_header(), name="header", size=5),
            Layout(self._build_task_table(), name="tasks", minimum_size=6),
            Layout(self._build_footer(), name="footer", size=3),
        )
        return layout

    # -- 顶部面板 --------------------------------------------------------

    def _build_header(self) -> Panel:
        """构建顶部面板：项目名、总进度条、总成本、总耗时。"""
        stats = self._orch.stats
        config = self._orch.config

        # 计算进度
        finished = stats.completed + stats.failed + stats.skipped
        total = stats.total_tasks
        elapsed = time.time() - stats.start_time if (stats.start_time or 0) > 0 else 0.0

        # 构建 Progress
        progress = Progress(
            TextColumn("[bold blue]{task.description}", justify="right"),
            BarColumn(bar_width=40),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("({task.completed}/{task.total})"),
            console=self._console,
            expand=False,
        )
        task_id = progress.add_task("Progress", total=total, completed=finished)

        # 生成 Renderable
        grid = Table.grid(padding=(0, 2))
        grid.add_column()
        grid.add_column()
        grid.add_row(
            Text(f"Project: {config.repo if config else 'N/A'}", style="bold white"),
            Text(
                f"Cost: {_format_cost(stats.total_cost_usd)}  |  "
                f"Elapsed: {_format_duration(elapsed)}",
                style="bold yellow",
            ),
        )
        grid.add_row(progress)

        return Panel(
            grid,
            title="[bold cyan]Claude Parallel[/]",
            border_style="cyan",
            padding=(0, 1),
        )

    # -- 中间任务表 -------------------------------------------------------

    def _build_task_table(self) -> Panel:
        """构建中间任务状态表。"""
        table = Table(
            title="",
            show_header=True,
            header_style="bold magenta",
            border_style="blue",
            expand=True,
            padding=(0, 1),
        )
        table.add_column("ID", style="bold", min_width=12, max_width=24)
        table.add_column("Status", min_width=10, max_width=14)
        table.add_column("Elapsed", justify="right", min_width=8, max_width=10)
        table.add_column("Cost", justify="right", min_width=7, max_width=10)
        table.add_column("Turns", justify="right", min_width=5, max_width=6)
        table.add_column("Model", min_width=10, max_width=24)
        table.add_column("Description", min_width=10)

        for task in self._orch.tasks:
            icon = STATUS_ICONS.get(task.status, "❓")
            color = STATUS_COLORS.get(task.status, "white")
            status_text = f"[{color}]{icon} {task.status}[/]"

            # 获取 worker / result 的详细信息
            worker = self._orch.workers.get(task.id)
            result = self._orch.results.get(task.id)

            if result:
                elapsed_str = _format_duration(result.duration_s)
                cost_str = _format_cost(result.cost_usd)
                turns_str = str(result.num_turns)
                model_str = result.model_used or "-"
            elif worker and worker.is_running:
                elapsed_str = _format_duration(worker.elapsed)
                cost_str = "..."
                turns_str = "..."
                model_str = task.model or "..."
            else:
                elapsed_str = "-"
                cost_str = "-"
                turns_str = "-"
                model_str = task.model or "-"

            # 描述截断
            desc = task.description[:60] + ("..." if len(task.description) > 60 else "")

            table.add_row(
                task.id,
                status_text,
                elapsed_str,
                cost_str,
                turns_str,
                model_str,
                f"[dim]{desc}[/]",
            )

        return Panel(table, border_style="blue", padding=(0, 0))

    # -- 底部面板 ---------------------------------------------------------

    def _build_footer(self) -> Panel:
        """构建底部面板：当前层级信息、活跃 Worker 数。"""
        with self._lock:
            level_idx = self._current_level_idx

        levels = self._orch.levels
        active_workers = sum(1 for w in self._orch.workers.values() if w.is_running)
        max_workers = self._orch.config.max_workers if self._orch.config else 0

        # 层级信息
        if level_idx >= 0 and level_idx < len(levels):
            level = levels[level_idx]
            level_tasks = ", ".join(t.id for t in level)
            level_info = (
                f"[bold]Level {level_idx}/{len(levels) - 1}[/]  "
                f"({len(level)} tasks: {level_tasks})"
            )
        elif len(levels) > 0:
            level_info = f"[dim]Levels: {len(levels)} — waiting to start[/]"
        else:
            level_info = "[dim]No tasks[/]"

        # 活跃 Worker
        worker_info = (
            f"[bold cyan]Active Workers: {active_workers}/{max_workers}[/]"
        )

        grid = Table.grid(padding=(0, 3))
        grid.add_column()
        grid.add_column()
        grid.add_row(level_info, worker_info)

        return Panel(grid, border_style="dim", padding=(0, 1))
