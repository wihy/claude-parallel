"""PerfConfig — 性能采集配置数据类"""

from dataclasses import dataclass, field


@dataclass
class PerfConfig:
    enabled: bool = False
    tag: str = "perf"
    device: str = ""
    attach: str = ""
    duration_sec: int = 1800
    templates: str = "power"         # 逗号分隔模板别名: "power,time,gpu"
    baseline_tag: str = ""
    threshold_pct: float = 0.0

    # Composite 模式 (多 instrument 合并单个 xctrace 进程)
    # "auto" — 多模板时自动启用 composite
    # "full"/"webperf"/"power_cpu"/... — 使用预置组合
    # "power+time+network" — 自由组合
    # "" — 禁用 composite, 使用旧的多进程模式
    composite: str = "auto"

    # 实时 syslog 分析
    live_rules_file: str = ""       # 自定义规则文件路径
    live_alert_log: str = ""        # 告警日志输出路径
    live_buffer_lines: int = 200    # 实时分析缓冲行数

    # 实时指标流
    stream_interval: float = 10.0   # 指标导出间隔 (秒)
    stream_window: int = 30         # 滚动窗口快照数
    stream_jsonl: str = ""          # 时序快照 JSONL 输出路径

    # Sampling Profiler 旁路
    sampling_enabled: bool = False
    sampling_interval_sec: int = 10  # 5-30 合理区间
    sampling_top_n: int = 10
    sampling_retention: int = 30     # 保留最近 N 个 cycle

    # 指标采集源 (device 路径不占 xctrace slot)
    metrics_source: str = "auto"      # "auto" | "device" | "xctrace"
    metrics_interval_ms: int = 1000   # pymobiledevice3 采样间隔
    battery_interval_sec: int = 10    # 电池轮询间隔

    # WebContent 进程采集
    attach_webcontent: bool = False    # 自动发现并采集 WebContent 进程

    # DvtBridge 网络与图形采集
    collect_network: bool = True       # 采集网络指标到 dvt_network.jsonl
    collect_graphics: bool = True      # 采集图形指标到 dvt_graphics.jsonl

    # Symbol resolver 配置 (Track B resolver 激活条件)
    binary_path: str = ""                                   # 主 binary 路径 (用于 atos 符号化)
    linkmap_paths: list = field(default_factory=list)       # LinkMap 文件路径列表 (多个可叠加,覆盖主 binary + dylib + Extensions)
    dsym_paths: list = field(default_factory=list)          # dSYM 路径列表

    @property
    def linkmap_path(self) -> str:
        """兼容 API — 返回第一个 LinkMap 路径 (若有),空字符串否则。

        历史代码可能用单数形式;新代码应直接用 linkmap_paths。
        """
        return self.linkmap_paths[0] if self.linkmap_paths else ""
