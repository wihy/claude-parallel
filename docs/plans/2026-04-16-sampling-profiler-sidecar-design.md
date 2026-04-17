# Sampling Profiler 旁路 — 设计

日期: 2026-04-16
状态: 已确认，进入实现
关联: 真机运行时模块第一阶段优化（建议 #1）

## 背景

当前 `PerfSessionManager.callstack()` (`src/perf/session.py:441-531`) 只能**事后**分析：必须 `stop()` 会话后，才通过 `xcrun xctrace export` 一次性导出 TimeProfiler XML 并聚合。痛点：

1. 运行时看不到热点函数，调试/回归期只能盲等
2. 长录制场景（默认 1800s）问题定位滞后 30 分钟
3. 主通道被 Power/System Trace 占用时无法并行观察 CPU 热点

更底层的 DTX Protocol 通道可实现 sub-second 热点流，但**协议私有、iOS 版本敏感、维护脆弱**，本期不做。

## 方案

在 PerfSessionManager 中新增一条 **Sampling Profiler 旁路** sidecar：按固定周期（默认 10s）循环调用 `xcrun xctrace record --template 'Time Profiler' --time-limit N`，每 cycle 结束后即时 export 并聚合 Top-N 热点，追加到 `logs/hotspots.jsonl`。主通道（长录制）保持不动，旁路只负责"运行时预览"。

**选型理由**：
- 复用既有 `xcrun xctrace` 链路，不引入新工具依赖
- 复用 `_parse_timeprofiler_xml` 解析器（session.py:657-700），零解析分叉
- JSONL append-only 天然支持 `tail -f` 式消费

**实时性评估**：xctrace 冷启动 2-5s + export 1-2s，单 cycle 延迟下限 ~5s。**5-30s 为合理区间**，默认 10s。真实时（<1s）需 DTX Protocol，不在本期范围。

## §1 架构

```
PerfSessionManager
 ├─ syslog sidecar       (既有)
 ├─ xctrace main         (既有，长录制精确分析)
 └─ sampling sidecar     (新增) ──► logs/hotspots.jsonl
        │
        └─ cycle loop (subprocess.Popen + threading.Thread):
             1. xctrace record --template time --time-limit 10
                --output <tmp>/cycle_{N}.trace --attach PID
             2. xctrace export --input ... --xpath TimeProfiler
             3. parse_timeprofiler_xml()  # 复用
             4. aggregate_top_n()
             5. append JSONL + rotate
             6. rm -rf cycle_N.trace
```

**新文件** `src/perf/sampling.py`，对外接口：

```python
@dataclass
class HotspotSnapshot:
    ts: float
    cycle: int
    duration_s: int
    sample_count: int
    top: list[dict]  # [{"symbol": str, "samples": int, "pct": float}]

class SamplingProfilerSidecar:
    def __init__(self, session_root: Path, device_udid: str, process: str,
                 interval_sec: int = 10, top_n: int = 10,
                 retention: int = 30): ...
    def start(self) -> None        # 启后台 cycle 线程
    def stop(self, timeout=10): ...  # 通知退出 + 等待 cycle 收尾
    def is_alive(self) -> bool: ...
```

**改造** `src/perf/session.py`:
- `PerfSessionManager.__init__` 持有 `Optional[SamplingProfilerSidecar]`
- `start()` 若 `PerfConfig.sampling_enabled` → 构造并启动旁路；meta.json 新增 `sampling` 段
- `stop()` 先停旁路再停主链路

## §2 数据格式

**`logs/hotspots.jsonl`**，每 cycle 一行：

```jsonl
{"ts": 1713250010, "cycle": 1, "duration_s": 10, "sample_count": 1234, "top": [
  {"symbol": "-[SOMetalRenderer drawFrame:]", "samples": 287, "pct": 23.3},
  {"symbol": "objc_msgSend", "samples": 156, "pct": 12.6}
]}
```

**保留策略**：ring-buffer，超 `retention`（默认 30）条时原地重写保留尾部。不做按时间窗口 rotate（cycle 一致性更重要）。

## §3 CLI

```bash
# 启动会话时开启（默认关闭，避免无脑叠加 xctrace 负载）
cpar perf start --tag my_run --device UDID --sampling \
    --sampling-interval 10 --sampling-top 10

# 运行时实时查看（tail -f 式，每新 cycle 刷新一屏）
cpar perf hotspots --tag my_run --follow

# 指定窗口 / 排名数
cpar perf hotspots --tag my_run --last 5 --top 20

# 全会话聚合（跨 cycle 求和）
cpar perf hotspots --tag my_run --aggregate
```

`cpar perf hotspots` 新建 `cmd_perf_hotspots` 放在 `src/cli.py`，与现有 `cmd_perf_*` 同风格。

## §4 配置

扩展 `src/perf/config.py::PerfConfig`：

```python
@dataclass
class PerfConfig:
    # ...现有字段
    sampling_enabled: bool = False
    sampling_interval_sec: int = 10   # 5-30 区间，<5 会在 start() 时 warn + clamp
    sampling_top_n: int = 10
    sampling_retention: int = 30
```

`build_perf_config_from_args` (cli.py:98-114) 同步增加 `--sampling / --sampling-interval / --sampling-top / --sampling-retention`。

## §5 双 xctrace 互斥风险 & 容错

**已知风险**：iOS 设备上同一进程能否并行两条 xctrace session 未验证（用户本地无真机）。本期采用**保守策略**：

1. 旁路默认关闭，显式 `--sampling` 才启用
2. 旁路 `start()` 前先检测主链路 xctrace 是否包含 Time Profiler 模板 → 若包含则**直接 warn 并不启动旁路**（避免解析竞争）
3. 若 xctrace cycle 返回非零 exit / stderr 含 `already recording` 类错误 → 旁路自动停机，写 `logs/sampling.stderr`，不影响主链路
4. 单 cycle 失败不致命 → 记录 stderr，下 cycle 继续；连续 3 次失败自停

**其他容错**：

| 场景 | 行为 |
|---|---|
| interval < 5s | clamp 到 5s + warn |
| cycle trace 文件生成失败 | 当前 cycle 跳过，continue |
| 设备断连 | cycle 失败计数器+1，到 3 自停 |
| export XML 损坏 | 本 cycle 丢弃，不中断 sidecar |
| JSONL 写入失败 | 异常吞掉 + stderr，下 cycle 继续 |
| 进程收到 SIGTERM | 打断当前 cycle（kill xctrace 子进程）+ flush JSONL |

## §6 与 Orchestrator 集成

`src/perf/integrator.py::PerfIntegrator`:
- `on_run_start`: 如果 sampling_enabled，session.start 已拉起旁路（无需额外钩子）
- `on_level_end`: 从 JSONL 读最新 1 条 top N → 追加到 Rich 表格输出（既有 metrics 表下方）
- `on_run_end`: 打印全会话 aggregate top N 到 final report

不新增钩子，全部复用现有时点。

## §7 测试策略

**单元测试** `tests/test_perf_sampling.py`：
1. `parse_timeprofiler_xml` fixture 端到端（用已有 `tests/fixtures/timeprofiler_sample.xml` 或 mock）
2. `aggregate_top_n` 边界：0 样本、所有符号一样、N 超过总符号数
3. Ring-buffer 追加：超过 retention 后前 N 条被删
4. `SamplingProfilerSidecar` 在 mock `subprocess.Popen`（xctrace 替身）下跑 3 cycle，验证 JSONL 条数/内容
5. Clamp：interval=1 → 被钳到 5 + warn
6. 主链路含 Time Profiler 时 `start()` 返回 False 且 log warn

**集成测试** `tests/test_perf_session_sampling.py`：
1. mock xctrace 替身 → PerfSessionManager.start(sampling=True) → 跑 ~15s → stop → 校验 meta.json / hotspots.jsonl
2. `cpar perf hotspots --tag X --last 3` 命令行端到端

**真机验证**（用户本地暂不可执行，留 README 手工清单）：
- 启动一个已知 CPU 繁忙 app，sampling 10s × 3 cycle，`cpar perf hotspots --follow` 观察是否出现业务符号
- 同时开启主 Time Profiler 通道，验证 §5 的互斥规避逻辑

## §8 文件清单

**新增**：
- `src/perf/sampling.py` (~250 行)
- `tests/test_perf_sampling.py`
- `tests/fixtures/timeprofiler_mini.xml`（可从现有会话抽小样本）

**修改**：
- `src/perf/config.py` — 新增 4 个字段
- `src/perf/session.py` — 启停旁路 + meta.json 写入
- `src/perf/integrator.py` — level_end/run_end 追加热点输出
- `src/cli.py` — `cmd_perf_hotspots` + `build_perf_config_from_args` 扩展
- `README.md` — perf 章节补 sampling 用法

## 不做（YAGNI）

- 真实时（<1s）采样 — 需 DTX，本期外
- Android 支持 — 属建议 #2 Perfetto 范畴
- 火焰图生成 — 属 CLI 增强，本期只给文本 Top-N
- 跨 session baseline 对比热点 — 属 gate 扩展，另起一期
- 符号化重做 — 属建议 #5
