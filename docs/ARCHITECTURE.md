# claude-parallel 整体架构

> 版本: v0.3.1 | 代码量: 16,976 行 | 20 个 Python 模块 (perf: 17 模块, 11,087 行)
> 分支: main | 最新 22 commits 涵盖 perf 全链路增强

---

## 1. 系统全景

```
┌─────────────────────────────────────────────────────────────────────┐
│                          用户入口                                    │
│   cpar chat -i/-m/pipe   │  cpar run/resume/plan/validate  │  cpar perf │
│   (对话模式 → YAML)      │  (直接执行 YAML)                │  (性能分析) │
└────────┬─────────────────┴──────────┬─────────────────────┴──────┬───┘
         │                            │                             │
         v                            v                             v
┌─────────────┐            ┌──────────────────┐          ┌──────────────────┐
│  chat.py    │            │   cli.py         │          │  cli.py perf *   │
│  +chat_input│──YAML────>│   (路由 + 子命令)  │          │  (15个子命令)     │
└─────────────┘            └───┬──────────────┘          └──────┬───────────┘
                               │                                 │
              ┌────────────────┼────────────────┐                │
              v                v                v                v
     ┌─────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐
     │ validator   │  │ orchestrator │  │ merger/      │  │ PerfIntegrator   │
     │ (DAG/类型/  │  │ (DAG调度+    │  │ reviewer     │  │ (perf ↔ cpar)    │
     │  环检测)    │  │  重试+budget) │  │ (合并+review) │  │                  │
     └─────────────┘  └──┬───┬───┬───┘  └──────────────┘  └──┬──────┬───────┘
                         │   │   │                           │      │
              ┌──────────┘   │   └──────────┐               │      │
              v              v              v               v      v
     ┌──────────────┐ ┌────────────┐ ┌──────────────┐ ┌─────────┐ ┌──────────┐
     │   worker.py  │ │ monitor.py │ │ context_     │ │ perf/   │ │ perf/    │
     │ (Claude Code │ │ (Rich Live │ │ extractor.py │ │ session │ │ live_*   │
     │  子进程管理)  │ │  实时面板)  │ │ (多语言接口)  │ │         │ │          │
     └──────┬───────┘ └────────────┘ └──────────────┘ └────┬────┘ └────┬─────┘
            │                                          │           │
            v                                          v           v
     ┌──────────────┐                          ┌──────────────┐ ┌──────────────┐
     │ claude -p    │                          │ xctrace      │ │ idevicesyslog│
     │ (CLI agent)  │                          │ (Instruments)│ │ (设备日志)    │
     │ + git worktree│                          │ + sampling   │ │ + battery    │
     └──────────────┘                          │ + templates  │ │ + metrics    │
                                               └──────────────┘ └──────────────┘
```

---

## 2. 模块职责一览

### 2.1 入口 & 对话层

| 模块 | 行数 | 职责 |
|------|------|------|
| `run.py` | 6 | 最外层入口，转发到 `src.cli.main()` |
| `cpar` | 45 | Shell wrapper 脚本，设置 PYTHONPATH |
| `chat.py` | 906 | 对话模式：自然语言需求 → Claude 规划 → YAML 生成 → 校验 → 执行 |
| `src/chat_input.py` | 409 | 富输入：多行编辑、历史补全、readline、/undo/ /clear/ /show |

**chat.py 输入模式** (优先级):
1. `cpar chat -i` → InquirerPy 7步向导
2. `cpar chat -m "需求"` → 直接传参
3. `cpar chat --message-file` / pipe → 文件/管道
4. `cpar chat` → 自由输入 + /done 提交

**chat.py 规划降级链** (3层):
```
正常调用 → budget×1.8重试 → compact prompt → hard constraint → 失败
```

### 2.2 核心调度层

| 模块 | 行数 | 核心类 | 职责 |
|------|------|--------|------|
| `src/cli.py` | 1,476 | `main()` | argparse 路由：run/resume/plan/merge/diff/review/validate/clean/logs/perf |
| `src/orchestrator.py` | 757 | `Orchestrator`, `RunStats`, `BudgetExceeded` | DAG 分层调度、重试+退避、budget 控制、resume、perf 集成 |
| `src/worker.py` | 524 | `Worker`, `WorkerResult`, `WorkerLogger` | Claude Code 子进程启停、JSON 输出解析、错误提取、流式日志 |
| `src/monitor.py` | 396 | `Monitor` | Rich Live 实时进度面板（任务状态+进度条+耗时+费用） |

**Orchestrator 生命周期**:
```
__init__ → load YAML → build DAG →
  for each level:
    level_start →
      for each task (parallel, max_workers):
        Worker.run() → on_complete → context extraction
    → level_end
  → (optional) merge → (optional) review → stats summary
```

### 2.3 DAG & 校验层

| 模块 | 行数 | 核心类 | 职责 |
|------|------|--------|------|
| `src/task_parser.py` | 184 | `Task`, `ProjectConfig`, `topological_levels()` | YAML 解析 + DAG 拓扑排序 |
| `src/validator.py` | 460 | `TaskValidator` | 前置校验：语法/类型/环检测/悬空引用/工具白名单/xctrace可用性 |
| `src/context_extractor.py` | 221 | `extract_context_for_downstream()` | 多语言接口提取（Python/JS/TS/Go/Rust/Java） |

### 2.4 合并 & 评审层

| 模块 | 行数 | 核心类 | 职责 |
|------|------|--------|------|
| `src/merger.py` | 590 | `WorktreeMerger`, `MergeReport` | cherry-pick 合并 + Claude 辅助冲突解决 + 子模块 patch |
| `src/reviewer.py` | 439 | `CodeReviewer`, `ReviewResult` | Claude 自动 Code Review（all worktree diffs） |

### 2.5 Perf 性能采集子系统 (src/perf/)

| 模块 | 行数 | 核心类 | 职责 |
|------|------|--------|------|
| `config.py` | 39 | `PerfConfig` | 采集配置 dataclass |
| `reconnect.py` | 182 | `ReconnectableMixin`, `ReconnectPolicy`, `ReconnectStats` | 通用子进程断连自动重连 (指数退避+抖动+可中断sleep) |
| `session.py` | 819 | `PerfSessionManager` | 生命周期：syslog + xctrace + sampling + battery + timeline + report + callstack |
| `live_log.py` | 632 | `LiveLogAnalyzer`, `LogRule` | 实时 syslog 流式分析 + 13 条内置告警规则 + 自动重连 |
| `live_metrics.py` | 608 | `LiveMetricsStreamer`, `MetricSnapshot` | 实时 xctrace 指标流（边录边导出 + 滚动窗口） |
| `sampling.py` | 1,042 | `SamplingProfilerSidecar` | Time Profiler 短周期旁路 + XML 解析 + 聚合 |
| `device_metrics.py` | 355 | `BatteryPoller`, `ProcessMetricsStreamer` | 非 xctrace 通道的电池 + 进程指标 |
| `templates.py` | 317 | `TemplateLibrary`, `InstrumentTemplate` | 10 内置 Instruments 模板 + 自定义模板注册 |
| `integrator.py` | 386 | `PerfIntegrator` | perf ↔ Orchestrator 集成胶水（生命周期钩子） |
| `symbolicate.py` | 1,139 | `auto_symbolicate`, `find_dsym_*` | dSYM 符号化 + 5 级自动搜索 (DerivedData→Archives→Spotlight→设备→ASC) |
| `time_sync.py` | 857 | `TimeSyncAnalyzer` | syslog-xctrace 时间轴对齐 + ±N 秒窗口事件归因 |
| `deep_export.py` | 855 | `DeepSchemaExporter` | GPU/Network/VM/Metal 四维深度数据导出 |
| `power_attribution.py` | 1,118 | `PowerAttributor` | 进程级功耗归因 + 异常检测 |
| `ai_diagnosis.py` | 1,260 | `AIDiagnoser` | AI 辅助诊断 (5种focus+回归分析+离线prompt) |
| `webcontent.py` | 579 | `WebContentProfiler` | WebKit 进程 Time Profiler + PID 动态刷新 |
| `dvt_bridge.py` | 692 | `DVTBridge` | pymobiledevice3 DVT 协议桥接 (per-process 指标) |

---

## 3. 数据流

### 3.1 主执行流程

```
YAML 文件 ──parse──> Task[] + DAG levels
                           │
                     ┌─────┴─────┐
                     │ validate  │ ← 类型/环/工具白名单
                     └─────┬─────┘
                           │
                     ┌─────┴─────┐
                     │  plan     │ ← 展示 DAG 层级 (dry run)
                     └─────┬─────┘
                           │
                  ┌────────┴────────┐
                  │   Orchestrator  │
                  │  (异步事件循环)  │
                  └──┬───┬───┬───┬─┘
                     │   │   │   │  (每层并行，层间串行)
                 ┌───┘   │   │   └───┐
                 v       v   v       v
              Worker   Worker Worker Worker
              (wt-1)   (wt-2) (wt-3) (wt-4)
                 │       │     │       │
                 v       v     v       v
              claude   claude claude claude
              -p "..."  (git worktree 隔离)
                 │
                 ├──> .claude-parallel/coord/<id>.STATUS
                 ├──> .claude-parallel/coord/<id>.result
                 ├──> .claude-parallel/logs/<id>.log
                 └──> .claude-parallel/context/<id>.md
                          │
                     ┌────┴────┐
                     │  diff   │ ← 预览所有 worktree 变更
                     └────┬────┘
                          │
                     ┌────┴────┐
                     │  merge  │ ← cherry-pick + Claude 冲突解决
                     └────┬────┘
                          │
                     ┌────┴────┐
                     │ review  │ ← Claude Code Review
                     └─────────┘
```

### 3.2 Perf 采集流程

```
cpar run --with-perf --perf-device UDID --perf-attach ProcessName
         │
         v
   PerfIntegrator.on_run_start()
         │
         ├──> PerfSessionManager.start()
         │      ├── idevicesyslog → syslog_full.log (后台子进程)
         │      ├── xctrace record → power.trace (后台子进程, 多模板并行)
         │      ├── BatteryPoller → battery.jsonl (独立子进程)
         │      └── ProcessMetricsStreamer → process_metrics.jsonl (可选)
         │
         ├──> LiveLogAnalyzer.start()     ← 实时 syslog 告警 (13规则)
         ├──> LiveMetricsStreamer.start() ← 实时 xctrace 指标流
         └──> SamplingProfilerSidecar     ← Time Profiler 旁路 (可选)
                │
                ├──> on_level_start() → 打印告警摘要 + 指标快照
                ├──> on_task_done()   → timeline 打点
                ├──> on_level_end()   → 热点函数快照
                │
         v
   PerfIntegrator.on_run_end()
         │
         ├──> stop 所有采集器
         ├──> export xctrace schemas (并行)
         ├──> parse + aggregate
         └──> report.json (含 metrics + timeline + callstack + baseline + gate)
```

---

## 4. WebKit 分析能力

### 4.1 已有 WebKit 支持 (live_log.py)

13 条内置告警规则中，有 **3 条专门针对 WebKit**:

| 规则名 | 级别 | 匹配模式 | 说明 |
|--------|------|----------|------|
| `webkit_crash` | critical | `WebKit.*crash\|WebProcess.*exit\|...` | WebKit 进程崩溃 |
| `webkit_oom` | critical | `WebProcess.*jetsam\|WebKit.*OOM\|WebContent.*killed` | WebKit 内存被杀 |
| `webkit_network` | warn | `WebKit.*network.*error\|WKWebView.*load.*fail\|NSURLError` | WebKit 网络错误 |

### 4.2 已有的 Native 堆栈分析 (sampling.py + session.py)

**Time Profiler 调用栈解析能力**:
- 支持 Xcode 16+ `time-profile` schema（含 `<backtrace><frame name="...">`）
- 支持 Legacy `TimeProfiler` schema（含 `<symbol-name>/<sample-count>`）
- 使用 `iterparse` 单遍解析，内存恒定
- 支持完整调用链（`keep_full_stack=True`）和叶子函数聚合（`flatten=True`）
- 支持时间切片过滤（`time_range`）
- `_is_symbolicated()` 检测符号化状态（过滤 `0x` 开头的未符号化地址）

**调用栈输出格式**:
```python
{
  "hot_functions": [{"symbol": "function_name", "samples": N, "weight_pct": P}],
  "call_paths": [{"frames": ["A → B → C"], "depth": 3, "weight_pct": P}],
  "summary": {"unique_symbols": M, "top_symbol": "...", "top_weight_pct": P}
}
```

---

## 5. 关键发现：WebKit/Native 堆栈的覆盖情况

### 5.1 已覆盖 (现有代码能做什么)

| 能力 | 实现位置 | 说明 |
|------|----------|------|
| WebKit 进程崩溃/崩溃检测 | `live_log.py` 规则 | syslog 正则匹配 |
| WebKit OOM/Jetsam 检测 | `live_log.py` 规则 | syslog 正则匹配 |
| WebKit 网络错误检测 | `live_log.py` 规则 | syslog 正则匹配 |
| Native 函数热点排名 | `sampling.py` aggregate_top_n | Time Profiler 叶子函数聚合 |
| Native 调用路径 | `session.py` callstack() | `A → B → C` 格式 |
| 时间切片 | `parse_timeprofiler_xml(time_range=)` | 指定时间段过滤 |
| 符号化检测 | `_is_symbolicated()` | 区分 0x 地址 vs 符号名 |
| Display/CPU/Network 功耗 | `live_metrics.py` + session | SystemPowerLevel + ProcessSubsystemPowerImpact |
| 热状态追踪 | `live_metrics.py` SCHEMA_COLUMNS | device-thermal-state-intervals |

### 5.2 未覆盖 (缺失能力)

| 缺失 | 说明 | 影响 |
|------|------|------|
| **dSYM 符号化** | 没有调用 `atos` 或 `dsymutil`，依赖 xctrace 自带符号化 | 如果 trace 中只有 `0x` 地址，则 `_is_symbolicated()` 直接跳过，丢失该采样 |
| **WebKit JS 调用栈** | Time Profiler 在 Web Content Process 中采集到的是 C/C++ 层（JIT编译后的帧是 `bmalloc::...` / `JSC::...`），不是 JS 函数名 | 无法直接关联到具体 JS 代码行 |
| **WebKit 专项聚合** | 没有按进程（WebContent vs 主App）分组聚合调用栈 | WebKit 的 CPU 开销和 App native 开销混在一起，无法分离 |
| **网络 endpoint 关联** | `network-connection-stat` schema 没有在 `live_metrics.py` 的 SCHEMA_COLUMNS 中定义 | 无法在实时流中看到 WebKit 请求了哪些远程 endpoint |
| **GPU/渲染帧关联 WebKit** | CoreAnimationFPS 采集了帧率但没有关联到具体 WKWebView 实例 | 无法区分哪个 WebView 导致掉帧 |
| **进程级功耗分离** | ProcessSubsystemPowerImpact 只有 CPU/Networking 分类，没有按子进程（WebContent）分离 | 无法直接看到 WebKit 独立功耗 |

### 5.3 结论

**现有代码架构已经包含了 WebKit 检测和 Native 调用栈的基础能力**:

1. **WebKit 层面**: syslog 实时告警覆盖了崩溃/OOM/网络三类关键事件。但这是**日志级**检测，不是**代码级**归因。
2. **Native 堆栈层面**: Time Profiler 的解析链路完整（iterparse → 聚合 → 调用路径），可以获取到 ObjC/C/C++ 层的函数名和调用关系。但**缺少 dSYM 符号化步骤和 WebKit 进程过滤**。

**要用 cpar 的现有 perf 子系统做 WebKit 功耗分析，真实效果是**:
- 能看到 "WebContent" 进程的 CPU 热点（如果 xctrace 自带符号化成功）
- 能看到 WebKit 崩溃/OOM 的实时告警
- 能看到 Display/CPU/Networking 的整体功耗趋势
- **看不到**: WebKit JS 层具体哪个函数消耗大、哪个 WKWebView 实例导致的掉帧、WebKit 和 Native 的功耗比例分离

---

## 6. 目录结构

```
~/claude-parallel/
├── run.py              # 入口 (6行, 转发到 src.cli)
├── cpar                # Shell wrapper (45行)
├── chat.py             # 对话模式 (906行)
├── README.md
├── ANALYSIS.md
├── src/
│   ├── __init__.py
│   ├── cli.py          # CLI 路由 + perf 21个子命令 (1,476行)
│   ├── orchestrator.py # DAG 调度器 (757行)
│   ├── worker.py       # Claude Code 子进程 (524行)
│   ├── monitor.py      # Rich Live 面板 (396行)
│   ├── task_parser.py  # YAML + 拓扑排序 (184行)
│   ├── validator.py    # 前置校验 (460行)
│   ├── context_extractor.py  # 多语言接口提取 (221行)
│   ├── merger.py       # cherry-pick 合并 (590行)
│   ├── reviewer.py     # Code Review (439行)
│   ├── chat_input.py   # 富输入 (409行)
│   ├── fs_utils.py     # 原子写/安全读 (52行)
│   └── perf/           # 性能采集子系统 (17 模块, 11,087 行)
│       ├── __init__.py       # 公共导出 (207行)
│       ├── config.py         # PerfConfig (39行)
│       ├── reconnect.py      # ReconnectableMixin 自动重连 (182行)
│       ├── session.py        # 会话管理 (819行)
│       ├── live_log.py       # syslog 实时分析 + 自动重连 (632行)
│       ├── live_metrics.py   # xctrace 指标流 (608行)
│       ├── sampling.py       # Time Profiler 旁路 (1,042行)
│       ├── webcontent.py     # WebKit 进程分析 + PID 刷新 (579行)
│       ├── dvt_bridge.py     # DVT 协议桥接 (692行)
│       ├── symbolicate.py    # dSYM 符号化 + 5 级搜索 (1,139行)
│       ├── time_sync.py      # 时序对齐 (857行)
│       ├── deep_export.py    # 深度 Schema 导出 (855行)
│       ├── power_attribution.py # 功耗归因 (1,118行)
│       ├── ai_diagnosis.py   # AI 诊断 (1,260行)
│       ├── device_metrics.py # 电池+进程指标 (355行)
│       ├── templates.py      # Instruments 模板 (317行)
│       └── integrator.py     # Orchestrator 集成 (386行)
├── docs/
│   ├── ARCHITECTURE.md  # 整体架构文档
│   ├── QUICKSTART.md    # 快速上手模板
│   ├── playbook.md
│   └── plans/           # 设计文档 (5份)
├── examples/            # 样例 YAML (8份)
├── tests/               # 单元测试
├── scripts/             # 辅助脚本
└── tasks/               # 任务文件
```

---

## 7. 依赖关系图

```
chat.py ──> validator.py, chat_input.py
cli.py ──> orchestrator.py, validator.py, perf/*
orchestrator.py ──> worker.py, monitor.py, merger.py, context_extractor.py, perf/*
worker.py ──> task_parser.py, fs_utils.py
merger.py ──> task_parser.py, worker.py
reviewer.py ──> task_parser.py, worker.py

perf/session.py ──> config.py, device_metrics.py, sampling.py, templates.py, webcontent.py, fs_utils
perf/integrator.py ──> config.py, session.py, live_log.py, live_metrics.py, sampling.py, templates.py
perf/live_log.py ──> reconnect.py (ReconnectableMixin)
perf/live_metrics.py ──> (独立, 只依赖 subprocess + threading)
perf/sampling.py ──> templates.py
perf/device_metrics.py ──> (独立, 只依赖 subprocess + plistlib)
perf/symbolicate.py ──> (独立, 只依赖 subprocess + dwarfdump + atos)
perf/time_sync.py ──> (独立, 只依赖 json + pathlib)
perf/deep_export.py ──> templates.py
perf/power_attribution.py ──> (独立, 只依赖 json + pathlib)
perf/ai_diagnosis.py ──> session.py, symbolicate.py, time_sync.py, deep_export.py, power_attribution.py
perf/webcontent.py ──> templates.py (PID 刷新: 自包含 find_webcontent_pids)
perf/dvt_bridge.py ──> (独立, pymobiledevice3 可选)
perf/reconnect.py ──> (独立 mixin, 只依赖 threading + time + random)
```
