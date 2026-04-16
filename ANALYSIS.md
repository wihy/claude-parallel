# claude-parallel 完整性与稳定性分析报告
# 重点: Claude 账号封禁风险评估

## ═══════════════════════════════════════════
## 一、封禁风险评估 (CRITICAL)
## ═══════════════════════════════════════════

### 🔴 高风险: 并发请求过多触发滥用检测

**位置**: orchestrator.py:235 (`asyncio.Semaphore(max_workers)`)

**问题**: 默认 max_workers=3，同一时间启动 3 个 claude CLI 实例。
每个实例内部可能有多个 API 调用 (每 turn 一次)。Claude 的 OAuth 认证
是单用户模式，3 个并发进程共享同一 token，可能导致:
- 同一 OAuth token 被多个进程并发使用
- 短时间内请求密度过高，触发 rate limit 或 abuse 检测
- Claude Code CLI 的 OAuth token 刷新可能冲突

**缓解现状**: 
- 每个任务有 --max-turns 和 --max-budget-usd 限制
- 使用 Semaphore 控制并发数

**缺失防护**:
- [ ] 没有在进程启动间添加随机延迟 (stagger)
- [ ] 没有检测 claude CLI 的 rate limit 输出 (429)
- [ ] max_workers 没有硬上限 (可设为 100)
- [ ] 重试时的指数退避只用于任务间，不用于同层并行启动


### 🔴 高风险: --dangerously-skip-permissions 标志

**位置**: worker.py:149

**问题**: 所有 Claude 进程都带 --dangerously-skip-permissions。
这个标志让 Claude 可以不经确认执行任意命令。虽然自动化必需，
但增加了被标记为"无人值守自动化"的风险。

**缓解现状**: 无额外缓解。

**缺失防护**:
- [ ] 没有限制 --allowedTools 范围 (当前只检查 YAML 配置，无强制默认)
- [ ] Bash 工具始终允许，Claude 可执行任意 shell 命令


### 🟡 中风险: Review 和冲突解决产生额外 Claude 调用

**位置**: 
- reviewer.py:350-356 — review 每个任务额外调用一次 claude CLI
- merger.py:440-451 — 冲突解决每个文件额外调用一次 claude CLI

**问题**: 一个 4 任务项目可能产生 4+4=8 次 Claude 调用。
如果并发 review + merge 中还有冲突解决，调用次数可能更多。

**缺失防护**:
- [ ] review 命令没有并发限制 (review_all 是串行的，还好)
- [ ] 冲突解决的 claude 调用没有 budget 感知，可能超出预期
- [ ] reviewer 没有全局调用计数，无法知道总共消耗了多少 API 配额


### 🟡 中风险: 重试机制可能加重请求负载

**位置**: worker.py:376-450

**问题**: retry_worker 的指数退避从 5s 开始，对于 rate limit 类错误
可能不够。Claude 的 rate limit 冷却期可能更长 (30s-5min)。

**缓解现状**: 
- 识别 rate_limit/429/503 作为可重试错误
- 指数退避 5s * 2^attempt

**缺失防护**:
- [ ] 退避基数 5s 太短，rate limit 场景建议 30s+
- [ ] 没有从 Claude CLI 输出中提取 Retry-After 头信息
- [ ] 同层多个任务同时重试时没有协调退避


## ═══════════════════════════════════════════
## 二、完整性问题
## ═══════════════════════════════════════════

### 🟡 进程泄露风险

**位置**: worker.py:295-300

**问题**: _kill() 只调用 terminate()，没有等待进程退出。
如果 Claude CLI 不响应 SIGTERM，进程会变成僵尸进程。

**建议**: 
- terminate 后等 5s，不退出则 kill -9
- cleanup 时清理所有子进程


### 🟡 Worktree 清理不完整

**位置**: cli.py:cmd_clean

**问题**: 
- 只检查 `/cp-` 在路径中的 worktree
- 不清理 git 分支 (worktree 删除后分支还在)
- 不清理 .claude-parallel/ 目录中的陈旧数据


### 🟢 Python 3.9 兼容性

**问题**: orchestrator.py:69 使用 `list[Task]` 类型标注。
Python 3.9 支持 `list[X]` 作为类型标注 (PEP 585)。
但 worker.py:83 使用 `list[str]` — 在 Python 3.9 中作为实例属性
类型标注是可以的，但在 Python 3.8 中不行。

**现状**: 声明 3.9+，所以没问题。但建议加入 runtime 检查。


### 🟢 状态文件大小写不一致

**位置**: worker.py:317 写入 `{task_id}.STATUS`
但 context summary 提到 `{task_id}.status`

**实际**: 代码中写入 .STATUS，cli.py 中读取 .result，
不会造成运行时错误，但文档描述不一致。


## ═══════════════════════════════════════════
## 三、建议修复 (按优先级)
## ═══════════════════════════════════════════

### P0 — 封禁防护 (必须)

1. **max_workers 硬上限**: 限制为 5，超过发出警告
2. **并行启动随机延迟**: 同层任务启动间隔 2-5 秒随机延迟
3. **Rate limit 退避增强**: 检测 429 后退避 60s+，而非当前 5s
4. **全局调用计数**: 记录 claude CLI 总调用次数，达到阈值发出警告
5. **每分钟调用限制**: 追踪最近 60s 内的 claude CLI 启动次数

### P1 — 稳定性加固 (推荐)

6. **进程强制清理**: terminate → 5s 等待 → kill -9
7. **Worktree 完整清理**: 删除 worktree + 分支 + 协调数据
8. **Signal 处理改进**: SIGTERM 也应触发优雅退出
9. **JSON 解析容错**: Claude 输出有时包含 ANSI 转义码

### P2 — 健壮性 (建议)

10. **磁盘空间检查**: 启动前检查磁盘剩余
11. **网络连通性检查**: 启动前验证 claude CLI 可连通
12. **Git 状态检查**: 确保仓库干净后再创建 worktree
