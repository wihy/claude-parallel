"""
perf_defaults — perf 子系统持久化默认配置。

配置文件: ~/.cpar/perf_defaults.json

优先级 (高→低):
  1. CLI 显式参数 (--device XXX)
  2. 环境变量 (CPAR_DEVICE, CPAR_REPO, ...)
  3. perf_defaults.json 持久化配置
  4. 代码内硬编码默认值

用法:
  from .perf_defaults import PerfDefaults

  defaults = PerfDefaults.load()
  device = args.device or defaults.get("device")
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


# 持久化文件路径
DEFAULTS_DIR = Path.home() / ".cpar"
DEFAULTS_FILE = DEFAULTS_DIR / "perf_defaults.json"

# 支持持久化的字段及其描述
PERSISTABLE_FIELDS = {
    "repo": "项目仓库路径",
    "attach": "目标进程名 (App 名)",
    "tag": "会话标签",
    "templates": "模板列表 (逗号分隔)",
    "duration": "录制时长 (秒)",
    "baseline": "Baseline tag",
    "threshold_pct": "回归阈值 (%)",
    "metrics_source": "指标采集源 (auto|device|xctrace)",
    "metrics_interval": "per-process 采样间隔 (ms)",
    "battery_interval": "电池轮询间隔 (秒)",
    "sampling_interval": "旁路采样间隔 (秒)",
    "sampling_top": "每 cycle 记录 Top N 热点",
    "composite": "Composite 模式",
    "attach_webcontent": "自动发现 WebContent 进程 (true/false)",
    "focus": "AI 诊断聚焦领域 (general|webkit|network)",
    "model": "LLM 模型名",
}

# CLI 参数名 → 持久化字段名 映射
# 有些 CLI 参数名和持久化字段名不完全一致 (如 --threshold-pct → threshold_pct)
ARG_TO_FIELD = {
    "repo": "repo",
    "attach": "attach",
    "tag": "tag",
    "templates": "templates",
    "duration": "duration",
    "baseline": "baseline",
    "threshold_pct": "threshold_pct",
    "threshold-pct": "threshold_pct",
    "metrics_source": "metrics_source",
    "metrics-source": "metrics_source",
    "metrics_interval": "metrics_interval",
    "metrics-interval": "metrics_interval",
    "battery_interval": "battery_interval",
    "battery-interval": "battery_interval",
    "sampling_interval": "sampling_interval",
    "sampling-interval": "sampling_interval",
    "sampling_top": "sampling_top",
    "sampling-top": "sampling_top",
    "composite": "composite",
    "attach_webcontent": "attach_webcontent",
    "attach-webcontent": "attach_webcontent",
    "focus": "focus",
    "model": "model",
}

# 环境变量映射
ENV_VARS = {
    "attach": "CPAR_ATTACH",
    "tag": "CPAR_TAG",
    "templates": "CPAR_TEMPLATES",
}


class PerfDefaults:
    """Perf 默认配置管理器。"""

    def __init__(self, data: Optional[Dict[str, Any]] = None):
        self._data: Dict[str, Any] = data or {}

    # ── 加载 / 保存 ──

    @classmethod
    def load(cls) -> "PerfDefaults":
        """从 ~/.cpar/perf_defaults.json 加载配置。"""
        if DEFAULTS_FILE.exists():
            try:
                raw = DEFAULTS_FILE.read_text(encoding="utf-8")
                data = json.loads(raw)
                if isinstance(data, dict):
                    return cls(data)
            except (json.JSONDecodeError, OSError):
                pass
        return cls()

    def save(self) -> None:
        """保存当前配置到文件。"""
        DEFAULTS_DIR.mkdir(parents=True, exist_ok=True)
        # 只保存有值的字段
        clean = {k: v for k, v in self._data.items() if v not in (None, "")}
        DEFAULTS_FILE.write_text(
            json.dumps(clean, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    # ── 读取 ──

    def get(self, field: str, fallback: Any = None) -> Any:
        """获取持久化值，优先级: 显式参数 → 环境变量 → 持久配置 → fallback。"""
        # 环境变量优先
        env_key = ENV_VARS.get(field)
        if env_key:
            env_val = os.environ.get(env_key)
            if env_val:
                return env_val
        # 持久化配置
        val = self._data.get(field)
        if val is not None and val != "":
            return val
        return fallback

    def resolve(self, field: str, cli_value: Any = None, fallback: Any = None) -> Any:
        """三级优先级解析: cli_value → 环境变量/持久配置 → fallback。

        对于 CLI 中的 store_true 类参数 (如 --sampling)，cli_value=False 时不覆盖持久配置。
        只在 cli_value 显式为真值或非空时才采用。
        """
        if cli_value is not None and cli_value not in (None, "", False, 0):
            return cli_value
        return self.get(field, fallback)

    def resolve_bool(self, field: str, cli_value: Any = None, fallback: bool = False) -> bool:
        """解析布尔字段，特殊处理 store_true 模式。

        argparse store_true 在未指定时给出 False，但用户可能意图"保持上次的设置"。
        所以只有 cli_value=True 时才覆盖持久配置。
        """
        if cli_value is True:
            return True
        persisted = self._data.get(field)
        if persisted is not None:
            return bool(persisted)
        return fallback

    # ── 修改 ──

    def set(self, field: str, value: Any) -> None:
        """设置默认值并持久化。"""
        if field not in PERSISTABLE_FIELDS:
            raise KeyError(f"未知字段: {field}。可用: {', '.join(PERSISTABLE_FIELDS)}")
        self._data[field] = value
        self.save()

    def unset(self, field: str) -> None:
        """删除默认值并持久化。"""
        self._data.pop(field, None)
        self.save()

    # ── 展示 ──

    def show(self) -> str:
        """格式化展示当前所有默认配置。"""
        if not self._data:
            return "  (无持久化配置。首次使用 cpar perf start 时会自动保存参数为默认值。)"

        lines = []
        for field, desc in PERSISTABLE_FIELDS.items():
            val = self._data.get(field)
            if val is not None:
                lines.append(f"  {field:25s} = {val}  ({desc})")

        if not lines:
            return "  (无持久化配置)"

        header = f"  配置文件: {DEFAULTS_FILE}\n"
        return header + "\n".join(lines)

    @property
    def data(self) -> Dict[str, Any]:
        return dict(self._data)

    def update_from_args(self, args) -> None:
        """从 CLI args 中提取非默认值并更新持久化配置。

        策略: 如果用户显式提供了非空、非默认的值，就更新到持久化配置。
        对于 start 子命令的关键参数 (repo, device, attach) 总是更新。
        """
        updated = False
        # 关键字段: 只要有值就更新
        key_fields = ["repo", "attach"]
        for f in key_fields:
            val = getattr(args, f, None)
            if val:
                if self._data.get(f) != val:
                    self._data[f] = val
                    updated = True

        # 可选字段: 有值且与默认不同时更新
        optional_fields = {
            "tag": "tag",
            "templates": "templates",
            "duration": "duration",
            "baseline": "baseline",
            "threshold_pct": "threshold_pct",
            "metrics_source": "metrics_source",
            "metrics_interval": "metrics_interval",
            "battery_interval": "battery_interval",
            "sampling_interval": "sampling_interval",
            "sampling_top": "sampling_top",
            "composite": "composite",
        }
        for attr, field in optional_fields.items():
            val = getattr(args, attr, None)
            if val is not None and val != "" and val != 0:
                # 跳过 argparse 默认值 (tag="perf", duration=1800, templates="power" 等)
                defaults = {
                    "tag": "perf", "templates": "power", "duration": 1800,
                    "threshold_pct": 0.0, "metrics_source": "auto",
                    "metrics_interval": 1000, "battery_interval": 10,
                    "sampling_interval": 10, "sampling_top": 10,
                    "composite": "auto",
                }
                if attr in defaults and val == defaults[attr]:
                    continue  # argparse 默认值，不更新持久配置
                if self._data.get(field) != val:
                    self._data[field] = val
                    updated = True

        # 布尔字段
        for attr, field in [("attach_webcontent", "attach_webcontent")]:
            val = getattr(args, attr, None)
            if val:
                if not self._data.get(field):
                    self._data[field] = True
                    updated = True

        if updated:
            self.save()

        return updated
