"""
DvtBridge — 通过 pymobiledevice3 DTX RPC 实时采集设备性能指标。

与 xctrace CLI 互补:
- xctrace: 功耗 (Display/CPU/Networking mW)、Time Profiler 调用栈
- DvtBridge: per-process CPU%、内存 (physFootprint)、GPU FPS、网络连接

不需要打开 Instruments GUI，通过 iOS 设备的 remoteserver daemon 直接通信。

架构:
- DvtBridgeThread: 在独立线程中运行 asyncio 事件循环
- DvtBridgeSession: 管理与设备的 DVT 连接
- 收集的数据写入 JSONL 文件，供 PerfIntegrator / TUI 仪表盘消费

已知限制:
- iOS 17+ 需要 tunneld 运行 (sudo pymobiledevice3 remote tunneld)
- 同一时刻只能有一个 DVT 连接（Instruments GUI 占用时无法连接）
- Energy Monitor API 不可靠，因此不使用
- 第一帧 CPU% 为 0，自动跳过
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, List, Any, Callable

from .reconnect import ReconnectableMixin, ReconnectPolicy

logger = logging.getLogger(__name__)


# ── 数据结构 ──


@dataclass
class DvtProcessSnapshot:
    """单进程单次采样快照"""
    ts: float
    pid: int
    name: str
    cpu_usage: Optional[float] = None       # CPU% (0-100)
    phys_footprint_mb: Optional[float] = None  # 物理内存 MB
    mem_anon_mb: Optional[float] = None     # 匿名内存 MB
    mem_virtual_mb: Optional[float] = None  # 虚拟内存 MB
    disk_bytes_read: int = 0
    disk_bytes_written: int = 0
    thread_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "pid": self.pid,
            "name": self.name,
            "cpuUsage": self.cpu_usage,
            "physFootprintMB": self.phys_footprint_mb,
            "memAnonMB": self.mem_anon_mb,
            "memVirtualMB": self.mem_virtual_mb,
            "diskBytesRead": self.disk_bytes_read,
            "diskBytesWritten": self.disk_bytes_written,
            "threadCount": self.thread_count,
        }


@dataclass
class DvtSystemSnapshot:
    """系统级单次采样快照"""
    ts: float
    cpu_total: Optional[float] = None
    phys_memory_free_mb: Optional[float] = None
    phys_memory_used_mb: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "cpuTotal": self.cpu_total,
            "physMemoryFreeMB": self.phys_memory_free_mb,
            "physMemoryUsedMB": self.phys_memory_used_mb,
        }


@dataclass
class DvtNetworkEvent:
    """网络事件快照"""
    ts: float
    event_type: str  # "interface" / "connection" / "update"
    pid: int = 0
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_packets: int = 0
    tx_packets: int = 0
    min_rtt: int = 0
    avg_rtt: int = 0
    interface_name: str = ""
    local_addr: str = ""
    remote_addr: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "type": self.event_type,
            "pid": self.pid,
            "rxBytes": self.rx_bytes,
            "txBytes": self.tx_bytes,
            "rxPackets": self.rx_packets,
            "txPackets": self.tx_packets,
            "minRtt": self.min_rtt,
            "avgRtt": self.avg_rtt,
        }


@dataclass
class DvtGraphicsSnapshot:
    """GPU/图形性能快照"""
    ts: float
    fps: Optional[float] = None
    frame_time_ms: Optional[float] = None
    device_utilization: Optional[float] = None  # GPU 利用率 %

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "fps": self.fps,
            "frameTimeMs": self.frame_time_ms,
            "deviceUtilization": self.device_utilization,
        }


# ── DVT 会话 (asyncio 侧) ──


class DvtBridgeSession(ReconnectableMixin):
    """
    管理与 iOS 设备的 DVT 连接，在 asyncio 事件循环中运行。

    通过 DvtBridgeThread 在独立线程中启动，不阻塞 cpar 主线程。
    集成 ReconnectableMixin 实现断连自动重连。
    """

    def __init__(
        self,
        device_udid: str,
        process_names: Optional[List[str]] = None,
        interval_ms: int = 1000,
        collect_graphics: bool = False,
        collect_network: bool = False,
        process_jsonl: Optional[Path] = None,
        system_jsonl: Optional[Path] = None,
        network_jsonl: Optional[Path] = None,
        graphics_jsonl: Optional[Path] = None,
        on_process_snapshot: Optional[Callable] = None,
        on_alert: Optional[Callable] = None,
        cpu_threshold: float = 80.0,
        memory_threshold_mb: float = 1500.0,
    ):
        self.device_udid = device_udid
        self.process_names = process_names or []
        self.interval_ms = interval_ms
        self.collect_graphics = collect_graphics
        self.collect_network = collect_network
        self.process_jsonl = process_jsonl
        self.system_jsonl = system_jsonl
        self.network_jsonl = network_jsonl
        self.graphics_jsonl = graphics_jsonl
        self.on_process_snapshot = on_process_snapshot
        self.on_alert = on_alert
        self.cpu_threshold = cpu_threshold
        self.memory_threshold_mb = memory_threshold_mb

        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._snapshot_count = 0
        self._error_count = 0
        self._start_ts: float = 0

        # ReconnectableMixin 初始化
        self._reconnect_stop_event = threading.Event()
        self.__init_reconnect__(
            policy=ReconnectPolicy(max_retries=10, initial_delay_sec=2.0),
            stop_event=self._reconnect_stop_event,
        )

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def snapshot_count(self) -> int:
        return self._snapshot_count

    async def run(self):
        """主入口 — 在 asyncio loop 中运行，支持断连重连"""
        self._running = True
        self._start_ts = time.time()
        self._loop = asyncio.get_running_loop()

        while self._running:
            try:
                await self._connect_and_collect()
            except Exception as e:
                self._log_error(f"DVT session failed: {e!r}")
                # 通知 ReconnectableMixin 断连
                self._handle_disconnect(str(e))

                # 如果不再运行（外部 stop），退出
                if not self._running:
                    break

                # 检查是否应该重试
                if not self._should_retry():
                    self._mark_reconnect_failed()
                    break

                # 退避等待
                delay = self._get_backoff_delay()
                logger.info("[dvt_bridge] 等待 %.1f 秒后重连...", delay)
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    break

                if not self._running:
                    break

                # 重连成功后继续循环，_connect_and_collect 成功后会在下方
                # 调用 _mark_reconnected()
                continue

            # _connect_and_collect 正常返回 → 连接成功（或自然结束）
            # 如果之前有断连记录（current_retry > 0），标记重连成功
            if self._reconnect_stats.current_retry > 0:
                self._mark_reconnected()

            # 连接正常结束，退出循环
            break

        self._running = False

    async def _connect_and_collect(self):
        """建立 DVT 连接并开始采集"""
        try:
            from pymobiledevice3.cli.cli_common import create_using_usbmux
            from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
            from pymobiledevice3.services.dvt.instruments.sysmontap import Sysmontap
            from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
        except ImportError as e:
            self._log_error(f"pymobiledevice3 import failed: {e}")
            return

        # 建立 lockdown 连接
        try:
            lockdown = await create_using_usbmux(serial=self.device_udid)
        except Exception as e:
            # 尝试通过 tunneld 连接 (iOS 17+)
            lockdown = await self._try_tunneld()
            if lockdown is None:
                self._log_error(f"无法连接设备 {self.device_udid}: {e}")
                return

        # 建立 DVT 连接
        dvt_provider = DvtProvider(lockdown)
        try:
            await dvt_provider.connect()
        except Exception as e:
            self._log_error(f"DVT 连接失败: {e} (设备可能被 Instruments 占用)")
            return

        try:
            # 启动 sysmontap
            sysmon = await Sysmontap.create(dvt_provider, interval=self.interval_ms)

            # 准备并行采集任务
            tasks = []

            # Sysmon 采集（核心，始终运行）
            async def sysmon_stream():
                async with sysmon:
                    first = True
                    async for snapshot_data in sysmon:
                        if not self._running:
                            break
                        if first:
                            first = False
                            continue
                        await self._process_sysmon_snapshot(snapshot_data)
                        self._snapshot_count += 1

            tasks.append(asyncio.create_task(sysmon_stream()))

            # NetworkMonitor 采集
            if self.collect_network:
                tasks.append(asyncio.create_task(
                    self._stream_network(dvt_provider)
                ))

            # Graphics 采集
            if self.collect_graphics:
                tasks.append(asyncio.create_task(
                    self._stream_graphics(dvt_provider)
                ))

            # 并行运行所有采集任务
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 检查是否有异常（非 sysmon 异常不影响主流程）
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    channel_name = (
                        "sysmon" if i == 0
                        else "network" if (self.collect_network and i == 1)
                        else "graphics"
                    )
                    # 如果是 sysmon 异常（i==0），需要向上抛出以触发重连
                    if i == 0:
                        raise result
                    else:
                        self._log_error(f"{channel_name} 采集异常: {result!r}")

        except Exception as e:
            self._log_error(f"sysmon 采集异常: {e!r}")
            raise  # 向上抛出以触发重连逻辑
        finally:
            try:
                await dvt_provider.close()
            except Exception as e:
                logger.debug("[dvt_bridge] dvt_provider.close() 失败: %s", e)

    async def _try_tunneld(self):
        """尝试通过 tunneld 获取连接 (iOS 17+)"""
        try:
            from pymobiledevice3.tunneld.api import get_tunneld_devices, TUNNELD_DEFAULT_ADDRESS
            from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService

            rsds = await get_tunneld_devices(TUNNELD_DEFAULT_ADDRESS)
            for rsd in rsds:
                if rsd.udid == self.device_udid:
                    return rsd
            # 没有精确匹配，尝试第一个
            if rsds:
                return rsds[0]
        except Exception as e:
            self._log_error(f"tunneld 连接失败: {e}")
        return None

    async def _process_sysmon_snapshot(self, snapshot_data):
        """处理单次 sysmon 采样"""
        now = time.time()

        # 解析进程数据
        if isinstance(snapshot_data, list):
            for entry in snapshot_data:
                if not isinstance(entry, dict):
                    continue

                name = entry.get("name", "")
                pid = entry.get("pid", 0)

                # 如果指定了进程名过滤
                if self.process_names and name not in self.process_names:
                    continue

                # 构建快照
                snap = DvtProcessSnapshot(
                    ts=now,
                    pid=pid,
                    name=name,
                    cpu_usage=entry.get("cpuUsage"),
                    phys_footprint_mb=_bytes_to_mb(entry.get("physFootprint")),
                    mem_anon_mb=_bytes_to_mb(entry.get("memAnon")),
                    mem_virtual_mb=_bytes_to_mb(entry.get("memVirtualSize")),
                    disk_bytes_read=entry.get("diskBytesRead", 0) or 0,
                    disk_bytes_written=entry.get("diskBytesWritten", 0) or 0,
                    thread_count=entry.get("threadCount", 0) or 0,
                )

                # 写入 JSONL
                self._append_jsonl(self.process_jsonl, snap.to_dict())

                # 阈值检查
                self._check_thresholds(snap)

                # 回调
                if self.on_process_snapshot:
                    try:
                        self.on_process_snapshot(snap)
                    except Exception as e:
                        logger.debug("[dvt_bridge] on_process_snapshot 回调异常: %s", e)

        # 解析系统数据 (snapshot_data 可能包含 "System" key 的 dict)
        elif isinstance(snapshot_data, dict):
            sys_data = snapshot_data.get("System")
            if sys_data:
                sys_snap = DvtSystemSnapshot(
                    ts=now,
                    cpu_total=_safe_float(getattr(sys_data, "cpuTotalUsage", None)),
                    phys_memory_free_mb=_bytes_to_mb(getattr(sys_data, "physMemoryFree", None)),
                    phys_memory_used_mb=_bytes_to_mb(getattr(sys_data, "physMemoryUsed", None)),
                )
                self._append_jsonl(self.system_jsonl, sys_snap.to_dict())

    def _check_thresholds(self, snap: DvtProcessSnapshot):
        """检查阈值并触发告警"""
        alerts = []

        if snap.cpu_usage is not None and snap.cpu_usage > self.cpu_threshold:
            alerts.append({
                "level": "warn" if snap.cpu_usage < 95 else "critical",
                "rule": "cpu_high",
                "message": f"{snap.name}({snap.pid}) CPU={snap.cpu_usage:.1f}%",
            })

        if snap.phys_footprint_mb is not None and snap.phys_footprint_mb > self.memory_threshold_mb:
            alerts.append({
                "level": "warn",
                "rule": "memory_high",
                "message": f"{snap.name}({snap.pid}) MEM={snap.phys_footprint_mb:.0f}MB",
            })

        for alert in alerts:
            alert["ts"] = snap.ts
            if self.on_alert:
                try:
                    self.on_alert(alert)
                except Exception as e:
                    logger.debug("[dvt_bridge] on_alert 回调异常: %s", e)

    def _append_jsonl(self, path: Optional[Path], data: Dict[str, Any]):
        """追加写入 JSONL"""
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _log_error(self, msg: str):
        self._error_count += 1
        logger.warning("[dvt_bridge] %s", msg)

    # ── ReconnectableMixin 接口实现 ──

    def _spawn_process(self) -> bool:
        """DVT 使用 asyncio 而非 subprocess，始终返回 True。"""
        return True

    def _is_process_alive(self) -> bool:
        """检查 DVT 会话是否仍在运行。"""
        return self._running

    # ── Network / Graphics 采集流 ──

    async def _stream_network(self, dvt_provider) -> None:
        """
        通过 DVT raw channel 采集网络数据。

        使用 com.apple.instruments.server.services.networking channel。
        数据写入 network_jsonl，格式: {ts, pid, name, rx_bytes, tx_bytes, connections}
        """
        channel_name = "com.apple.instruments.server.services.networking"
        try:
            channel = await dvt_provider.open_channel(channel_name)
        except Exception as e:
            self._log_error(f"无法打开 NetworkMonitor channel: {e}")
            return

        try:
            # 启动监控
            try:
                await channel.call_method("start")
            except Exception:
                # 某些版本可能不需要 start，忽略
                pass

            while self._running:
                try:
                    # 从 channel 读取数据（带超时以响应 stop）
                    try:
                        data = await asyncio.wait_for(channel.next(), timeout=5.0)
                    except asyncio.TimeoutError:
                        continue

                    if data is None:
                        continue

                    now = time.time()

                    # 解析 channel 返回的数据
                    # 网络数据通常是 dict 或包含多个 dict 的列表
                    entries = data if isinstance(data, list) else [data]
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue

                        record = {
                            "ts": now,
                            "pid": entry.get("pid", 0),
                            "name": entry.get("name", ""),
                            "rx_bytes": entry.get("rxBytes", 0),
                            "tx_bytes": entry.get("txBytes", 0),
                            "connections": entry.get("connections", 0),
                        }
                        self._append_jsonl(self.network_jsonl, record)

                except Exception as e:
                    logger.debug("[dvt_bridge] network 数据解析异常: %s", e)
                    await asyncio.sleep(0.1)

        except Exception as e:
            self._log_error(f"NetworkMonitor 流异常退出: {e!r}")
        finally:
            try:
                await channel.stopMonitoring()
            except Exception:
                pass

    async def _stream_graphics(self, dvt_provider) -> None:
        """
        通过 DVT raw channel 采集图形/GPU 数据。

        使用 com.apple.instruments.server.services.graphics channel。
        数据写入 graphics_jsonl，格式: {ts, pid, name, fps, gpu_time_ms, draw_calls}
        """
        channel_name = "com.apple.instruments.server.services.graphics"
        try:
            channel = await dvt_provider.open_channel(channel_name)
        except Exception as e:
            self._log_error(f"无法打开 Graphics channel: {e}")
            return

        try:
            # 启动监控
            try:
                await channel.call_method("startSamplingAtTime", 0.0)
            except Exception:
                # 某些版本可能不支持此方法，尝试 start
                try:
                    await channel.call_method("start")
                except Exception:
                    pass

            while self._running:
                try:
                    # 从 channel 读取数据（带超时以响应 stop）
                    try:
                        data = await asyncio.wait_for(channel.next(), timeout=5.0)
                    except asyncio.TimeoutError:
                        continue

                    if data is None:
                        continue

                    now = time.time()

                    # 解析 channel 返回的数据
                    entries = data if isinstance(data, list) else [data]
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue

                        record = {
                            "ts": now,
                            "pid": entry.get("pid", 0),
                            "name": entry.get("name", ""),
                            "fps": entry.get("fps"),
                            "gpu_time_ms": entry.get("gpuTimeMS"),
                            "draw_calls": entry.get("drawCalls", 0),
                        }
                        self._append_jsonl(self.graphics_jsonl, record)

                except Exception as e:
                    logger.debug("[dvt_bridge] graphics 数据解析异常: %s", e)
                    await asyncio.sleep(0.1)

        except Exception as e:
            self._log_error(f"Graphics 流异常退出: {e!r}")
        finally:
            try:
                await channel.stopMonitoring()
            except Exception:
                pass


# ── DVT Bridge 线程 (桥接 asyncio 和 cpar 线程模型) ──


class DvtBridgeThread:
    """
    在独立线程中运行 DVT asyncio 事件循环。

    用法:
        bridge = DvtBridgeThread(device_udid="...", process_names=["Soul_New"])
        bridge.start()   # 启动后台采集线程
        ...
        bridge.stop()    # 停止
    """

    def __init__(
        self,
        device_udid: str,
        process_names: Optional[List[str]] = None,
        interval_ms: int = 1000,
        collect_graphics: bool = False,
        collect_network: bool = False,
        output_dir: Optional[Path] = None,
        cpu_threshold: float = 80.0,
        memory_threshold_mb: float = 1500.0,
        on_process_snapshot: Optional[Callable] = None,
        on_alert: Optional[Callable] = None,
    ):
        self.device_udid = device_udid
        self.process_names = process_names or []
        self.interval_ms = interval_ms
        self.collect_graphics = collect_graphics
        self.collect_network = collect_network
        self.cpu_threshold = cpu_threshold
        self.memory_threshold_mb = memory_threshold_mb
        self.on_process_snapshot = on_process_snapshot
        self.on_alert = on_alert

        # 输出文件
        self.output_dir = output_dir or Path(".")
        self.process_jsonl = self.output_dir / "dvt_process.jsonl"
        self.system_jsonl = self.output_dir / "dvt_system.jsonl"
        self.network_jsonl = self.output_dir / "dvt_network.jsonl"
        self.graphics_jsonl = self.output_dir / "dvt_graphics.jsonl"

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._session: Optional[DvtBridgeSession] = None
        self._stop_event = threading.Event()
        self._started = threading.Event()

    def start(self) -> Dict[str, Any]:
        """启动后台采集线程"""
        if self._thread and self._thread.is_alive():
            return {"status": "already_running"}

        self._stop_event.clear()

        self._session = DvtBridgeSession(
            device_udid=self.device_udid,
            process_names=self.process_names,
            interval_ms=self.interval_ms,
            collect_graphics=self.collect_graphics,
            collect_network=self.collect_network,
            process_jsonl=self.process_jsonl,
            system_jsonl=self.system_jsonl,
            network_jsonl=self.network_jsonl,
            graphics_jsonl=self.graphics_jsonl,
            on_process_snapshot=self._threadsafe_callback(self.on_process_snapshot),
            on_alert=self._threadsafe_callback(self.on_alert),
            cpu_threshold=self.cpu_threshold,
            memory_threshold_mb=self.memory_threshold_mb,
        )

        self._thread = threading.Thread(
            target=self._run_loop,
            name="dvt-bridge",
            daemon=True,
        )
        self._thread.start()

        # 等待启动 (最多 10s)
        self._started.wait(timeout=10)

        return {
            "status": "started" if self._started.is_set() else "starting",
            "device": self.device_udid,
            "processes": self.process_names,
            "interval_ms": self.interval_ms,
        }

    def stop(self) -> Dict[str, Any]:
        """停止采集"""
        self._stop_event.set()

        # 通知 asyncio loop 停止
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._session:
            self._session._running = False

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

        return {
            "status": "stopped",
            "snapshots": self._session.snapshot_count if self._session else 0,
            "errors": self._session._error_count if self._session else 0,
            "duration_sec": round(time.time() - self._session._start_ts, 1) if self._session and self._session._start_ts else 0,
        }

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def get_status(self) -> Dict[str, Any]:
        """获取当前状态"""
        return {
            "running": self.is_alive(),
            "device": self.device_udid,
            "processes": self.process_names,
            "interval_ms": self.interval_ms,
            "snapshots": self._session.snapshot_count if self._session else 0,
            "errors": self._session._error_count if self._session else 0,
        }

    def get_latest_processes(self, n: int = 10) -> List[Dict[str, Any]]:
        """读取最近 N 条进程快照"""
        return _read_jsonl(self.process_jsonl, last_n=n)

    def get_latest_system(self, n: int = 10) -> List[Dict[str, Any]]:
        """读取最近 N 条系统快照"""
        return _read_jsonl(self.system_jsonl, last_n=n)

    # ── 内部 ──

    def _run_loop(self):
        """在独立线程中运行 asyncio 事件循环"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._started.set()

        try:
            self._loop.run_until_complete(self._session.run())
        except Exception as e:
            logger.error("[dvt_bridge] loop error: %s", e)
        finally:
            self._loop.close()

    def _threadsafe_callback(self, callback):
        """将回调包装为线程安全的调用"""
        if not callback:
            return None

        def wrapper(data):
            try:
                callback(data)
            except Exception:
                pass

        return wrapper


# ── 独立子进程模式 (用于 daemon 化) ──


def dvt_bridge_main():
    """作为独立子进程运行的入口。SIGTERM 优雅退出。"""
    import argparse

    parser = argparse.ArgumentParser(description="DVT Bridge Daemon")
    parser.add_argument("--device", required=True, help="设备 UDID")
    parser.add_argument("--process", nargs="*", default=[], help="进程名过滤")
    parser.add_argument("--interval", type=int, default=1000, help="采样间隔 (ms)")
    parser.add_argument("--output-dir", required=True, help="输出目录")
    parser.add_argument("--cpu-threshold", type=float, default=80.0, help="CPU 告警阈值 (%)")
    parser.add_argument("--memory-threshold", type=float, default=1500.0, help="内存告警阈值 (MB)")
    args = parser.parse_args()

    running = True

    def _stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    bridge = DvtBridgeThread(
        device_udid=args.device,
        process_names=args.process,
        interval_ms=args.interval,
        output_dir=output_dir,
        cpu_threshold=args.cpu_threshold,
        memory_threshold_mb=args.memory_threshold,
    )

    bridge.start()

    # 保持进程运行直到收到 SIGTERM
    try:
        while running and bridge.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()


# ── JSONL 读取 ──


def read_dvt_process_jsonl(path: Path, last_n: int = 0) -> List[Dict[str, Any]]:
    """读取 DVT 进程指标 JSONL"""
    return _read_jsonl(path, last_n)


def read_dvt_system_jsonl(path: Path, last_n: int = 0) -> List[Dict[str, Any]]:
    """读取 DVT 系统指标 JSONL"""
    return _read_jsonl(path, last_n)


def format_dvt_process_text(
    records: List[Dict[str, Any]], top_n: int = 10,
) -> str:
    """格式化 DVT 进程指标"""
    if not records:
        return "  (无 DVT 进程数据)"

    lines = []
    for r in records[-top_n:]:
        ts = r.get("ts", 0)
        ts_str = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "?"
        name = r.get("name", "?")
        pid = r.get("pid", "?")
        cpu = r.get("cpuUsage")
        cpu_str = f"{cpu:.1f}%" if isinstance(cpu, (int, float)) else "?"
        mem = r.get("physFootprintMB")
        mem_str = f"{mem:.0f}MB" if isinstance(mem, (int, float)) else "?"
        threads = r.get("threadCount", "?")
        lines.append(f"  {ts_str}  {name}({pid})  CPU={cpu_str}  MEM={mem_str}  threads={threads}")

    return "\n".join(lines)


def check_dvt_available() -> Dict[str, Any]:
    """检查 DVT 桥接是否可用"""
    result = {
        "pymobiledevice3": False,
        "tunneld": False,
        "usbmux": False,
    }

    # 检查 pymobiledevice3
    try:
        import pymobiledevice3  # noqa: F401
        result["pymobiledevice3"] = True
        result["version"] = getattr(pymobiledevice3, "__version__", "unknown")
    except ImportError:
        result["error"] = "pymobiledevice3 not installed"
        return result

    # 检查 tunneld
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "pymobiledevice3.*tunneld"],
            capture_output=True, timeout=3,
        )
        result["tunneld"] = proc.returncode == 0
    except Exception:
        pass

    # 检查 usbmux
    try:
        from pymobiledevice3 import usbmux as usbmuxd
        devices = usbmuxd.select_devices()
        result["usbmux"] = True
        result["usbmux_devices"] = len(devices)
    except Exception:
        pass

    return result


# ── 工具函数 ──


def _bytes_to_mb(val) -> Optional[float]:
    """字节数转 MB"""
    if val is None:
        return None
    try:
        return float(val) / (1024 * 1024)
    except (TypeError, ValueError):
        return None


def _safe_float(val) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _read_jsonl(path: Path, last_n: int = 0) -> List[Dict[str, Any]]:
    """通用 JSONL 读取"""
    if not path.exists():
        return []
    records = []
    try:
        text = path.read_text(encoding="utf-8")
        for line in text.strip().splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    if last_n > 0:
        records = records[-last_n:]
    return records
