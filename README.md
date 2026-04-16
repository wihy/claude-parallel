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

## 前置要求

- Python 3.9+
- Claude Code CLI (`npm install -g @anthropic-ai/claude-code`)
- 已认证的 Claude 账号（OAuth 或 API Key）
- Git 仓库

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

## 完整命令列表

```
run       执行任务 (--dry/--merge/--clean/--retry N/--total-budget $)
resume    从中断处恢复
plan      展示执行计划
validate  校验 YAML 配置
diff      预览所有 worktree 变更
merge     合并 worktree (支持冲突自动解决)
review    对所有变更执行 Code Review
clean     清理 worktree 和协调文件
logs      查看任务日志
```

## 文件结构

```
claude-parallel/
├── run.py                      # CLI 入口
├── src/
│   ├── cli.py                  # CLI 命令处理
│   ├── task_parser.py          # YAML 解析 + DAG 拓扑排序
│   ├── worker.py               # Worker 进程管理 + 重试 + 日志
│   ├── orchestrator.py         # 调度器 (DAG/重试/预算/恢复)
│   ├── monitor.py              # Rich Live 实时进度面板
│   ├── merger.py               # Worktree 合并 + 冲突自动解决
│   ├── reviewer.py             # 自动 Code Review
│   ├── validator.py            # YAML 配置校验
│   └── context_extractor.py    # 多语言上下文提取
├── examples/
│   ├── auth-system.yaml        # 4任务 DAG 示例
│   ├── test-dag.yaml           # 端到端 DAG 测试
│   ├── simple-parallel.yaml    # 简单并行
│   └── test-p2.yaml            # Phase 2 测试
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
