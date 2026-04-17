# 快速上手提示模板

使用 `cpar perf` 前的环境确认和常用命令模板。复制粘贴即可。

## 1. 环境信息

| 项目 | 值 | 确认方式 |
|------|------|---------|
| 工具目录 | `~/claude-parallel` | `ls ~/claude-parallel/cpar` |
| 项目仓库 | `/Users/chunhaixu/SoulApp` | 按实际项目路径替换 |
| App Bundle ID | `com.soulapp.cn` | Xcode → Target → General |
| App 可执行名 | `Soul_New` | `CFBundleExecutable` in Info.plist |
| 设备型号 | iPhone 14 Pro | — |
| iOS 版本 | 26.3.1 | — |
| 设备 UDID | `00008120-00164C893AEB401E` | `cpar perf devices` |

## 2. 开始前检查清单

```bash
# ① 确认设备已通过 USB 连接
cpar perf devices
# 预期: 列出 iPhone14 pro (00008120-00164C893AEB401E)
# 若无输出: 检查 USB 线 → 信任弹窗 → 重插

# ② 确认 App 已安装并在前台运行
# 在设备上打开 SoulApp

# ③ 确认 xctrace 可用
xcrun xctrace --version
# 预期: Instruments... 16.x

# ④（可选）确认 pymobiledevice3 + tunneld
pymobiledevice3 --version
# 若需 per-process CPU/内存:
sudo pymobiledevice3 remote tunneld  # 一次性后台运行
```

## 3. 常用命令模板

> 以下命令中的变量含义:
> - `REPO` = 你的项目仓库路径
> - `TAG` = 本次采集标签（自定义，如 `webview-opt-v1`）
> - `UDID` = 设备 UDID
> - `PROCESS` = App 可执行名

### 3.1 纯热点分析（最简单）

```bash
# 启动
cpar perf start --repo REPO --tag TAG \
  --device UDID --attach PROCESS \
  --sampling

# 实时追踪（另一个终端）
cpar perf hotspots --repo REPO --tag TAG --follow

# 停止 + 聚合
cpar perf stop --repo REPO --tag TAG
cpar perf hotspots --repo REPO --tag TAG --aggregate
```

**SoulApp 示例**：
```bash
cpar perf start --repo ~/SoulApp --tag luk-hotspot \
  --device 00008120-00164C893AEB401E --attach Soul_New \
  --sampling

cpar perf hotspots --repo ~/SoulApp --tag luk-hotspot --follow
```

### 3.2 热点 + 电池并行（推荐日常使用）

```bash
cpar perf start --repo REPO --tag TAG \
  --device UDID --attach PROCESS \
  --sampling --metrics-source device --battery-interval 5

# 同时查看
cpar perf hotspots --repo REPO --tag TAG --follow
cpar perf battery --repo REPO --tag TAG --last 10

# 停止
cpar perf stop --repo REPO --tag TAG
```

**SoulApp 示例**：
```bash
cpar perf start --repo ~/SoulApp --tag webview-opt-full \
  --device 00008120-00164C893AEB401E --attach Soul_New \
  --sampling --metrics-source device --battery-interval 5
```

### 3.3 全指标仪表盘（systemtrace + 电池）

```bash
# systemtrace 一轮拿到：CPU 负载 + 温度状态 + 热点函数 + 电池
cpar perf start --repo REPO --tag TAG \
  --device UDID --attach PROCESS \
  --templates systemtrace --duration 300 \
  --battery-interval 5

# 录制完成后查看 dashboard
cpar perf stop --repo REPO --tag TAG
cpar perf dashboard --repo REPO --tag TAG
cpar perf dashboard --repo REPO --tag TAG --csv > metrics.csv
```

**SoulApp 示例**：
```bash
cpar perf start --repo ~/SoulApp --tag game-dashboard \
  --device 00008120-00164C893AEB401E --attach Soul_New \
  --templates systemtrace --duration 300 --battery-interval 5
```

> 注意：systemtrace 自带 time-profile schema，录制完成后可直接用 `cpar perf callstack` 分析热点函数。

### 3.4 纯功耗分析（Power Profiler）

```bash
cpar perf start --repo REPO --tag TAG \
  --device UDID --attach PROCESS \
  --templates power --duration 300

cpar perf stop --repo REPO --tag TAG
cpar perf report --repo REPO --tag TAG
```

**注意**：功耗模式与 sampling 互斥（iOS 只允许一个 xctrace），不要同时加 `--sampling`。

### 3.5 调用栈分析（事后分析）

```bash
# 需要先完成一次含 Time Profiler 模板的采集
cpar perf start --repo REPO --tag TAG \
  --device UDID --attach PROCESS \
  --templates time --duration 30

cpar perf stop --repo REPO --tag TAG
cpar perf callstack --repo REPO --tag TAG --top 20
```

### 3.6 基线对比 + 性能门禁

```bash
# 先录 baseline
cpar perf start --repo REPO --tag baseline \
  --device UDID --attach PROCESS --templates power --duration 300
cpar perf stop --repo REPO --tag baseline

# 对比（阈值 10%）
cpar perf report --repo REPO --tag current \
  --baseline baseline --threshold-pct 10.0
```

### 3.7 实时 Syslog 告警

```bash
cpar perf live --device UDID --tag syslog-watch
```

### 3.8 查看已有数据

```bash
# 热点快照
cpar perf hotspots --repo REPO --tag TAG --last 5
cpar perf hotspots --repo REPO --tag TAG --aggregate --json

# 电池趋势
cpar perf battery --repo REPO --tag TAG --last 20

# Per-process 指标（需 tunneld）
cpar perf metrics --repo REPO --tag TAG --last 10
```

## 4. 故障排查

| 问题 | 排查 |
|------|------|
| `perf devices` 无输出 | USB 线松了 / 未信任 / 重插 |
| `sampling.enabled: false` + `reason: xctrace_exclusive` | 主链路 xctrace 已占 slot，改用 `--metrics-source device` |
| hotspots.jsonl 为空 | 检查 `logs/sampling.stderr`，可能 xctrace exit=2（互斥） |
| 全是 `0x...` 地址无符号 | Xcode 需打开目标项目以加载 dSYM |
| `pymobiledevice3` 报 tunneld 错误 | 运行 `sudo pymobiledevice3 remote tunneld` |
| iOS 26 syslog 不抓 App 日志 | 已知限制，改用 Xcode Console.app 抓 `process:Soul_New` |
| battery.jsonl 无数据 | 确认 `ideviceinfo` 已安装：`brew install libimobiledevice` |

## 5. 数据目录结构

采集数据存储在项目的 `.claude-parallel/perf/<TAG>/` 下：

```
.claude-parallel/perf/<TAG>/
├── meta.json              # 会话元数据（PID、配置、状态）
├── timeline.json          # 事件时间线
├── report.json            # 最终报告
├── logs/
│   ├── hotspots.jsonl     # 热点函数时序 ← cpar perf hotspots 读取
│   ├── battery.jsonl      # 电池指标时序 ← cpar perf battery 读取
│   ├── process_metrics.jsonl  # 进程指标 ← cpar perf metrics 读取
│   ├── syslog_full.log    # 完整 syslog
│   ├── alerts.log         # 告警摘要
│   ├── metrics.jsonl      # xctrace 指标快照
│   └── sampling.stderr    # sampling 错误日志
├── traces/                # xctrace trace 文件
└── exports/               # 导出的 XML
```
