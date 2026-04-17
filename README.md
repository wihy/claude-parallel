# Claude Parallel v0.3.0

多 Claude Code 并行协同执行框架。通过 YAML 定义任务依赖关系，自动编排多个 Claude Code 实例并行工作。

## 特性

### 核心能力
- **DAG 依赖调度** — 自动拓扑排序，同层任务并行，跨层顺序执行
- **Git Worktree 隔离** — 每个任务在独立 worktree 中工作，互不干扰
- **依赖上下文注入** — 上游任务的产出自动提取接口/API 签名并传递给下游
- **多语言支持** — 上下文提取支持 Python/JS/TS/Go/Rust/Java

### 鲁棒性 (Phase 2)
- **失败自动重试** — 可配置重试次数 + 指数退避，自动识别可重试错误
- **总预算控制** — 设置全局预算上限，超限自动停止
- **实时进度面板** — Rich Live 展示每个 Worker 的实时状态
- **中断恢复** — Ctrl+C 优雅退出，`resume` 命令从中断处继续
- **流式日志** — 每个 Worker 独立日志文件，实时捕获 stderr

### 工程化 (Phase 3)
- **智能合并** — WorktreeMerger 支持冲突自动检测，Claude 辅助解决冲突
- **Diff 预览** — merge 前查看所有 worktree 的变更摘要
- **Code Review** — 可选的自动代码审查，发现潜在问题
- **配置校验** — 执行前验证 YAML 正确性，友好错误提示
- **自动提交** — Worktree 中未提交变更自动 commit，防丢失

### 真机性能采集 (Phase 4 — perf 子系统)
- **Instruments 模板管理** — 10 个内置模板 (Power/Time/Network/GPU/Leaks 等)，支持自定义扩展
- **xctrace 采集会话** — 自动管理 xctrace record 生命周期 (start/stop)，支持 attach 进程
- **实时 syslog 告警** — 流式分析 iOS 真机日志，13 条内置规则 (OOM/Jetsam/热管理/WebKit 崩溃/ANR 等)
- **实时指标流** — 周期性导出 xctrace 指标，滚动窗口快照，支持 JSONL 时序记录
- **与 run 命令集成** — `--with-perf` 在任务执行期间同步采集真机性能数据
- **基线对比 + 回归门禁** — 对比历史基线，超阈值自动告警，可配置 `--strict-perf-gate` 阻断发布
- **Time Profiler 调用栈分析** — `perf callstack` 自动导出 xctrace trace 并解析调用栈热点，Top N 排行
- **PerfGate 退化检测** — 功耗/CPU/调用栈多维对比基线，百分比偏差计算，超阈值告警
- **Session 持久化管理** — 采集会话 save/load/list/delete，支持跨 session 基线对比
- **Report 增强** — `--with-callstack` 一键含调用栈分析，`--callstack-top N` 控制热点数量

### Sampling Profiler 旁路 + Device 指标 (Phase 4.1)
- **运行时热点预览** — xctrace Time Profiler 10s 短周期循环采集，实时 Top-N 热点函数排行
- **Xcode 16+ 兼容** — 自动检测 `time-profile` (新) / `TimeProfiler` (旧) 两种 XML 格式，id/ref 两遍扫描
- **独立子进程 daemon** — sampling sidecar 以独立进程运行，主进程退出后持续采集
- **Device 指标通道** — `ideviceinfo` 电池轮询 + `pymobiledevice3` per-process CPU%/内存，不占 xctrace slot
- **xctrace 互斥解除** — `--metrics-source device` 模式下，电池 + sampling 完全并行，不再互斥
- **`hotspots --follow`** — tail -f 式实时追踪热点变化
- **真机验证** — iPhone 14 Pro (iOS 26.3) 端到端验证通过，业务符号可见（SOAnimationBatchHandler 等）

### Dashboard 全指标仪表盘 + 稳定性加固 (Phase 4.2)
- **`perf dashboard`** — 统一时序表 + 汇总统计（avg/peak/min/jitter），支持 `--json` / `--csv` 导出
- **systemtrace 真机适配** — 适配 Xcode 16+ 真实 schema（`system-load` / `device-thermal-state-intervals` / `time-profile`）
- **温度状态采集** — 从 systemtrace 提取设备热状态（Nominal/Fair/Serious/Critical）
- **电池始终采集** — BatteryPoller 不受 `metrics_source` 限制，与任何 xctrace 模板并行
- **JSONL 原子轮转** — `fcntl.flock` 加锁 + 原子 rename，消除竞态写数据丢失
- **PID 文件持久化** — `.sampling_daemon.pid` 防止崩溃后孤儿进程泄漏
- **渐进降频容错** — xctrace export 连续失败时自动降频（3→5→10 次自停），避免 CPU 空转
- **正则预编译** — 6 个高频正则编译为模块常量，减少解析开销
- **读写锁保护** — `fcntl.LOCK_SH` / `LOCK_EX` 防止 JSONL 读写竞争
- **并发分析** — `ThreadPoolExecutor` 并行 xctrace export/parse，报告生成 4 路并行，多 trace 调用栈并行解析

## 前置要求

- Python 3.9+
- Claude Code CLI (`npm install -g @anthropic-ai/claude-code`)
- 已认证的 Claude 账号（OAuth 或 API Key）
- Git 仓库

### perf 子系统额外要求（可选）
- macOS (xctrace / Instruments)
- Xcode Command Line Tools (`xcode-select --install`)
- 已连接的 iOS 真机或模拟器
- idevicesyslog (libimobiledevice，用于 syslog 采集)
- pymobiledevice3 (可选，用于 per-process CPU%/内存采集，需 `sudo pymobiledevice3 remote tunneld`)

## 安装

```bash
cd ~/claude-parallel
pip3 install --user pyyaml rich
```

## 快速开始

### 1. 创建任务文件

```yaml
project:
  repo: ~/myproject
  max_workers: 3
  retry_count: 2
  total_budget_usd: 10.0

tasks:
  - id: backend
    description: "创建 FastAPI 用户管理端点"
    files: ["src/api/users.py"]
    depends_on: []

  - id: frontend
    description: "创建用户管理 React 组件"
    files: ["src/components/Users.tsx"]
    depends_on: ["backend"]
    extra_prompt: "API 端点: GET /api/users, POST /api/users"

  - id: tests
    description: "编写集成测试"
    files: ["tests/test_users.py"]
    depends_on: ["backend"]
```

### 2. 校验配置

```bash
python3 run.py validate tasks.yaml
```

### 3. 预览计划

```bash
python3 run.py plan tasks.yaml
```

### 4. 执行

```bash
# 执行 + 合并 + 清理
python3 run.py run tasks.yaml --merge --clean

# 带预算控制
python3 run.py run tasks.yaml --retry 3 --total-budget 5.0
```

### 5. 查看变更

```bash
# 预览所有变更 (不合并)
python3 run.py diff tasks.yaml

# Code Review
python3 run.py review tasks.yaml --budget 1.0
```

### 6. 中断恢复

```bash
python3 run.py resume tasks.yaml --merge
```

### 7. 真机性能采集

```bash
# 查看已连接设备
cpar perf devices

# 启动功耗采集 (attach 到目标进程)
cpar perf start --repo . --device UDID --attach Soul_New --templates power

# 实时查看 syslog 告警
cpar perf live --device UDID

# 停止采集并生成报告
cpar perf stop --repo . --tag perf
cpar perf report --repo . --tag perf --with-callstack
```

### 8. Sampling Profiler 运行时热点

```bash
# 启动 sampling 旁路（10s/cycle 循环采集 Time Profiler）
cpar perf start --repo . --tag hotspot \
  --device UDID --attach Soul_New --sampling

# 实时追踪热点函数（另一个终端）
cpar perf hotspots --repo . --tag hotspot --follow

# 全会话聚合 Top-N
cpar perf stop --repo . --tag hotspot
cpar perf hotspots --repo . --tag hotspot --aggregate
```

### 9. 热点 + 电池并行（推荐）

```bash
# device 模式：电池轮询 + sampling 并行，不占 xctrace slot
cpar perf start --repo . --tag full \
  --device UDID --attach Soul_New \
  --sampling --metrics-source device --battery-interval 5

# 同时查看
cpar perf hotspots --repo . --tag full --follow
cpar perf battery --repo . --tag full --last 10
```

### 10. 与并行执行框架集成

```bash
python3 run.py run tasks.yaml --with-perf \
  --perf-device UDID --perf-attach Soul_New \
  --perf-sampling --perf-metrics-source auto
```

## 完整命令列表

```
run       执行任务 (--dry/--merge/--clean/--retry N/--total-budget $, --with-perf ..., --perf-threshold-pct N, --strict-perf-gate)
resume    从中断处恢复
plan      展示执行计划
validate  校验 YAML 配置
diff      预览所有 worktree 变更
merge     合并 worktree (支持冲突自动解决)
review    对所有变更执行 Code Review
clean     清理 worktree 和协调文件
logs      查看任务日志
chat      对话模式 (自然语言生成任务 YAML)

perf 子命令 (15 个):
  perf start       启动采集 (--sampling, --metrics-source device|xctrace|auto)
  perf stop        停止采集
  perf tail        实时查看 syslog
  perf report      生成报告 (--with-callstack, --callstack-top N)
  perf devices     列出已连接设备
  perf live        实时 syslog 告警分析
  perf rules       列出/管理告警规则 (13 条内置)
  perf stream      实时 xctrace 指标流
  perf snapshot    导出指标快照
  perf callstack   调用栈分析 (--top N)
  perf hotspots    运行时热点函数 (--follow, --aggregate, --last N)
  perf dashboard   全指标仪表盘 (时序表+汇总, --json/--csv)
  perf metrics     Per-process CPU/内存指标
  perf battery     电池功耗趋势
  perf templates   Instruments 模板管理 (10 个内置)
```

## 文件结构

```
claude-parallel/
├── run.py                      # CLI 入口
├── chat.py                     # 对话模式入口
├── cpar                        # 封装脚本
├── src/
│   ├── cli.py                  # CLI 命令处理
│   ├── task_parser.py          # YAML 解析 + DAG 拓扑排序
│   ├── worker.py               # Worker 进程管理 + 重试 + 日志
│   ├── orchestrator.py         # 调度器 (DAG/重试/预算/恢复)
│   ├── monitor.py              # Rich Live 实时进度面板
│   ├── merger.py               # Worktree 合并 + 冲突自动解决
│   ├── reviewer.py             # 自动 Code Review
│   ├── validator.py            # YAML 配置校验
│   ├── context_extractor.py    # 多语言上下文提取
│   ├── chat_input.py           # 多行富文本输入 (prompt_toolkit)
│   └── perf/                   # 真机性能采集子系统
│       ├── config.py           # PerfConfig 数据类 (30 个配置字段)
│       ├── session.py          # 采集会话生命周期 + metrics_source 决策
│       ├── sampling.py         # Sampling Profiler 旁路 + XML 解析器
│       ├── device_metrics.py   # BatteryPoller + ProcessMetricsStreamer
│       ├── live_log.py         # 实时 syslog 流式告警分析
│       ├── live_metrics.py     # 实时 xctrace 指标流 + 滚动窗口
│       ├── templates.py        # Instruments 模板注册与管理
│       └── integrator.py       # 与 Orchestrator 深度集成胶水
├── scripts/
│   └── perf_e2e_smoke.sh       # iOS 真机端到端冒烟验证 (4 轮)
├── tests/
│   ├── test_chat_input.py      # 输入模块测试
│   ├── test_perf_sampling.py   # Sampling + 解析器测试 (29 cases)
│   └── test_device_metrics.py  # Device 指标测试 (15 cases)
├── examples/
│   ├── auth-system.yaml        # 4任务 DAG 示例
│   ├── test-dag.yaml           # 端到端 DAG 测试
│   ├── simple-parallel.yaml    # 简单并行
│   └── test-p2.yaml            # Phase 2 测试
├── docs/
│   └── playbook.md             # 编排 playbook
└── README.md
```

## 执行结果目录

```
.claude-parallel/
├── coord/          # 状态和结果
├── context/        # 智能提取的跨任务上下文
├── logs/           # Worker 执行日志
├── reviews/        # Code Review 结果
├── results/        # 执行报告
└── plan-snapshot.json   # 执行计划 (用于 resume)
```

## 端到端测试结果

4 任务 DAG (3 层级)，全部成功 + 合并:

```
  Level 0: [utils]         35s  $0.17  3 turns
  Level 1: [greeting]      33s  $0.29  4 turns  ← 并行
           [mathops]        32s  $0.28  4 turns  ← 并行
  Level 2: [main-app]      ~20s  ?      ? turns
  
  总耗时: ~2min (串行需 ~3.5min，节省 ~43%)
  合并: 4/4 clean merge
  生成代码: 可直接运行 ✓
```

## License

MIT
