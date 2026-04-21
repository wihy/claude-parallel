# Claude Parallel v0.4.0

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

### 性能与分析深度 (Phase 4.3)
- **iterparse 流式解析** — XML 解析从正则 3 遍全文扫描改为 `xml.etree.ElementTree.iterparse` 单遍流式，内存恒定
- **Pipeline 重叠采集** — record 与上一轮 export+parse 并行执行，cycle 延迟从 ~20s 降至 ~8s，MIN_INTERVAL 降至 3s
- **完整调用树** — `perf callstack --full-stack` 保留全部 frame（而非仅叶子函数），可回溯调用链
- **时间切片分析** — `perf callstack --from 60 --to 120` 只分析指定时间段的采样，定位"哪个时段的哪个方法导致高 CPU"
- **systemtrace 自动识别** — systemtrace 模板含 time-profile schema，采集后直接可用 callstack 分析，无需单独跑 Time Profiler

### 深度性能分析 (Phase 4.4 — 5 个新模块)

- **dSYM 符号化** (`perf symbolicate`) — 自动从 Xcode DerivedData / Spotlight 查找 dSYM，批量 atos 符号化业务代码调用栈，Swift demangling，JSON 缓存避免重复解析
- **syslog-xctrace 时序对齐** (`perf time-sync`) — 自动计算 syslog 与 xctrace 时间轴 offset，将日志事件与性能指标对齐到统一时间线，±N 秒窗口事件归因（OOM 前 CPU/内存/功耗变化）
- **深度 Schema 采集** (`perf deep-export`) — 一键导出 GPU Frame Time / Network Flow / VM Tracking / Metal Performance 四维深度数据，自动探测可用 schema，iterparse 流式解析，P50/P95/P99 统计
- **进程级功耗归因** (`perf power-attr`) — 按 CPU% 比例将 SystemPowerLevel 总功耗分摊到各进程，回答"到底是谁在耗电"。支持**多维归因** (CPU/GPU/Network 子系统级拆分)，进程树跟踪 + 僵尸进程/功耗飙升/内存增长异常检测
- **AI 辅助诊断** (`perf ai-diag`) — 全 session 数据喂 LLM 生成诊断报告，支持 5 种 focus (general/webkit/power/memory/gpu)，回归分析 (before vs after)，WebKit 专项报告，离线模式输出 prompt

### 连接可靠性 + Release 分析 (Phase 4.5)

- **子进程自动重连** (`ReconnectableMixin`) — 通用重连 mixin，指数退避 (2s→4s→8s→16s→30s cap) + 随机抖动，可中断 sleep 响应 stop 信号，断连/重连事件自动 mark_event
- **LiveLogAnalyzer 自动重连** — idevicesyslog 进程退出后自动重连，双层循环架构（外层重连调度 + 内行读取），get_summary 包含 reconnect 统计信息
- **Release 包 dSYM 自动发现** — 5 级搜索策略: DerivedData → Xcode Archives → Spotlight UUID → 设备 UUID 提取 → App Store Connect API 下载，支持 xcodebuild -downloadDsyms 和 JWT 认证
- **WebKit PID 动态刷新** — 每 N cycle 自动重新扫描 WebContent PID，PID 变化时记录事件并迁移采集目标，连续 3 次未找到自动停止 daemon
- **binary UUID 提取** — `extract_binary_uuid()` 用 dwarfdump 从 App 二进制提取 UUID，用于 dSYM 精确匹配

### 架构分层 + 实时方法定位 (Phase 5 — v0.4.0)

**架构重构** — `src/` 从平铺单层演化为 4 层 + perf 内部 6 层:

```
src/
├── app/            ① 应用入口 (CLI argparse + 命令分发)
├── application/    ② 编排与工作流 (orchestration / worker / merge / review / validation / context_extraction)
├── domain/         ③ 领域模型 (Task / ProjectConfig / WorkerResult — 纯数据无行为)
├── infrastructure/ ④ 外部设施 (claude/ input/ storage/ monitoring/ dashboard/ 适配器)
└── perf/           ⑤ perf 子系统 (内部 6 层)
    ├── protocol/   与设备/协议通信 (reconnect / dvt / device)
    ├── capture/    原始数据采集 (sampling / webcontent / live_log / live_metrics)
    ├── decode/     Schema/XML 解析 (templates / timeprofiler / deep_export / time_sync)
    ├── locate/     符号定位层 (linkmap / atos / cache / resolver / dsym)
    ├── analyze/    高层分析 (power_attribution / ai_diagnosis / callstack / metrics / syslog_stats)
    └── present/    呈现 (report_html / dvt_metrics)
```

依赖方向: `app → application → domain ← infrastructure`; perf 只依赖 domain + infrastructure。

**实时方法定位统一入口** — `SymbolResolver` 三层查询:

```
addr ─► ① LRU cache (0.01ms) ─► ② LinkMap bisect (0.1ms)
                                          ↓ miss
                                   ③ atos daemon (2ms 常驻 stdin/stdout 进程池)
                                          ↓ miss
                                   ④ hex 兜底 (500ms timeout 保险)
```

- **常驻 atos daemon** (`src.perf.locate.atos.AtosDaemon`) — 消除每次 subprocess 500ms 启动开销,线程安全 lookup,5 次连续失败自动黑名单
- **LRU cache 持久化** (`src.perf.locate.cache.SymbolCache`) — OrderedDict + 原子 JSON 落盘,session 间恢复命中
- **In-cycle 符号化** — sampling cycle 的 `aggregate_top_n` 后立即批量 resolve,JSONL 每条带 `source` 字段 (`cache | linkmap | atos | unresolved`)
- **xctrace overhead 过滤** — `aggregate_top_n(filter_overhead=True)` 剔除 `dyld3::findClosestSymbol` 等 xctrace 自身采样开销 (实测饱和 Top-10 达 45%),让业务热点真正可见

**新增 CLI 旗标** (激活 resolver):

```bash
# perf start 子命令
cpar perf start --binary PATH --linkmap PATH --dsym PATH ...

# run --with-perf 主流程 (同样生效)
cpar run tasks.yaml --with-perf \
  --perf-binary PATH --perf-linkmap PATH --perf-dsym PATH ...
```

**使用示例** (iOS arm64 app 标准基址 `0x100000000`):

```bash
# Debug build: 传 .app 目录, resolver 自动选内部 .debug.dylib (launcher stub 无符号)
cpar perf start --repo ~/SoulApp --tag live \
  --device UDID --attach Soul_New \
  --sampling --metrics-source device \
  --binary ~/Library/Developer/Xcode/DerivedData/Soul_New-*/Build/Products/Debug-iphoneos/Soul_New.app \
  --linkmap ~/Library/Developer/Xcode/DerivedData/Soul_New-*/Build/Intermediates.noindex/Soul_New.build/Debug-iphoneos/Soul_New.build/Soul_New-LinkMap-normal-arm64.txt

# 多 LinkMap 叠加 (主 + Extensions, 提升命中率)
cpar perf start ... \
  --binary ~/Soul_New.app \
  --linkmap ~/Soul_New-LinkMap.txt \
  --linkmap ~/SoulShareExtension-LinkMap.txt \
  --linkmap ~/SoulWidgetExtension-LinkMap.txt

# Release build: 传主 binary 原样使用 (无 .debug.dylib)
cpar perf start ... --binary ~/Soul.app/Soul --linkmap ~/Soul-LinkMap.txt

# 查看业务符号已命中的 Top-N (自动过滤 xctrace dyld overhead)
cpar perf hotspots --repo ~/SoulApp --tag live --aggregate
```

**iOS Debug vs Release 差异** (resolver 自动识别):

| Build 类型 | `<App>.app/<App>` | `<App>.debug.dylib` | 推荐传 `--binary` |
|---|---|---|---|
| Debug | launcher stub (~50 KB, 无业务符号) | 完整代码 (百 MB 级) | `<App>.app` 目录 — 自动选 dylib |
| Release | 完整代码 | 不存在 | `<App>.app/<App>` 主 binary |

```bash
# 符号化业务代码调用栈 (自动搜索 dSYM: DerivedData → Archives → ASC)
cpar perf symbolicate --repo ~/SoulApp --app-id com.soulapp.Soul
cpar perf symbolicate --repo ~/SoulApp --app-id com.soulapp.Soul --app-name Soul

# Release 包: 从 Archive + ASC 搜索 dSYM
cpar perf symbolicate --repo ~/SoulApp --app-id com.soulapp.Soul \
  --asc-api-key KEY_ID --asc-issuer ISSUER --asc-key-path AuthKey.p8

# syslog 与 xctrace 时间对齐
cpar perf time-sync --repo ~/SoulApp --tag perf --window 5

# 导出 GPU/网络/内存/Metal 深度数据
cpar perf deep-export --repo ~/SoulApp --tag perf --schemas gpu,network,vm,metal

# 查看各进程功耗归因排行
cpar perf power-attr --repo ~/SoulApp --tag perf

# AI 诊断 (离线 prompt / 在线 LLM / WebKit 专项 / 回归分析)
cpar perf ai-diag --repo ~/SoulApp --tag perf --focus webkit --offline
cpar perf ai-diag --repo ~/SoulApp --tag after --baseline-tag before
```

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
cpar validate tasks.yaml
```

### 3. 预览计划

```bash
cpar plan tasks.yaml
```

### 4. 执行

```bash
# 执行 + 合并 + 清理
cpar run tasks.yaml --merge --clean

# 带预算控制
cpar run tasks.yaml --retry 3 --total-budget 5.0
```

### 5. 查看变更

```bash
# 预览所有变更 (不合并)
cpar diff tasks.yaml

# Code Review
cpar review tasks.yaml --budget 1.0
```

### 6. 中断恢复

```bash
cpar resume tasks.yaml --merge
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
cpar run tasks.yaml --with-perf \
  --perf-device UDID --perf-attach Soul_New \
  --perf-sampling --perf-metrics-source auto
```

## 完整命令列表

共 32 个命令: 7 执行 + 3 运维 + 22 性能分析。`perf` 与 `analyze` 等价。

### 执行类

```
run <YAML>       执行任务 (--dry, --merge, --clean, --retry N, --total-budget $,
                 --with-perf, --perf-tag, --perf-device, --perf-attach,
                 --perf-duration, --perf-templates, --perf-baseline,
                 --perf-threshold-pct, --strict-perf-gate, --perf-sampling,
                 --perf-metrics-source auto|device|xctrace, --perf-composite,
                 --perf-binary, --perf-linkmap, --perf-dsym,
                 --web-dashboard, --web-port)
resume <YAML>    从中断处恢复 (参数同 run)
plan <YAML>      展示执行计划 (DAG 层级图)
merge <YAML>     合并 worktree (冲突自动解决)
diff <YAML>      预览所有 worktree 变更
review <YAML>    Code Review (--budget $)
validate <YAML>  校验 YAML 配置 (--with-perf, --perf-device, --perf-attach)
```

### 运维类

```
clean <repo>       清理 worktree + cp-* 分支 + 协调目录
                   (--prune-logs, --keep-days N, --keep-last N, --force)
logs <repo>        查看任务日志 (--task/-t ID, --tail/-n N)
dashboard          启动独立 Web Dashboard 浏览器实时看调度+功耗
                   (--repo, --tag, --port, --host, --no-open, --source NAME=PATH)
```

### 性能分析类 (perf / analyze)

`cpar perf <子命令>` 和 `cpar analyze <子命令>` 完全等价。

```
── 采集控制 ──────────────────────────────────────────────────
  start         启动真机性能采集 (--device, --attach, --tag, --duration,
                --sampling, --attach-webcontent, --composite, --binary,
                --linkmap, --dsym, --no-tunneld, --metrics-source,
                --metrics-interval, --battery-interval)
  stop          停止采集 (--clean, --no-clean, --keep-report)
  tail          查看实时 syslog (--lines N)
  devices       列出 xctrace 可用设备

── 报告与分析 ────────────────────────────────────────────────
  report        生成 perf 报告 (--with-callstack, --callstack-top N,
                --json, --html, --html-output, --baseline, --threshold-pct,
                --clean, --no-clean, --keep-report)
  live          实时 syslog 告警分析 (--device, --rules, --buffer, --interval)
  rules         列出/管理告警规则 (--list, --export, --test)
  stream        实时 xctrace 指标流 (<trace>, --interval, --window)
  snapshot      立即导出指标快照 (<trace>, --json)

── 进程级分析 ────────────────────────────────────────────────
  metrics       Per-process CPU/内存指标 (--last N, --json)
  battery       电池功耗趋势 (--last N, --json)
  hotspots      运行时热点函数 (--follow, --top N, --last N, --aggregate, --json)
  callstack     Time Profiler 调用栈分析 (--top N, --min-weight, --max-depth,
                --no-flatten, --full-stack, --from SEC, --to SEC, --json)
  webcontent    WebContent 进程 JS/WebKit 热点 (--top N, --last N, --json)
  power-attr    进程级功耗归因分析 (--json)
  dashboard     全指标统一仪表盘 时序表+汇总 (--last N, --json, --csv)

── 符号化与调试 ──────────────────────────────────────────────
  linkmap       LinkMap 解析 (find|parse|lookup|search|warm|bench)
                (--project, --arch, --max N, --json)
  symbolicate   dSYM 符号化调用栈地址 (--app-id, --dsym, --uuid, --arch,
                --top N, --json)

── 时间关联与深度采集 ────────────────────────────────────────
  time-sync     syslog-xctrace 时序对齐 + 事件归因 (--syslog, --window N, --json)
  deep-export   深度 Schema 采集 GPU/Network/VM/Metal (--schemas, --json)

── 基础设施 ──────────────────────────────────────────────────
  tunneld       管理 pymobiledevice3 RemoteXPC tunneld (iOS 17+ DVT 必需)
                (start|stop|status|ensure, --no-prompt)
  config        查看/修改 perf 默认配置 (show | set FIELD VALUE | unset FIELD)
  templates     Instruments 模板管理 (--list, --available, --devices,
                --build-cmd, --device, --attach, --duration)

── AI 辅助 ───────────────────────────────────────────────────
  ai-diag       AI 辅助性能诊断 (--focus general|webkit|power|memory|gpu,
                --baseline-tag, --offline, --model, --json)
```

## 文件结构

```
claude-parallel/
├── run.py                      # CLI 入口
├── chat.py                     # 对话模式入口
├── cpar                        # Shell 封装脚本
├── src/
│   ├── app/                    ① 应用入口
│   │   ├── cli.py              # 主 CLI: argparse + 命令分发 (~521 行)
│   │   ├── execution_cli.py    # 执行子命令注册 (run/resume/plan/merge/diff/review/validate)
│   │   ├── ops_cli.py          # 运维子命令 (clean/logs/dashboard)
│   │   └── perf_cli.py         # ★ 所有 perf/analyze 子命令实现 (~1872 行)
│   ├── application/            ② 编排与工作流
│   │   ├── orchestration.py    # Orchestrator 调度器 (DAG/重试/预算/恢复)
│   │   ├── worker.py           # Worker 进程管理 + 重试 + 日志
│   │   ├── merge.py            # WorktreeMerger 合并 + 冲突解决
│   │   ├── review.py           # 自动 Code Review
│   │   ├── validation.py       # YAML 配置校验
│   │   └── context_extraction.py # 多语言上下文提取
│   ├── domain/                 ③ 领域模型 (纯数据)
│   │   └── tasks.py            # Task/ProjectConfig/WorkerResult 数据类
│   ├── infrastructure/         ④ 外部设施
│   │   ├── claude/             # Claude CLI 统一调用 + 重试
│   │   ├── storage/atomic.py   # 原子 JSON 读写
│   │   ├── monitoring/         # Rich Live 进度面板
│   │   └── dashboard/          # Web 仪表盘 (WebSocket + 源码定位)
│   └── perf/                   ⑤ perf 子系统 (内部 6 层)
│       ├── protocol/           # 设备/协议通信 (reconnect / dvt / device)
│       ├── capture/            # 原始数据采集 (sampling / webcontent / live_log / live_metrics)
│       ├── decode/             # Schema/XML 解析 (templates / timeprofiler / deep_export / time_sync)
│       ├── locate/             # 符号定位层 (linkmap / atos / cache / resolver / dsym)
│       ├── analyze/            # 高层分析 (power_attribution / ai_diagnosis / callstack / metrics)
│       └── present/            # 呈现 (report_html / dvt_metrics)
├── tests/
│   ├── test_chat_input.py      # 输入模块测试
│   ├── test_perf_sampling.py   # Sampling + 解析器测试 (29 cases)
│   └── test_device_metrics.py  # Device 指标测试 (15 cases)
├── examples/                   # YAML 示例
├── docs/
│   ├── ARCHITECTURE.md         # 整体架构文档
│   ├── QUICKSTART.md           # 快速上手模板
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
