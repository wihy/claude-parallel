# perf 子系统瓶颈修复 — 设计

日期: 2026-04-17
状态: 已确认，进入实现

## 背景

审计发现 4 层瓶颈：解析性能（正则 3 遍扫描 + 全量读内存）、实时性（cycle 延迟 ~20s）、采集覆盖（xctrace 单 slot 互斥）、分析深度（只有叶子函数，无时间切片）。

## 方案

### Layer 1: XML 解析改 iterparse

- `sampling.py` 的 `_parse_time_profile_format` 从 3 遍正则改为单遍 `xml.etree.ElementTree.iterparse`
- `live_metrics.py` 的 `_parse_exported_xml` 同理改造
- `elem.clear()` 每行释放，内存恒定
- id_map 渐进构建，单遍完成 ref 解析

### Layer 2: Pipeline 重叠

- `_cycle_loop` 改双线程：record 线程 + export 线程
- 当前轮 record 与上一轮 export+parse 重叠执行
- 首轮延迟不变，后续延迟从 ~17s 降到 ~10s
- MIN_INTERVAL 从 5s 降到 3s

### Layer 3: systemtrace 自动接入 callstack

- `_find_timeprofiler_traces` 识别 systemtrace 模板含 time-profile
- 无需单独跑 Time Profiler 轮次

### Layer 4: 完整调用树 + 时间切片

- `parse_timeprofiler_xml` 新增 `keep_full_stack` 参数
- 解析时提取 `sample-time` 时间戳
- `callstack()` 新增 `--from` / `--to` 时间段切片
- `callstack()` 新增 `--full-stack` 完整调用树

## 改动范围

| 文件 | 改动 |
|---|---|
| sampling.py | iterparse + pipeline cycle loop |
| live_metrics.py | iterparse |
| session.py | callstack 调用树 + 时间切片 + systemtrace 识别 |
| cli.py | --from/--to/--full-stack 参数 |

## 不动

- device_metrics.py / integrator.py / config.py / templates.py
