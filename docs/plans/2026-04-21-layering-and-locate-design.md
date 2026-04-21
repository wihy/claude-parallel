# 架构分层整理 + perf 实时方法定位优化 — 设计文档

- **日期**: 2026-04-21
- **作者**: brainstorm (chunhaixu + Claude)
- **状态**: Design approved, pending implementation
- **输出形态**: 蓝图 + 分阶段路线图（不在本文档中写代码）

---

## §1 目标与非目标

### 本次重构要解决的问题

1. **架构分层未完成**：服务层（orchestrator/worker/merger/reviewer/validator/context_extractor/claude_client）平铺在 `src/` 根，与已建立的 `app/domain/infrastructure` 三层脱节，方向感混乱。
2. **perf/ 是飞地**：17 模块平铺，`session.py` 扇出 6 个兄弟成为事实上的 god-module，内部没有"采集/解析/定位"分层。
3. **实时方法定位"可见但不可读"**：sampling cycle 输出的是十六进制地址（`symbol=0xDEADBEEF`），符号化是后置批处理，用户需要手动跑 `perf symbolicate` 才能看到业务函数名。
4. **符号化三路径无统一入口**：`symbolicate.py`(dSYM+atos) + `linkmap.py` + `resymbolize.py` 各自独立，`resymbolize` 只是事后补丁，不在默认管道里。

### 成功标准（定量）

**架构**
- A1: `src/` 根只剩 `__init__.py`，所有服务层代码归到 `application/`
- A2: `perf/` 切成 `protocol/capture/decode/locate/analyze/present/session.py`，session 降至 <400 行纯编排
- A3: 5 个 shim 文件全删，`perf/__init__.py` 无 `from .x import *`

**Locate**
- B1: `cpar perf hotspots --follow` 默认显示业务函数名（命中率 >90%），无需手动 symbolicate
- B2: sampling cycle 时延增加 <10%（当前 ~8s，目标 <9s）
- B3: `SymbolResolver` 单一入口，旧 3 路径成其内部策略

### 明确不做

- 不拆成多个 Python 包 / monorepo
- 不改 CLI 子命令名、不改 `cpar` / `run.py` / `chat.py` 入口
- 不引入 import-linter 等强约束工具（分层规则靠代码评审 + README 说明）
- 不做 perf 测试覆盖率的全面补齐（保持现有 3 个测试文件；仅为 locate 层新增必要 unit test）

### 兼容性约束

- ✅ 保留 CLI 名 (`cpar`、`cpar perf <sub>` 全兼容)
- ✅ 保留 `run.py` / `chat.py` / `cpar` 入口
- ✅ 保留 `from src.perf import PerfConfig, ...` 主要公共入口
- 🔧 **删除** 5 个 shim（内部调用方改走新路径）
- 🔧 **收窄** `perf/__init__.py` 的 `*` import，改为显式列名

---

## §2 目标分层骨架（Track A 全景）

### 目标目录树

```
src/
├── __init__.py
│
├── app/                           # ① 应用入口（已有）
│   ├── cli.py                     # main + banner + report print
│   ├── execution_cli.py           # run/resume/plan/merge/diff/review/validate
│   ├── ops_cli.py                 # clean/logs/dashboard
│   ├── perf_cli.py                # perf 23 子命令
│   └── chat_cli.py                # 对话模式
│
├── domain/                        # ② 领域模型（纯数据 + 纯函数）
│   ├── tasks.py                   # Task, ProjectConfig, parse_task_file, topological_levels
│   ├── worker_result.py           # ★ 新：从 worker.py 抽出的 WorkerResult dataclass
│   └── perf_session.py            # ★ 新：PerfConfig + PerfSessionMeta 领域数据
│
├── application/                   # ③ 编排与工作流（★ 新增层）
│   ├── orchestration.py           # Orchestrator（原 orchestrator.py 搬入）
│   ├── worker.py                  # Worker, retry_worker, WorkerLogger
│   ├── merge.py                   # WorktreeMerger
│   ├── review.py                  # CodeReviewer
│   ├── validation.py              # TaskValidator
│   └── context_extraction.py      # extract_context_for_downstream
│
├── infrastructure/                # ④ 外部设施（已有 + 扩）
│   ├── claude/                    # ★ 新：claude_client.py 迁入
│   │   └── client.py
│   ├── storage/atomic.py          # 现状
│   ├── monitoring/rich_monitor.py # 现状
│   ├── dashboard/server.py        # 现状
│   └── input/                     # ★ 新：chat_input.py 迁入
│       └── chat_input.py
│
└── perf/                          # ⑤ perf 子系统（见 §3）
```

### 依赖方向规则（靠人工评审守护，不加工具）

```
app  →  application  →  domain
             ↓           ↑
       infrastructure ───┘

perf 视作独立子系统：可被 application 引用；自身只依赖 domain + infrastructure
```

- ❌ domain 不能 import 其他层（纯模型）
- ❌ infrastructure 不能 import application
- ❌ perf 不能 import application（反向依赖会劈回头）
- ✅ application 可以引用 domain、infrastructure、perf

### 删除清单

全部为 1 行 `from .xxx import *` 的 shim：

- `src/cli.py`
- `src/monitor.py`
- `src/task_parser.py`
- `src/fs_utils.py`
- `src/web_dashboard.py`

---

## §3 perf/ 子系统 5 层拆分

```
src/perf/
├── __init__.py                    # 收窄：只导出稳定公共 API（约 15 个名字）
│
├── protocol/                      # ① 与设备/协议通信（无业务语义）
│   ├── reconnect.py               # ReconnectableMixin + ReconnectPolicy
│   ├── dvt.py                     # ← dvt_bridge.py（1046 行）重命名
│   └── device.py                  # ← device_metrics.py；BatteryPoller + ProcessMetrics
│
├── capture/                       # ② 原始数据采集（启 xctrace / idevicesyslog / 循环 sidecar）
│   ├── sampling.py                # 只留采集环 + XML export（符号化剥离，见 ④）
│   ├── webcontent.py              # WebContent PID 刷新
│   ├── live_metrics.py            # xctrace 指标流
│   └── live_log.py                # idevicesyslog 告警
│
├── decode/                        # ③ Schema / XML 解析（无 I/O）
│   ├── timeprofiler.py            # ← sampling.py 抽出：parse_timeprofiler_xml, aggregate_top_n
│   ├── templates.py               # Instruments 模板清单 + build_xctrace_record_cmd
│   ├── deep_export.py             # GPU/Network/VM/Metal schema 解析
│   └── time_sync.py               # syslog-xctrace 时序对齐
│
├── locate/                        # ④ ★ 符号定位层（B 议题核心）
│   ├── resolver.py                # SymbolResolver 统一入口（见 §4）
│   ├── linkmap.py                 # LinkMap 解析 + bisect 反查
│   ├── dsym.py                    # ← symbolicate.py 拆：dSYM 搜索/UUID 匹配/cache
│   ├── atos.py                    # ← symbolicate.py 拆：常驻 atos daemon 进程池
│   └── cache.py                   # address→symbol LRU + 持久化 JSON
│
├── analyze/                       # ⑤ 高层分析（基于 decode + locate 产物）
│   ├── power_attribution.py       # 功耗归因
│   └── ai_diagnosis.py            # LLM 诊断
│
├── present/                       # ⑥ 呈现
│   └── report_html.py
│
├── session.py                     # 纯编排（<400 行）：start/stop/callstack/hotspots
├── integrator.py                  # 与 application/orchestration 的集成胶水
├── perf_defaults.py               # 默认参数常量
└── config.py                      # PerfConfig（未来可移入 domain/perf_session.py）
```

### 内部依赖方向（单向向下）

```
session ── integrator
  ↓
analyze ── present
  ↓
locate ← capture ← decode
            ↓
          protocol
```

### 关键迁移

- `symbolicate.py`(1139 行) → 拆成 `locate/dsym.py` + `locate/atos.py` + `locate/cache.py`
- `resymbolize.py`(142 行) → 合入 `locate/resolver.py` 作为策略之一（不再独立文件）
- `sampling.py`(1169 行) → 保留采集部分；`parse_timeprofiler_xml` / `aggregate_top_n` 迁出到 `decode/timeprofiler.py`（行数降到 ~600）
- `session.py`(1086 行) → 把 export/parse 调用委托给 `decode/`，自己只留生命周期（~380 行）
- `device_metrics.py` 与 `dvt_bridge.py` 指标重叠问题：两者均保留在 `protocol/`，integrator 级别统一选源（`metrics-source` 旗标集中在那里）

---

## §4 Locate 层设计（Track B 核心）

### 统一入口：`SymbolResolver`

位于 `perf/locate/resolver.py`：

```python
class SymbolResolver:
    def __init__(self, binary_path, dsym_paths, linkmap_path, cache_dir):
        self._linkmap = None        # MultiLinkMap，warmup 时加载
        self._atos = None           # AtosDaemon，warmup 时启动
        self._cache = SymbolCache(cache_dir)

    def warmup(self) -> None:
        """session.start() 调用一次：加载 LinkMap + 启 atos daemon + 恢复缓存"""

    def resolve(self, addr: int, *, timeout_ms: int = 500) -> Symbol:
        """单地址同步查询，超时返回 Symbol(hex, source='timeout')"""

    def resolve_batch(self, addrs: list[int]) -> dict[int, Symbol]:
        """批量 —— sampling cycle 聚合后调一次"""

    def shutdown(self) -> None:
        """session.stop() 调用：关 atos daemon、flush 缓存"""
```

### 三层查询策略（resolver 内部，对外单入口）

```
addr ─► ① LRU cache        命中 → 0.01ms
         ↓ miss
        ② LinkMap bisect   命中 → ~0.1ms（业务符号主路径）
         ↓ miss（系统符号 / inline）
        ③ atos daemon      命中 → ~2ms（Swift demangle / inline 帧）
         ↓ timeout 500ms
        ④ hex 兜底          Symbol("0xADDR", source='unresolved')
```

### 关键机制

- **AtosDaemon**（`locate/atos.py`）：启动 `atos -i -o binary -l loadAddr` 常驻子进程，stdin 喂 addr、stdout 读 symbol，消除每次 subprocess 的 500ms 启动开销
- **warmup 策略**：session 启动时后台线程预加载 LinkMap + 启 atos daemon + 从 `cache_dir/symbols.json` 恢复缓存；warmup 未完成时 resolver 降级为 hex 输出（不阻塞 cycle）
- **cache 持久化**：每个 session 独立目录，下次同包同 UUID 命中率近 100%
- **超时保险**：每次 `resolve` 强制 timeout，超时直接返回 hex + 标记，永不阻塞 sampling cycle

### 被删除的实体

- `symbolicate.py`（1139 行）全部功能吸收进 `locate/{dsym,atos,cache}.py`
- `resymbolize.py`（142 行）消失，逻辑在 `resolver.resolve` 的"② LinkMap bisect"步

---

## §5 In-Cycle 符号化集成

### 当前路径（问题）

```
record(10s) → export XML → parse → aggregate_top_n() → 写 JSONL（symbol=0xADDR）
                                                       └─ 用户看到 hex
```

### 目标路径

```
record(10s) → export XML → parse → aggregate_top_n() ─┐
                                                      │
                                resolver.resolve_batch(addrs) ← warmup 已完成
                                                      │
                                 merge(symbol field) ─┘
                                    ↓
                               写 JSONL（symbol=<业务函数>, source=linkmap|atos|hex）
```

### 具体改动

1. `SamplingProfilerSidecar.__init__` 注入 `SymbolResolver`（由 `session.start()` 创建并 warmup 完成后传入）
2. cycle 流程新增一步：`aggregate_top_n` 产出带 `addr` 的 Top-N 后，批量调 `resolver.resolve_batch`，把 `symbol` 字段就地替换为解析后的名字
3. JSONL schema 扩展：每条 hotspot 增加 `source` 字段（`linkmap` / `atos` / `cache` / `unresolved`），下游 `hotspots --follow` 可按 source 着色
4. Resolver 未就绪保护：若 `resolver is None` 或 warmup 未完成，保持当前行为（输出 hex），cycle 不阻塞
5. resolver 被 sampling 持有但不被构造：构造/生命周期归 `session.py`，sampling 只消费

### session.py 改动（约 20 行）

```python
def start(self, ...):
    ...
    # ★ 新：构建 resolver（仅当可定位的 binary/dsym/linkmap 可发现）
    self._resolver = SymbolResolver.from_config(self.cfg, self.repo_path)
    threading.Thread(target=self._resolver.warmup, daemon=True).start()
    sidecar = SamplingProfilerSidecar(..., resolver=self._resolver)

def stop(self):
    ...
    if self._resolver:
        self._resolver.shutdown()
```

### 用户可见效果

- `cpar perf hotspots --follow` 首 cycle 10s 内输出 hex（warmup 未完成，符合预期）；第 2 cycle 起 90%+ 业务符号直接可读
- 若 LinkMap/dSYM 都不可用 → 退化为当前行为（hex + 提示），零破坏
- `perf symbolicate` 子命令保留原功能（深度后置符号化 + 独立 JSON 产物），不再是刚需（作为深度报告补充工具）

---

## §6 双轨路线图

### Track B（locate 优化）— 2 周见效

| 周 | 任务 | 产出 |
|---|------|------|
| B-W1.1 | 新建 `src/perf/locate/`（**即便 Track A 还没切目录，先放在这里作为前哨**），迁入 `linkmap.py` 原样 | `perf/locate/linkmap.py` |
| B-W1.2 | 实现 `AtosDaemon`（常驻 `atos -i` 子进程池，`shutdown` 安全终止） | `perf/locate/atos.py` + 单元测试 |
| B-W1.3 | 实现 `SymbolCache`（LRU + 落盘 JSON，session 级目录） | `perf/locate/cache.py` |
| B-W1.4 | 实现 `SymbolResolver`（串联 cache → linkmap → atos → hex，500ms timeout） | `perf/locate/resolver.py` |
| B-W2.1 | `session.py` 启动时构建 resolver + 异步 warmup | session 改 <30 行 |
| B-W2.2 | `sampling.py` 接入 `resolver.resolve_batch`，JSONL 加 `source` 字段 | sampling 改 <50 行 |
| B-W2.3 | 删除 `resymbolize.py`；`symbolicate.py` 保留但内部改走 resolver | 路径收敛 |
| B-W2.4 | 真机验证：`hotspots --follow` 命中率 >90% | 达成 §1 成功标准 B1/B2/B3 |

### Track A（架构分层）— 4 周完成

| 周 | 任务 | 产出 |
|---|------|------|
| A-W1 | 建 `src/application/`，迁移 `orchestrator→orchestration.py` + `merger→merge.py` + `reviewer→review.py` + `validator→validation.py` + `context_extractor→context_extraction.py` + `worker.py`；修所有 import | 服务层归位 |
| A-W2 | `claude_client.py` → `infrastructure/claude/client.py`；`chat_input.py` → `infrastructure/input/chat_input.py`；`WorkerResult` 抽到 `domain/worker_result.py` | 新边界落成 |
| A-W3 | 删 5 个 shim（`src/cli.py` `src/monitor.py` `src/task_parser.py` `src/fs_utils.py` `src/web_dashboard.py`）；改入口 `run.py` / `chat.py` / `cpar` 的 import 路径 | 死代码清零 |
| A-W4.1 | 切 `perf/` 子目录：`protocol/ capture/ decode/ analyze/ present/` + 移动文件 + `__init__.py` 做透明转发 | 5 层骨架 |
| A-W4.2 | 拆 `symbolicate.py` 到 `locate/{dsym,atos,cache}.py`（**此时与 Track B 合流，把 Track B 的 `resolver.py` 平移进 `locate/`，Track B 原地不动**） | 合流完成 |
| A-W4.3 | `sampling.py` 的 `parse_timeprofiler_xml` / `aggregate_top_n` 抽到 `decode/timeprofiler.py` | session <400 行、sampling <600 行 |
| A-W4.4 | 收窄 `perf/__init__.py` 显式列名 | 公共 API 收敛 |

### 合流点管理

- Track B 全程在 `src/perf/locate/`（未来的目标位置）工作，**不产生迁移成本**
- Track A 在 A-W4.2 遇到 `locate/` 已存在时直接接纳，避免冲突
- 两轨合并的唯一碰撞文件：`sampling.py`（Track B 改注入、Track A 抽解析）—— 约定：**Track B 先合，Track A 基于合并后再改**

### 里程碑

- **T+2 周**：hotspots 实时业务符号 ✅
- **T+3 周**：服务层归位 + shim 清零 ✅
- **T+4 周**：perf 5 层切分完成 ✅

---

## §7 风险、验证与后续

### 主要风险与缓解

| 风险 | 触发场景 | 缓解 |
|------|---------|------|
| **resolver warmup 阻塞 cycle** | LinkMap 加载慢（大 App 数秒）或 atos 启动慢 | warmup 走后台线程；未完成期间 resolver 直接返回 hex；**永不同步等** |
| **atos daemon 子进程崩溃** | atos 被系统杀 / dSYM 有坏段 | `AtosDaemon` 自带 `ReconnectableMixin` 同款指数退避（2s→4s→8s），失败计数达阈值后该地址入黑名单，不再走 atos |
| **500ms timeout 被频繁触发** | 冷启动或大批量查询 | 批量接口 `resolve_batch` 做整体预算（10 地址共享 2s），单地址软超时 200ms |
| **Track A/B 合流冲突** | A-W4.2 时 locate/ 已存在 | 约定 Track A 不新建 locate/，直接沿用 Track B 成果 |
| **删 shim 导致外部脚本炸** | `run.py` / 用户自建脚本的 `from src.task_parser import X` | A-W3 全仓搜索 `from src.(cli\|monitor\|task_parser\|fs_utils\|web_dashboard)` 并修正；外部 examples/README 同步更新 |
| **`perf/__init__.py` 收窄破坏消费方** | 有地方 `from src.perf import *` | 先全仓 grep 实际使用的名字，收窄列表就是那个集合 ∪ 原公共 API 文档列表 |

### 验证门槛（每 track 合并前必须通过）

| 项 | Track | 手段 |
|---|-------|------|
| 单元测试全绿 | A + B | `python -m pytest tests/` |
| iOS 真机冒烟 | B | `scripts/perf_e2e_smoke.sh` —— 4 轮 cycle，第 2 轮起业务符号可见 |
| 端到端 DAG | A | `python3 run.py run examples/auth-system.yaml --merge --clean` |
| `import` 方向检查 | A | 人工 diff + README 分层规则宣读；不上 import-linter |
| 命令行回归 | A + B | `cpar perf start/stop/hotspots/symbolicate/report` 全命令各跑一次 |

### 成功的定量读数

- sampling cycle 中位延迟：当前 ~8s → 目标 <9s（涨幅 <12%）
- hotspots 业务符号命中率：当前 0%（全 hex） → 目标 >90%
- `src/` 根文件数：当前 14 → 目标 ≤ 3（`__init__.py` + 一个 shim 都不留）
- `perf/session.py` 行数：当前 1086 → 目标 <400
- `perf/sampling.py` 行数：当前 1169 → 目标 <600

### 后续（本轮 brainstorm 范围外）

- import-linter 强约束（本轮推迟，依靠评审）
- perf 子系统独立 package 发布（方案 C，暂不做）
- 测试覆盖率补齐（Worker / Merger 无单测，本轮不扩）

---

## 附录 A：关键决策与依据

| 决策 | 选择 | 依据 |
|------|------|------|
| 输出形态 | 蓝图 + 分阶段路线图 | 用户选项 2 |
| 分层形状 | 方案 B — 中度整形（5 层 perf + 服务层归位） | 用户选项 B，平衡工作量与彻底性 |
| 符号化策略 | 在 cycle 内同步符号化（强实时） | 用户选项 1，配合 500ms timeout + warmup 保护 |
| 推进方式 | 双轨并行（Track A + Track B） | 用户选项 3，2 周 B 见效 + 4 周 A 完成 |
| 破坏性容忍 | 内部清理、外部稳定 | 用户选项 2，清 shim + 收窄公共 API，保留 CLI |
| `perf symbolicate` CLI | 保留原功能 | 用户选项 a，维持兼容性 |

## 附录 B：依赖关系初查（进 Track A-W1 前必做）

执行前清单：

```bash
# 1. 谁 import 了要迁移的服务层模块？
grep -rn "from src\.\(orchestrator\|worker\|merger\|reviewer\|validator\|context_extractor\|claude_client\|chat_input\)" --include="*.py" .

# 2. 谁 import 了要删除的 shim？
grep -rn "from src\.\(cli\|monitor\|task_parser\|fs_utils\|web_dashboard\)" --include="*.py" .

# 3. perf/__init__.py 的实际消费者
grep -rn "from src\.perf import\|from \.perf import" --include="*.py" .

# 4. WorkerResult 使用面
grep -rn "WorkerResult" --include="*.py" .
```

结果用于 Track A-W1 生成精确的 import 重写 patch。

## 附录 C：Track B Week 1 实施要点速查

| 文件 | 关键接口 | 实现要点 |
|------|---------|---------|
| `locate/linkmap.py` | 原样迁移 | 不改实现，仅改路径 |
| `locate/atos.py` | `AtosDaemon(binary, load_addr).lookup(addr) -> str` | `Popen(['atos', '-i', '-o', bin, '-l', hex(load)])`；stdin/stdout pipe；`Thread` 读 stdout；进程退出自动 reconnect |
| `locate/cache.py` | `SymbolCache.get(addr)` / `.put(addr, sym)` / `.load()` / `.flush()` | `OrderedDict` 容量 10K；session 目录 `symbols.json`；flush 原子写（复用 `infrastructure.storage.atomic`） |
| `locate/resolver.py` | `resolve(addr)` / `resolve_batch(addrs)` / `warmup()` / `shutdown()` | 串联三层策略；timeout 用 `concurrent.futures.Future.result(timeout=)`；warmup 标志用 `threading.Event` |
