# Changelog

## v0.4.0 — 2026-04-21

### Added — 架构分层

- `src/application/` 新增服务层,承载 orchestration/worker/merge/review/validation/context_extraction
- `src/domain/worker_result.py` 抽出 `WorkerResult` dataclass (上升到领域层,所有层可依赖)
- `src/infrastructure/claude/client.py` 接纳原 `claude_client.py`
- `src/infrastructure/input/chat_input.py` 接纳原 `chat_input.py`
- `src/perf/protocol/` 子包 (reconnect / dvt / device) — 设备通信层
- `src/perf/capture/` 子包 (sampling / webcontent / live_metrics / live_log) — 原始数据采集
- `src/perf/decode/` 子包 (templates / timeprofiler / deep_export / time_sync) — Schema/XML 解析
- `src/perf/analyze/` 子包 (power_attribution / ai_diagnosis / callstack / metrics / syslog_stats) — 高层分析
- `src/perf/present/` 子包 (report_html / dvt_metrics) — 呈现层

### Added — 实时方法定位 (`perf/locate/`)

- `SymbolResolver` 统一入口 — cache → LinkMap bisect → atos daemon → hex 四层兜底,500ms timeout 保险
- `AtosDaemon` 常驻 `atos -i` 子进程 — 消除每次 subprocess 500ms 启动开销,线程安全 lookup,5 次失败自动黑名单
- `SymbolCache` LRU + 原子 JSON 持久化 — session 间命中恢复
- `locate.linkmap` 原 `linkmap.py` 迁入 locate 层
- `locate.dsym` 原 `symbolicate.py` 迁入,保留 `cpar perf symbolicate` CLI 深度后置工具功能
- **In-cycle 符号化** — sampling cycle 的 `aggregate_top_n` 后立即批量 resolve,JSONL 每条带 `source` 字段
- **新 CLI 旗标** `--binary` / `--linkmap` / `--dsym` (perf start) 和 `--perf-binary` / `--perf-linkmap` / `--perf-dsym` (run/resume --with-perf)

### Added — 其它

- `aggregate_top_n(filter_overhead=True)` — 剔除 xctrace dyld 采样自身开销 (实测饱和 Top-10 达 45%),让业务热点可见
- 128 → 117 个单元测试 (shim-compat 测试随 shim 删除而退役,新增 locate 层 / 分层 / overhead 过滤测试)

### Changed

- `session.py` 1123 → 665 行 (−40.8%),拆出 callstack/metrics/syslog_stats/dvt_metrics 到 analyze/present 层
- `sampling.py` 1229 → 837 行 (−32%),parse/aggregate 抽到 `decode/timeprofiler.py`
- `perf/__init__.py` 公共 API 收窄为显式 `__all__` 列表,无 `from .x import *`
- 依赖方向规则: `app → application → domain ← infrastructure`; perf 只依赖 domain + infrastructure

### Removed

- 13 个服务层 shim (`src/{cli,monitor,task_parser,fs_utils,web_dashboard,orchestrator,worker,merger,reviewer,validator,context_extractor,claude_client,chat_input}.py`)
- 15 个 perf 根目录 shim (`src/perf/{sampling,webcontent,live_log,live_metrics,templates,deep_export,time_sync,reconnect,dvt_bridge,device_metrics,linkmap,symbolicate,power_attribution,ai_diagnosis,report_html}.py`)
- `src/perf/resymbolize.py` (142 行,事后符号化补丁,能力已被 `SymbolResolver` 的 LinkMap 层吸收)

### Fixed (通过 iPhone 真机验证发现)

- `sampling.py` subprocess daemon 未继承父进程 resolver → 独立子进程里 `self._resolver=None` → 所有采样走 unresolved hex 兜底。现 daemon_code 序列化 resolver 构造参数让子进程自行重建 (commit `178f218`)
- `AtosDaemon` `load_addr=0` 默认值 → atos 无法计算正确 offset → iOS 业务地址全部解析失败。改为 iOS arm64 标准基址 `0x100000000` (commit `399444c`)
- `session.py` 真机使用 binary 参数必须指向 `.debug.dylib` 而非 launcher stub (iOS Debug build 特有)。文档已说明

---

## v0.3.1 — pre-Phase 5 (历史,略)

`src/` 根目录平铺所有模块,perf 平铺 17 个模块。完整细节见 git log。
