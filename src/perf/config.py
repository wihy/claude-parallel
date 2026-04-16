"""PerfConfig — 性能采集配置数据类"""

from dataclasses import dataclass


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

    # 实时 syslog 分析
    live_rules_file: str = ""       # 自定义规则文件路径
    live_alert_log: str = ""        # 告警日志输出路径
    live_buffer_lines: int = 200    # 实时分析缓冲行数

    # 实时指标流
    stream_interval: float = 10.0   # 指标导出间隔 (秒)
    stream_window: int = 30         # 滚动窗口快照数
    stream_jsonl: str = ""          # 时序快照 JSONL 输出路径
