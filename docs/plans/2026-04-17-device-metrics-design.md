# 指标采集改事件驱动 — 设计

日期: 2026-04-17
状态: 已确认，进入实现
关联: 建议 #4，解除 sampling 旁路的 xctrace 互斥限制

## 背景

iOS 设备同一时刻只允许一个 xctrace 录制。当前功耗指标通过
`xcrun xctrace --template "Power Profiler"` 采集，占用唯一 slot，
导致 sampling 旁路（Time Profiler）无法同时运行。

## 方案

用两条非 xctrace 通道替代 Power Profiler：

1. **BatteryPoller** — `ideviceinfo -q com.apple.mobile.battery` 周期轮询（10s）
   - 整机电流 mA / 电压 V / 温度 ℃ / 电量 %
   - 输出: logs/battery.jsonl

2. **ProcessMetricsStreamer** — `pymobiledevice3 dvt sysmon process monitor`
   - Per-process cpuUsage% / physFootprint (内存)
   - 流式采样，间隔 1s（可配置至 500ms）
   - 输出: logs/process_metrics.jsonl

xctrace slot 完全留给 sampling sidecar，三路并行不再互斥。

## 架构

```
┌─ BatteryPoller (ideviceinfo) ───────┐  logs/battery.jsonl
│  轮询 10s, 独立子进程               │
├─ ProcessMetricsStreamer (pymd3) ────┤  logs/process_metrics.jsonl
│  流式 1s, 独立子进程                │
├─ SamplingProfilerSidecar (xctrace) ─┤  logs/hotspots.jsonl
│  cycle 10s, 独立子进程              │
└─ idevicesyslog ─────────────────────┘  logs/syslog_full.log
```

## 模块

**新文件** `src/perf/device_metrics.py`:
- `BatteryPoller` — ideviceinfo 周期轮询 → JSONL
- `ProcessMetricsStreamer` — pymobiledevice3 sysmon 包装 → JSONL
- `read_battery_jsonl()` / `read_process_metrics_jsonl()` — 读取辅助
- `format_metrics_text()` — 文本格式化

## 配置

PerfConfig 新增:
```python
metrics_source: str = "auto"     # "auto" | "device" | "xctrace"
metrics_interval_ms: int = 1000
battery_interval_sec: int = 10
```

决策逻辑:
- auto + pymobiledevice3 可用 → device 路径
- auto + pymobiledevice3 不可用 → xctrace fallback
- device → 强制新路径
- xctrace → 强制旧路径

## CLI

新增参数:
- `--perf-metrics-source auto|device|xctrace`
- `--perf-metrics-interval 1000`
- `--perf-battery-interval 10`

新增命令:
- `cpar perf metrics --repo --tag --follow/--last/--json`
- `cpar perf battery --repo --tag --last/--json`

## 兼容性

- 不装 pymobiledevice3 → auto fallback 到 xctrace，行为完全不变
- LiveMetricsStreamer 保留作为 xctrace fallback
- cpar perf stream 旧命令继续工作

## 测试

- mock ideviceinfo / pymobiledevice3 输出 → JSONL 解析
- metrics_source=auto 决策逻辑
- 真机: device + sampling 同时运行验证不互斥

## 不做

- GPU/FPS — 不在 sysmontap 标准属性
- Network 流量 — 需单独工具链
- 历史趋势图表 — 后续优化
