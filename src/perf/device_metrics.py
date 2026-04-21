"""Transitional shim — 迁移到 src.perf.protocol.device."""
from .protocol.device import *  # noqa: F401,F403
from .protocol.device import _read_battery, _battery_poll_loop, _kill_pid  # noqa: F401
