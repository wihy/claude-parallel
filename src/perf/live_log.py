"""
LiveLogAnalyzer — 实时 syslog 流式分析引擎。

功能:
- 通过 idevicesyslog 实时读取日志流
- 正则规则引擎匹配关键字
- 告警触发: 自动 mark_event / 写告警日志 / 超阈值通知
- 规则可通过 YAML 文件自定义
- 内置常用 iOS 功耗/性能告警规则
"""

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, List, Any, Callable

from .reconnect import ReconnectableMixin, ReconnectPolicy

logger = logging.getLogger(__name__)


# ── 规则定义 ──

@dataclass
class LogRule:
    """单条 syslog 匹配规则"""
    name: str                           # 规则名 (唯一标识)
    pattern: str                        # 正则表达式 (大小写不敏感)
    level: str = "warn"                 # 告警级别: info / warn / error / critical
    description: str = ""               # 规则说明
    max_hits: int = 0                   # 告警阈值 (0=每次匹配都告警)
    window_sec: float = 0               # 滑动窗口 (秒), 0=不限
    mark_event: bool = True             # 是否自动 mark_event
    throttle_sec: float = 5.0           # 同规则两次告警的最小间隔 (秒)

    # 运行时状态 (不序列化)
    _hits: int = field(default=0, init=False, repr=False)
    _last_alert_ts: float = field(default=0.0, init=False, repr=False)
    _window_start: float = field(default=0.0, init=False, repr=False)
    _compiled: Optional[re.Pattern] = field(default=None, init=False, repr=False)

    def compile(self) -> re.Pattern:
        if self._compiled is None:
            self._compiled = re.compile(self.pattern, re.IGNORECASE)
        return self._compiled

    def check(self, line: str) -> Optional[Dict[str, Any]]:
        """检查一行日志, 返回告警 dict 或 None"""
        pat = self.compile()
        m = pat.search(line)
        if not m:
            return None

        now = time.time()

        # 窗口计数
        if self.window_sec > 0:
            if now - self._window_start > self.window_sec:
                self._hits = 0
                self._window_start = now
        self._hits += 1

        # 阈值检查
        if self.max_hits > 0 and self._hits < self.max_hits:
            return None

        # 节流
        if now - self._last_alert_ts < self.throttle_sec:
            return None

        self._last_alert_ts = now
        return {
            "rule": self.name,
            "level": self.level,
            "description": self.description,
            "pattern": self.pattern,
            "match": m.group(0)[:200],
            "hits": self._hits,
            "ts": now,
        }

    def reset(self):
        self._hits = 0
        self._last_alert_ts = 0.0
        self._window_start = 0.0


# ── 内置规则集 ──

DEFAULT_RULES: List[LogRule] = [
    # 内存相关
    LogRule(
        name="jetsam_kill",
        pattern=r"jetsam.*kill|kill.*jetsam|low memory|OOM|out of memory",
        level="critical",
        description="进程被 Jetsam 杀死或内存不足",
        max_hits=1,
        mark_event=True,
    ),
    LogRule(
        name="memory_pressure",
        pattern=r"memory pressure|memstatus|vm_compressor",
        level="warn",
        description="内存压力事件",
        max_hits=3,
        window_sec=60,
        mark_event=True,
    ),

    # 温度/功耗
    LogRule(
        name="thermal_warning",
        pattern=r"thermal(?:pressure)?(?:warning|mitigation|throttle|tripped)",
        level="error",
        description="热管理告警 — 设备过热降频",
        max_hits=1,
        mark_event=True,
    ),
    LogRule(
        name="thermal_pressure",
        pattern=r"thermalPressureLevel\s*=\s*(?:critical|serious|nominal)",
        level="warn",
        description="热压力等级变化",
        max_hits=2,
        window_sec=60,
        mark_event=True,
    ),

    # WebKit 相关
    LogRule(
        name="webkit_crash",
        pattern=r"WebKit.*crash|WebProcess.*exit|WebProcess.*crash|WKWebView.*crash",
        level="critical",
        description="WebKit 进程崩溃",
        max_hits=1,
        mark_event=True,
    ),
    LogRule(
        name="webkit_oom",
        pattern=r"WebProcess.*jetsam|WebKit.*OOM|WebContent.*killed",
        level="critical",
        description="WebKit 进程因内存被杀",
        max_hits=1,
        mark_event=True,
    ),
    LogRule(
        name="webkit_network",
        pattern=r"WebKit.*network.*error|WKWebView.*load.*fail|NSURLError",
        level="warn",
        description="WebKit 网络加载失败",
        max_hits=5,
        window_sec=60,
        throttle_sec=10,
        mark_event=True,
    ),

    # 生命周期
    LogRule(
        name="app_background",
        pattern=r"(?:applicationDidEnterBackground|sceneDidEnterBackground|willResignActive)",
        level="info",
        description="App 进入后台",
        max_hits=0,
        throttle_sec=30,
        mark_event=True,
    ),
    LogRule(
        name="app_foreground",
        pattern=r"(?:applicationWillEnterForeground|sceneWillEnterForeground|didBecomeActive)",
        level="info",
        description="App 回到前台",
        max_hits=0,
        throttle_sec=30,
        mark_event=True,
    ),
    LogRule(
        name="app_suspend",
        pattern=r"(?:applicationWillSuspend|taskSuspending|background_task_expired)",
        level="warn",
        description="App 被挂起/后台任务过期",
        max_hits=1,
        mark_event=True,
    ),

    # 崩溃
    LogRule(
        name="app_crash",
        pattern=r"(?:SpringBoard.*crash|ReportCrash|assertion.*failed.*terminate|SIGABRT|SIGSEGV|SIGKILL)",
        level="critical",
        description="App 崩溃或异常退出",
        max_hits=1,
        mark_event=True,
    ),

    # GPU / 渲染
    LogRule(
        name="gpu_timeout",
        pattern=r"GPU.*timeout|Metal.*timeout|CA::Render::.*timeout|Rendering.*stall",
        level="error",
        description="GPU 渲染超时",
        max_hits=2,
        window_sec=60,
        mark_event=True,
    ),
    LogRule(
        name="frame_drop",
        pattern=r"(?:frame.*drop|dropped frame|CA::Transaction.*commit.*slow|Rendering.*slow)",
        level="warn",
        description="帧率下降 / 渲染慢",
        max_hits=10,
        window_sec=60,
        throttle_sec=15,
        mark_event=True,
    ),
]


# ── 分析器 ──

class LiveLogAnalyzer(ReconnectableMixin):
    """
    实时 syslog 流式分析器 (带自动重连)。

    工作方式:
    1. 启动 idevicesyslog 进程
    2. 逐行读取 stdout, 对每行应用所有规则
    3. 命中规则时触发回调 + 写告警日志 + 可选 mark_event
    4. 后台线程运行, 主线程可随时查询告警统计
    5. idevicesyslog 进程退出时自动重连 (指数退避)
    """

    def __init__(
        self,
        device: str = "",
        rules: Optional[List[LogRule]] = None,
        alert_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        alert_log_path: Optional[str] = None,
        perf_manager=None,  # Optional[PerfSessionManager]
        buffer_lines: int = 200,
        reconnect_policy: Optional[ReconnectPolicy] = None,
    ):
        # 初始化重连 mixin
        policy = reconnect_policy or ReconnectPolicy(
            max_retries=20,
            initial_delay_sec=2.0,
            max_delay_sec=30.0,
            backoff_factor=2.0,
        )
        super().__init__(
            policy=policy,
            stop_event=threading.Event(),
            on_disconnect=self._on_syslog_disconnect,
            on_reconnect=self._on_syslog_reconnect,
        )

        self.device = device
        self.rules = rules if rules is not None else [copy.deepcopy(r) for r in DEFAULT_RULES]
        self.alert_callback = alert_callback
        self.alert_log_path = Path(alert_log_path) if alert_log_path else None
        self.perf_manager = perf_manager
        self.buffer_lines = buffer_lines

        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._alerts: List[Dict[str, Any]] = []
        self._lines_processed = 0
        self._start_time: float = 0.0
        self._alert_counts: Dict[str, int] = {}  # rule_name -> count
        self._line_buffer: List[str] = []         # 最近 N 行原始日志
        self._lock = threading.Lock()

    # ── 生命周期 ──

    def start(self) -> Dict[str, Any]:
        """启动实时 syslog 分析"""
        if self._running.is_set():
            return {"status": "already_running"}

        # 同步 stop_event 给 mixin
        self._stop_event = self._running

        if not self._spawn_syslog_process():
            return {"status": "error", "error": "idevicesyslog spawn failed (see logs)"}

        self._running.set()
        self._start_time = time.time()

        # 初始化告警日志
        if self.alert_log_path:
            self.alert_log_path.parent.mkdir(parents=True, exist_ok=True)

        # 重置重连计数
        self._reconnect_stats.current_retry = 0

        # 后台线程
        self._thread = threading.Thread(target=self._reconnectable_reader_loop, daemon=True)
        self._thread.start()

        return {
            "status": "running",
            "pid": self._process.pid,
            "device": self.device or "auto",
            "rules_count": len(self.rules),
            "rules": [r.name for r in self.rules],
            "reconnect_enabled": True,
        }

    def stop(self) -> Dict[str, Any]:
        """停止分析"""
        if not self._running.is_set():
            return {"status": "not_running"}

        self._running.clear()

        if self._process and self._process.poll() is None:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception as e:
                logger.debug("进程 terminate 失败: %s", e)
                try:
                    self._process.kill()
                except Exception as e:
                    logger.debug("进程 kill 失败: %s", e)

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

        summary = self.get_summary()
        summary["status"] = "stopped"
        return summary

    def is_running(self) -> bool:
        return self._running.is_set()

    # ── 查询 ──

    def get_summary(self) -> Dict[str, Any]:
        """获取当前分析摘要"""
        with self._lock:
            duration = time.time() - self._start_time if self._start_time else 0
            summary = {
                "status": "running" if self._running.is_set() else "stopped",
                "device": self.device or "auto",
                "duration_sec": round(duration, 1),
                "lines_processed": self._lines_processed,
                "total_alerts": len(self._alerts),
                "alert_counts": dict(self._alert_counts),
                "recent_alerts": self._alerts[-10:],
                "rules_count": len(self.rules),
                "line_buffer_size": len(self._line_buffer),
            }
        # 加入重连统计
        summary["reconnect"] = self.get_reconnect_stats()
        return summary

    def get_alerts(self, level: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        """获取告警列表"""
        with self._lock:
            alerts = list(self._alerts)
        if level:
            alerts = [a for a in alerts if a.get("level") == level]
        return alerts[-limit:]

    def get_recent_lines(self, lines: int = 50) -> List[str]:
        """获取最近 N 行原始日志"""
        with self._lock:
            return list(self._line_buffer[-lines:])

    def get_alert_counts_by_level(self) -> Dict[str, int]:
        """按级别统计告警数"""
        with self._lock:
            counts = {"critical": 0, "error": 0, "warn": 0, "info": 0}
            for a in self._alerts:
                lvl = a.get("level", "info")
                counts[lvl] = counts.get(lvl, 0) + 1
            return counts

    def has_critical_alerts(self) -> bool:
        with self._lock:
            return any(a.get("level") == "critical" for a in self._alerts)

    # ── 规则管理 ──

    def add_rule(self, rule: LogRule):
        self.rules.append(rule)

    def remove_rule(self, name: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r.name != name]
        return len(self.rules) < before

    def list_rules(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": r.name,
                "pattern": r.pattern,
                "level": r.level,
                "description": r.description,
                "max_hits": r.max_hits,
                "window_sec": r.window_sec,
            }
            for r in self.rules
        ]

    @classmethod
    def load_rules_from_file(cls, path: str) -> List[LogRule]:
        """从 YAML/JSON 文件加载自定义规则"""
        p = Path(path)
        if not p.exists():
            return []

        text = p.read_text(errors="replace")
        if p.suffix in (".yaml", ".yml"):
            try:
                import yaml
                data = yaml.safe_load(text)
            except ImportError:
                # 退回到简单 JSON-like 解析
                data = json.loads(text)
        else:
            data = json.loads(text)

        rules = []
        for item in data.get("rules", []):
            rules.append(LogRule(
                name=item.get("name", "unnamed"),
                pattern=item.get("pattern", ""),
                level=item.get("level", "warn"),
                description=item.get("description", ""),
                max_hits=item.get("max_hits", 0),
                window_sec=item.get("window_sec", 0),
                mark_event=item.get("mark_event", True),
                throttle_sec=item.get("throttle_sec", 5.0),
            ))
        return rules

    # ── 内部 ──

    def _spawn_syslog_process(self) -> bool:
        """启动/重新启动 idevicesyslog 子进程。返回是否成功。"""
        cmd = ["idevicesyslog"]
        if self.device:
            cmd.extend(["-u", self.device])

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,  # 行缓冲
                text=True,
                errors="replace",
            )
            return True
        except FileNotFoundError:
            self._log_error("idevicesyslog not found. Install: brew install libimobiledevice")
            return False
        except Exception as e:
            self._log_error(f"idevicesyslog spawn failed: {e!r}")
            return False

    def _on_syslog_disconnect(self, reason: str):
        """重连 mixin 回调: 断连时记录到告警日志。"""
        self._log_error(f"syslog 断连: {reason}")
        if self.perf_manager:
            try:
                self.perf_manager.mark_event(
                    "syslog_disconnect",
                    detail=f"idevicesyslog disconnected: {reason}",
                )
            except Exception as e:
                logger.debug("mark_event disconnect 失败: %s", e)

    def _on_syslog_reconnect(self):
        """重连 mixin 回调: 重连成功时记录事件。"""
        self._log_error(f"syslog 重连成功 (PID={self._process.pid if self._process else '?'})")
        if self.perf_manager:
            try:
                self.perf_manager.mark_event(
                    "syslog_reconnect",
                    detail=f"idevicesyslog reconnected (PID={self._process.pid})",
                )
            except Exception as e:
                logger.debug("mark_event reconnect 失败: %s", e)

    def _reconnectable_reader_loop(self):
        """外层循环: 断连时自动重连。"""
        while self._running.is_set():
            # 内层读取循环
            exited_normally = self._inner_reader_loop()

            # 如果是主动停止或 EOF 不需要重连
            if not self._running.is_set():
                break

            if exited_normally:
                # 进程正常退出 (可能设备断开)，尝试重连
                self._handle_disconnect("idevicesyslog process exited")

            if not self._should_retry():
                self._mark_reconnect_failed()
                break

            # 退避等待
            delay = self._get_backoff_delay()
            self._log_error(
                f"syslog 重连等待 {delay:.1f}s "
                f"(retry={self._reconnect_stats.current_retry})"
            )
            if not self._reconnect_sleep(delay):
                break  # 被 stop 打断

            # 尝试重连
            if self._spawn_syslog_process():
                self._mark_reconnected()
            else:
                # spawn 失败，继续下一轮退避
                self._handle_disconnect("idevicesyslog spawn failed")
                if not self._should_retry():
                    self._mark_reconnect_failed()
                    break

        self._running.clear()

    def _inner_reader_loop(self) -> bool:
        """内层循环: 逐行读取 syslog 并分析。

        Returns:
            True 表示进程退出 (非异常)，需要重连
            False 表示读取异常或被 stop 打断
        """
        try:
            while self._running.is_set() and self._process and self._process.poll() is None:
                try:
                    line = self._process.stdout.readline()
                except Exception as e:
                    self._log_error(f"syslog readline 失败: {e!r}")
                    return True  # 尝试重连

                if not line:
                    # poll() 检查进程是否真的退出了
                    if self._process.poll() is not None:
                        return True  # 进程退出，触发重连
                    time.sleep(0.1)
                    continue

                line = line.rstrip("\n\r")
                if not line:
                    continue

                try:
                    with self._lock:
                        self._lines_processed += 1
                        self._line_buffer.append(line)
                        if len(self._line_buffer) > self.buffer_lines:
                            self._line_buffer = self._line_buffer[-self.buffer_lines:]

                    self._analyze_line(line)
                except Exception as e:
                    self._log_error(f"_analyze_line 异常: {e!r}")

            return True  # 正常退出循环 (进程退出)

        except Exception as e:
            if self._running.is_set():
                self._log_error(f"_inner_reader_loop 异常: {e!r}")
            return False

    def _log_error(self, msg: str):
        """把后台线程异常写到 alert log 旁边的 errors.log，避免静默。"""
        if not self.alert_log_path:
            return
        err_path = self.alert_log_path.parent / "live_log.errors.log"
        try:
            ts = time.strftime("%H:%M:%S")
            err_path.parent.mkdir(parents=True, exist_ok=True)
            with open(err_path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception as e:
            logger.debug("错误日志写入失败: %s", e)

    def _analyze_line(self, line: str):
        """对一行日志应用所有规则"""
        for rule in self.rules:
            alert = rule.check(line)
            if alert is None:
                continue

            # 补充信息
            alert["line_preview"] = line[:300]

            with self._lock:
                self._alerts.append(alert)
                self._alert_counts[rule.name] = self._alert_counts.get(rule.name, 0) + 1

            # 写告警日志
            self._write_alert_log(alert)

            # 自动 mark_event
            if rule.mark_event and self.perf_manager:
                try:
                    self.perf_manager.mark_event(
                        f"alert_{rule.name}",
                        detail=f"[{rule.level.upper()}] {rule.description}: {alert['match'][:100]}",
                    )
                except Exception as e:
                    logger.debug("mark_event alert 失败: %s", e)

            # 回调
            if self.alert_callback:
                try:
                    self.alert_callback(alert)
                except Exception as e:
                    logger.debug("alert_callback 执行异常: %s", e, exc_info=True)

    def _write_alert_log(self, alert: Dict[str, Any]):
        if not self.alert_log_path:
            return
        try:
            ts = time.strftime("%H:%M:%S", time.localtime(alert["ts"]))
            line = f"[{ts}] [{alert['level'].upper()}] {alert['rule']}: {alert['match'][:150]}\n"
            with open(self.alert_log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            logger.debug("告警日志写入失败: %s", e)
