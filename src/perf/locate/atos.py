"""常驻 atos 子进程 — 消除每次 subprocess 的启动开销。

使用方式:
    daemon = AtosDaemon(binary_path, load_addr)
    daemon.start()          # 启动 atos -i -o bin -l addr
    sym = daemon.lookup(0x100001234)  # 喂 stdin,读 stdout
    daemon.shutdown()       # 停进程,flush
"""

import subprocess
import threading
import queue
from pathlib import Path
from typing import Optional


ATOS_FAILURE_THRESHOLD = 5          # 连续 N 次失败后入黑名单
ATOS_READ_TIMEOUT_SEC = 0.5         # 单次 lookup 软超时 (真机大 dylib
                                    # 深 offset 地址偶尔需要 >200ms, 200ms
                                    # 在 500MB+ binary 上会误判为 miss;
                                    # 0.5s 对齐 resolver 外层 DEFAULT_TIMEOUT_MS)


class AtosDaemon:
    """常驻 atos 进程 —— 用 stdin/stdout 流式查询符号。

    线程安全: lookup() 持锁串行化到 atos;对外 API 可多线程调用。
    """

    def __init__(
        self, binary_path: str, load_addr: int = 0,
        *, read_timeout_sec: Optional[float] = None,
    ):
        self.binary_path = str(Path(binary_path).expanduser())
        self.load_addr = load_addr
        # 允许调用方覆盖超时 (大 binary / 压力测试可调高)
        self.read_timeout_sec = (
            read_timeout_sec if read_timeout_sec is not None
            else ATOS_READ_TIMEOUT_SEC
        )
        self._proc: Optional[subprocess.Popen] = None
        self._started = False
        self._lock = threading.Lock()
        self._response_queue: "queue.Queue[str]" = queue.Queue()
        self._reader_thread: Optional[threading.Thread] = None
        self._failures: dict = {}
        self._blacklist: set = set()

    def start(self) -> None:
        if self._started:
            return
        args = [
            "atos", "-i",
            "-o", self.binary_path,
            "-l", hex(self.load_addr),
        ]
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,  # line-buffered
            text=True,
        )
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        self._started = True

    def _read_loop(self) -> None:
        if not self._proc or not self._proc.stdout:
            return
        for line in self._proc.stdout:
            self._response_queue.put(line.rstrip("\n"))

    def _put_response(self, text: str) -> None:
        """测试钩子 — 直接注入响应,绕开真实 atos 子进程。"""
        self._response_queue.put(text)

    def _drain_queue(self) -> int:
        """清空 response queue 中的残余响应 (前一次 lookup 超时后晚到的)。

        持锁前提下调用。返回丢弃的响应数。
        """
        n = 0
        while True:
            try:
                self._response_queue.get_nowait()
                n += 1
            except queue.Empty:
                return n

    def lookup(self, addr: int) -> str:
        """同步查询单个地址。失败或未启动时返回 hex 字符串。

        Stale response 防护: 写 stdin 前 drain queue,阻断"上一次 lookup
        超时后晚到的响应被当前 lookup 误读"的竞态 (真机 atos 偶发 >200ms
        响应场景)。
        """
        if addr in self._blacklist:
            return f"0x{addr:x}"
        if not self._started or not self._proc or not self._proc.stdin:
            return f"0x{addr:x}"

        with self._lock:
            # Phase 1: 清空残余响应 (前一次 timeout 后晚到的)
            self._drain_queue()
            try:
                self._proc.stdin.write(f"{hex(addr)}\n")
                self._proc.stdin.flush()
                sym = self._response_queue.get(timeout=self.read_timeout_sec)
                if sym and not sym.startswith("0x"):
                    self._failures.pop(addr, None)
                    return sym
                self._record_failure(addr)
                return f"0x{addr:x}"
            except (BrokenPipeError, OSError, queue.Empty):
                self._record_failure(addr)
                return f"0x{addr:x}"

    def _record_failure(self, addr: int) -> None:
        self._failures[addr] = self._failures.get(addr, 0) + 1
        if self._failures[addr] >= ATOS_FAILURE_THRESHOLD:
            self._blacklist.add(addr)

    def shutdown(self) -> None:
        if not self._started:
            return
        self._started = False
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self._proc.kill()
                except OSError:
                    pass
        self._proc = None
