# cpar 实战 Playbook

## 一、执行决策表

| 任务特征 | 推荐模式 | 理由 |
|---------|---------|------|
| 2~3 个独立小改动（文件不重叠） | 直接 Claude 多实例 | 启动快，配置成本最低 |
| 3~10 个中等任务，有明确依赖 | cpar（DAG + worktree） | 并行收益高、可恢复、可审计 |
| 高耦合重构（多人都会改同一核心文件） | 混合模式（先单 agent 规划，再 cpar 分段） | 先冻结接口，避免并行互踩 |
| 探索性需求（需求不稳定、边做边改） | 单 Claude 主导 + 少量并行子任务 | 先收敛方向，再并行执行 |
| 临近发布，要求稳定性最高 | 混合模式（cpar 执行 + 单 agent 总集成验收） | 并行提速 + 严格收口 |

## 二、推荐默认参数

- max_workers: 2（稳定后可到 3）
- default_max_turns: 15
- default_max_budget_usd: 2.0
- retry_count: 1~2
- retry_backoff: 5.0
- total_budget_usd: 按任务规模设置（例如 8~20）
- 研究/Web 任务单独设:
  - max_turns >= 15
  - max_budget_usd >= 2.0

## 三、第一轮跑法（稳）

1. 先用 max_workers=2
2. 先 run，不自动 merge
3. 先看 diff + review，再 merge

命令顺序：
```bash
cpar validate tasks.yaml
cpar plan tasks.yaml
cpar run tasks.yaml
cpar diff tasks.yaml
cpar review tasks.yaml --budget 1.0
cpar merge tasks.yaml
```

## 四、常见失败 -> 直接处理

### A. 规划阶段失败（chat 生成 YAML 失败）
- 症状：max turns / budget / 空输出
- 处理：
  - --planner-budget 提到 1.2 或 1.6
  - --planner-retries 设 2~3
  - 保持 --planner-models 备用链
  - 需求文字先压缩到"目标+范围+验收标准"三段

### B. 运行阶段 429/rate limit
- 症状：重试多、速度慢
- 处理：
  - max_workers 从 3 降到 2
  - 减少同层任务数量（把任务分两层）
  - 避免短时间连续跑多个大 DAG

### C. merge 冲突多
- 症状：cherry-pick 频繁冲突
- 处理：
  - 回到 task 设计，按"文件所有权"重切
  - 把公共文件改动集中到单独任务（比如 config/router）
  - 汇总任务只做集成与验证，不再改核心逻辑

### D. 任务经常超预算
- 症状：error_max_budget_usd
- 处理：
  - 重型任务单独设 max_budget_usd: 2.0~2.5
  - 研究任务单独设 max_turns: 15~20
  - 把"分析+实现"拆成两个任务，不要塞一个任务里

## 五、真正提速点（不是盲目加并发）

- 提速核心 = 减冲突，不是加 workers
- 最有效动作：
  - 每任务 files 明确
  - depends_on 准确
  - 最后加 integration-qa 收口任务
- 这样整体吞吐通常比"3~5 并发硬冲"更快更稳

## 六、混合流水线 tasks.yaml 模板

```yaml
project:
  repo: __REPO__
  branch: main
  max_workers: 2
  default_max_turns: 15
  default_max_budget_usd: 2.0
  retry_count: 2
  retry_backoff: 5.0
  total_budget_usd: 12.0

tasks:
  # Phase 0: 先做统一设计（单点收敛）
  - id: design-contracts
    description: |
      梳理改造范围，冻结接口与数据契约。
      输出:
      1) 关键接口签名
      2) 模块边界
      3) 每个并行任务的文件所有权清单
      禁止大规模实现，仅产出设计与约束文档。
    files:
      - docs/architecture/contracts.md
      - docs/architecture/task-boundaries.md
    allowed_tools: ["Read", "Write", "Edit", "Bash"]
    depends_on: []
    max_turns: 12
    max_budget_usd: 1.5

  # Phase 1: 并行实现（互不重叠）
  - id: backend-impl
    description: |
      按 contracts.md 实现后端改造，只改 backend/ 与相关测试。
      若发现契约冲突，记录到 docs/architecture/contract-issues.md，不擅自扩改边界。
    files:
      - backend/
      - tests/backend/
    allowed_tools: ["Read", "Write", "Edit", "Bash"]
    depends_on: ["design-contracts"]

  - id: frontend-impl
    description: |
      按 contracts.md 实现前端改造，只改 frontend/ 与相关测试。
      严格遵循接口契约，不修改后端代码。
    files:
      - frontend/
      - tests/frontend/
    allowed_tools: ["Read", "Write", "Edit", "Bash"]
    depends_on: ["design-contracts"]

  - id: docs-update
    description: |
      更新 README、变更说明、使用示例，确保文档反映最终接口与行为。
    files:
      - README.md
      - docs/
    allowed_tools: ["Read", "Write", "Edit"]
    depends_on: ["design-contracts"]

  # Phase 2: 总集成与验收（收口）
  - id: integration-qa
    description: |
      对所有改动做总集成验收:
      1) 编译/静态检查
      2) help 输出可用
      3) validate 通过
      4) 单元断言与关键回归
      输出最终验收报告 docs/release/qa-report.md
    files:
      - docs/release/qa-report.md
      - tests/
    allowed_tools: ["Read", "Write", "Edit", "Bash"]
    depends_on: ["backend-impl", "frontend-impl", "docs-update"]
    max_turns: 18
    max_budget_usd: 2.5
```
