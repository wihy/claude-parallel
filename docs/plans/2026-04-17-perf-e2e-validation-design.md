# iOS 真机 Sampling Profiler 端到端验证 — 设计

日期: 2026-04-17
状态: 已确认，可执行
前置: feat(perf): Sampling Profiler 旁路 (9886d55)

## 目标

在 iOS 真机 + SoulApp (`Soul_New`) 上跑通完整 sampling profiler 流程，回答三个问题:

1. **能跑通吗？** — xctrace 短周期旁路在真机上是否稳定
2. **输出有用吗？** — 热点函数列表是否出现业务符号
3. **延迟可接受吗？** — 操作到 follow 刷新是否在 15-20s 内

## 验证方案

### Round 1: 旁路基础可用性

只开 sampling 旁路（不开主链路），验证 sidecar 基础功能:
- sampling.enabled == true
- hotspots.jsonl 至少 1 个 cycle
- 热点列表含符号名（非纯地址）
- 无 "already recording" 错误
- stop 后无僵尸进程

### Round 2: 双 xctrace 互斥验证

Power 主链路 + sampling 旁路同时启动:
- 两条链路正常共存
- Power trace 和 hotspots.jsonl 同时产出

补充 Round 2b: 主链路 Time Profiler + sampling:
- 预期 sampling 被冲突检测自动跳过
- reason == "main_timeprofiler_conflict"

### Round 3: 业务符号可见性 + 延迟 (交互式)

三个场景依次触发:
- A. 冷启动 (kill → 重新打开)
- B. 重 CPU 操作 (群聊/滑动消息列表)
- C. 空闲态 (停在首页不操作)

验证:
- --follow 每 ~12s 刷新一屏
- 聚合 Top 中出现 SO/Soul 业务符号
- 场景 C 采样数明显低于场景 B
- 延迟 ~15-20s 内

## 判定标准

| 结果 | 含义 | 后续 |
|---|---|---|
| 业务符号可见 + 忙闲可区分 | 验证通过 | 进入跨平台设计 |
| 只有系统符号 | 符号化不足 | 优先建议 #5 符号化解耦 |
| cycle 不稳定/频繁失败 | xctrace 短周期不可靠 | 评估 DTX 或降低频率 |

## 执行方式

```bash
# 自动化 Round 1 + 2 + 2b
./scripts/perf_e2e_smoke.sh all

# 交互式 Round 3
./scripts/perf_e2e_smoke.sh 3
```

脚本自动检测设备 UDID，也支持 `--device UDID --process NAME` 手动指定。
