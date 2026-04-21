"""
ReconnectableMixin — 子进程断连自动重连通用 mixin。

为所有基于子进程采集的组件提供统一的重连策略:
- 指数退避 + 随机抖动
- 最大重试次数 / 最大退避时间
- 重连事件回调 (on_disconnect / on_reconnect)
- 可中断的 sleep (响应 stop 信号)
"""

import logging
import random
import time
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, Dict, Any

logger = logging.getLogger(__name__)


@dataclass
class ReconnectPolicy:
    """重连策略配置"""
    max_retries: int = 20          # 最大重试次数 (0=不重试, -1=无限)
    initial_delay_sec: float = 2.0 # 首次重连等待
    max_delay_sec: float = 30.0    # 最大等待时间
    backoff_factor: float = 2.0    # 退避倍数
    jitter: float = 0.3           # 抖动系数 (0~1)
    retry_on_errors: tuple = ()    # 仅对特定异常重试 (空=全部)


@dataclass
class ReconnectStats:
    """重连统计"""
    total_disconnects: int = 0
    total_reconnects: int = 0
    total_failed_retries: int = 0
    current_retry: int = 0
    last_disconnect_ts: float = 0.0
    last_reconnect_ts: float = 0.0
    history: list = field(default_factory=list)  # [(ts, event, detail), ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_disconnects": self.total_disconnects,
            "total_reconnects": self.total_reconnects,
            "total_failed_retries": self.total_failed_retries,
            "current_retry": self.current_retry,
            "last_disconnect_ts": self.last_disconnect_ts,
            "last_reconnect_ts": self.last_reconnect_ts,
            "recent_history": self.history[-20:],
        }


class ReconnectableMixin:
    """
    子进程断连自动重连 mixin。

    使用方式:
        class MyCollector(ReconnectableMixin):
            def _spawn_process(self):
                ... return subprocess.Popen(...)
            def _is_process_alive(self) -> bool:
                return self._process and self._process.poll() is None

        # 检测断连并自动重连
        if not self._is_process_alive():
            self._handle_disconnect("idevicesyslog exited")
            if self._should_retry():
                self._spawn_process()
    """

    def __init_reconnect__(
        self,
        policy: Optional[ReconnectPolicy] = None,
        stop_event: Optional[threading.Event] = None,
        on_disconnect: Optional[Callable[[str], None]] = None,
        on_reconnect: Optional[Callable[[], None]] = None,
        on_reconnect_failed: Optional[Callable[[], None]] = None,
    ):
        self._reconnect_policy = policy or ReconnectPolicy()
        self._reconnect_stats = ReconnectStats()
        self._stop_event = stop_event or threading.Event()
        self._on_disconnect = on_disconnect
        self._on_reconnect = on_reconnect
        self._on_reconnect_failed = on_reconnect_failed

    def _handle_disconnect(self, reason: str = "") -> None:
        """记录断连事件并通知。"""
        self._reconnect_stats.total_disconnects += 1
        self._reconnect_stats.current_retry += 1
        self._reconnect_stats.last_disconnect_ts = time.time()
        self._reconnect_stats.history.append(
            (time.time(), "disconnect", reason)
        )
        logger.warning(
            "%s 断连 (retry=%d/%s): %s",
            self.__class__.__name__,
            self._reconnect_stats.current_retry,
            self._reconnect_policy.max_retries,
            reason,
        )
        if self._on_disconnect:
            try:
                self._on_disconnect(reason)
            except Exception:
                pass

    def _get_backoff_delay(self) -> float:
        """计算当前退避延迟 (指数 + 抖动)。"""
        retry = self._reconnect_stats.current_retry
        policy = self._reconnect_policy

        delay = policy.initial_delay_sec * (policy.backoff_factor ** (retry - 1))
        delay = min(delay, policy.max_delay_sec)

        # 随机抖动
        if policy.jitter > 0:
            jitter_range = delay * policy.jitter
            delay += random.uniform(-jitter_range, jitter_range)

        return max(0.5, delay)

    def _reconnect_sleep(self, delay_sec: float) -> bool:
        """可中断的 sleep。返回 True 表示 sleep 完成，False 表示被 stop 打断。"""
        # 分段 sleep，每 0.5s 检查一次 stop
        chunks = int(delay_sec / 0.5) + 1
        per_chunk = delay_sec / chunks
        for _ in range(chunks):
            if self._stop_event.is_set():
                return False
            time.sleep(per_chunk)
        return True

    def _should_retry(self) -> bool:
        """判断是否应该继续重试。"""
        if self._stop_event.is_set():
            return False
        max_r = self._reconnect_policy.max_retries
        if max_r < 0:  # 无限重试
            return True
        return self._reconnect_stats.current_retry <= max_r

    def _mark_reconnected(self) -> None:
        """标记重连成功。"""
        self._reconnect_stats.total_reconnects += 1
        self._reconnect_stats.current_retry = 0
        self._reconnect_stats.last_reconnect_ts = time.time()
        self._reconnect_stats.history.append(
            (time.time(), "reconnect", "success")
        )
        logger.info(
            "%s 重连成功 (累计 %d 次)",
            self.__class__.__name__,
            self._reconnect_stats.total_reconnects,
        )
        if self._on_reconnect:
            try:
                self._on_reconnect()
            except Exception:
                pass

    def _mark_reconnect_failed(self) -> None:
        """标记重连彻底失败。"""
        self._reconnect_stats.total_failed_retries += 1
        self._reconnect_stats.history.append(
            (time.time(), "reconnect_failed", "max retries exceeded")
        )
        logger.error(
            "%s 重连失败，已达最大重试次数 %d",
            self.__class__.__name__,
            self._reconnect_policy.max_retries,
        )
        if self._on_reconnect_failed:
            try:
                self._on_reconnect_failed()
            except Exception:
                pass

    def get_reconnect_stats(self) -> Dict[str, Any]:
        """获取重连统计信息。"""
        return self._reconnect_stats.to_dict()
