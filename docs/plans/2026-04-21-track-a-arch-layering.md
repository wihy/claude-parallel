# Track A: 架构分层整理 — 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 完成"应用 → 编排 → 领域 → 设施"四层分层，消除 `src/` 根目录的悬空服务层模块和 5 个 shim 文件；把 `perf/` 从平铺 17 模块切成 5 层子包（protocol/capture/decode/locate/analyze/present），使 `session.py` 降至 <400 行纯编排。

**Architecture:** 新建 `src/application/` 层承载服务型模块（orchestrator/worker/merger/reviewer/validator/context_extractor）；`claude_client.py` 和 `chat_input.py` 下沉到 `infrastructure/`；`WorkerResult` 上升到 `domain/`；`perf/` 按职责内部分层，但对外 `from src.perf import ...` 主要公共 API 保留。整个 refactor 分 4 周，每周一个主迭代，内部再拆 8-10 个原子任务。

**Tech Stack:** 纯 Python 重构，无新依赖；靠代码评审 + README 分层规则守护方向；`git mv` 保留历史；每次迁移后跑 `unittest discover` 做回归。

**设计出处:** `docs/plans/2026-04-21-layering-and-locate-design.md` §2 + §3 + §6

**与 Track B 的合流约定:**
- Track A 在 A-W4.2 时**不新建** `src/perf/locate/`，直接接受 Track B 已产出的目录
- 唯一碰撞文件 `sampling.py`：**Track B 先合，Track A 基于合并后再做解析抽取**

---

## 前置检查

### Task 0: Baseline

**Step 0.1: 确认 worktree**

Run:
```bash
cd /Users/chunhaixu/claude-parallel/.worktrees/arch-layering
git status
git branch --show-current
```

Expected: branch `feature/arch-layering`, clean tree

**Step 0.2: Baseline 测试**

Run:
```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
```

Expected: `Ran 44 tests ... OK`

**Step 0.3: 生成依赖关系快照（设计文档附录 B）**

Run:
```bash
grep -rn "from src\.\(orchestrator\|worker\|merger\|reviewer\|validator\|context_extractor\|claude_client\|chat_input\)" --include="*.py" . > /tmp/track_a_consumers.txt
wc -l /tmp/track_a_consumers.txt
cat /tmp/track_a_consumers.txt | head -30
```

保存此文件 —— 后续迁移时按图索骥修改 import。

---

## 阶段 A-W1: 服务层归位（6 个原子任务）

### Task 1: 建 application/ 包 + 迁移 orchestrator.py

**Files:**
- Create: `src/application/__init__.py`
- Create: `src/application/orchestration.py`（从 `src/orchestrator.py` 移动）
- Modify: `src/orchestrator.py` → 改为 1 行 shim（过渡期，A-W3 删除）
- Modify: 所有 `from src.orchestrator import` / `from .orchestrator import` 消费方
- Test: `tests/test_application_layer.py`

**Step 1.1: 写失败测试**

`tests/test_application_layer.py`：
```python
import unittest


class ApplicationLayerTest(unittest.TestCase):

    def test_orchestrator_importable_from_application(self):
        from src.application.orchestration import Orchestrator, BudgetExceeded
        self.assertTrue(callable(Orchestrator))

    def test_orchestrator_shim_still_works(self):
        # 过渡期兼容
        from src.orchestrator import Orchestrator
        self.assertTrue(callable(Orchestrator))


if __name__ == "__main__":
    unittest.main()
```

**Step 1.2: 跑测试确认失败**

Run:
```bash
python3 -m unittest tests.test_application_layer -v 2>&1 | tail -10
```

Expected: 2 test FAIL

**Step 1.3: 创建 application 包 + 移动文件**

```bash
mkdir -p src/application
: > src/application/__init__.py
git mv src/orchestrator.py src/application/orchestration.py
```

在 `src/orchestrator.py` 重建（shim）：
```python
"""Transitional shim — 迁移到 src.application.orchestration. 将在 A-W3 删除."""
from .application.orchestration import *  # noqa: F401,F403
```

**Step 1.4: 修 orchestration.py 内部相对 import**

打开 `src/application/orchestration.py`，把所有 `from .xxx import` 改为绝对 `from src.xxx import`（以便自由移动），或保留相对但从 `..` 起（当前文件在子包内）。

推荐统一用 `from src.xxx` 绝对 import。具体：
```python
# before
from .infrastructure.storage.atomic import ...
from .domain.tasks import ...
from .worker import Worker, WorkerResult, retry_worker
from .infrastructure.monitoring.rich_monitor import Monitor
from .merger import WorktreeMerger, MergeReport
from .context_extractor import extract_context_for_downstream
from .perf import PerfConfig, PerfSessionManager, PerfIntegrator

# after
from src.infrastructure.storage.atomic import ...
from src.domain.tasks import ...
from src.worker import Worker, WorkerResult, retry_worker
from src.infrastructure.monitoring.rich_monitor import Monitor
from src.merger import WorktreeMerger, MergeReport
from src.context_extractor import extract_context_for_downstream
from src.perf import PerfConfig, PerfSessionManager, PerfIntegrator
```

**Step 1.5: 扫消费方**

Run:
```bash
grep -rn "from src\.orchestrator\|from \.orchestrator" --include="*.py" . | grep -v ".worktrees/" | grep -v __pycache__
```

对每个命中点：如果是测试 / 文档引用，不改；如果是生产代码，**保留 `from src.orchestrator`**（走 shim），本步不动，A-W3 统一改。

**Step 1.6: 跑测试确认通过**

Run:
```bash
python3 -m unittest tests.test_application_layer -v 2>&1 | tail -10
```

Expected: `Ran 2 tests ... OK`

**Step 1.7: 回归**

Run:
```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
python3 run.py --help 2>&1 | head -5
```

Expected: `Ran 46 tests ... OK` + 帮助文本正常

**Step 1.8: 提交**

```bash
git add src/application/ src/orchestrator.py tests/test_application_layer.py
git commit -m "refactor(app): 迁移 orchestrator.py → application/orchestration.py (保留 shim)"
```

---

### Task 2: 迁移 worker.py → application/worker.py

**Files:**
- Move: `src/worker.py` → `src/application/worker.py`
- Create: `src/worker.py` shim

**Step 2.1: 写失败测试**

追加到 `tests/test_application_layer.py`：
```python
    def test_worker_importable_from_application(self):
        from src.application.worker import Worker, WorkerResult, retry_worker
        self.assertTrue(callable(Worker))

    def test_worker_shim_still_works(self):
        from src.worker import Worker
        self.assertTrue(callable(Worker))
```

Run 确认失败：
```bash
python3 -m unittest tests.test_application_layer -v 2>&1 | tail -10
```

Expected: 新 2 test FAIL

**Step 2.2: 移动 + 建 shim**

```bash
git mv src/worker.py src/application/worker.py
```

创建 `src/worker.py`：
```python
"""Transitional shim — 迁移到 src.application.worker. 将在 A-W3 删除."""
from .application.worker import *  # noqa: F401,F403
```

**Step 2.3: 修 worker.py 内部 import**

在 `src/application/worker.py` 顶部：
```python
# before
from .infrastructure.storage.atomic import atomic_write_json, atomic_write_text
from .domain.tasks import Task, ProjectConfig

# after
from src.infrastructure.storage.atomic import atomic_write_json, atomic_write_text
from src.domain.tasks import Task, ProjectConfig
```

**Step 2.4: orchestration.py 内部把 `from src.worker` 改为 `from src.application.worker`**

直接修 `src/application/orchestration.py`，避免自己的服务层模块间通过 shim 互相调用。

**Step 2.5: 跑测试 + 回归**

```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
```

Expected: `Ran 48 tests ... OK`

**Step 2.6: 提交**

```bash
git add src/application/ src/worker.py tests/test_application_layer.py
git commit -m "refactor(app): 迁移 worker.py → application/worker.py"
```

---

### Task 3: 迁移 merger.py → application/merge.py

**Files:** 同 Task 2 模式
- Move: `src/merger.py` → `src/application/merge.py`
- Create: `src/merger.py` shim

**Step 3.1: 写失败测试** —— 追加到 `tests/test_application_layer.py`
```python
    def test_merger_importable_from_application(self):
        from src.application.merge import WorktreeMerger, MergeReport
        self.assertTrue(callable(WorktreeMerger))
```

**Step 3.2: 跑测试确认失败 + 移动 + 修 import + 回归 + 提交**

（流程与 Task 2 相同；合并后回归基线变为 49 tests）

```bash
git mv src/merger.py src/application/merge.py
# 修 orchestration.py 里 from src.merger → from src.application.merge
# 建 src/merger.py shim
python3 -m unittest discover -s tests -q 2>&1 | tail -5
git add src/application/ src/merger.py tests/test_application_layer.py
git commit -m "refactor(app): 迁移 merger.py → application/merge.py"
```

---

### Task 4: 迁移 reviewer.py / validator.py / context_extractor.py

**Files:**
- Move: `src/reviewer.py` → `src/application/review.py`
- Move: `src/validator.py` → `src/application/validation.py`
- Move: `src/context_extractor.py` → `src/application/context_extraction.py`
- Build 3 shims

**Step 4.1: 三个一起迁（结构相同）**

```bash
git mv src/reviewer.py src/application/review.py
git mv src/validator.py src/application/validation.py
git mv src/context_extractor.py src/application/context_extraction.py
```

建 3 个 shim 到原路径，每个 1 行 `from .application.<new_name> import *`。

**Step 4.2: 修每个迁入文件的内部 import**

依次修改 `src/application/{review,validation,context_extraction}.py`，把 `from .xxx` 改为 `from src.xxx`（与 Task 1/2 同规则）。

**Step 4.3: 修 orchestration.py 内的引用**

把 `from src.reviewer` / `from src.validator` / `from src.context_extractor` 都改为新路径。

**Step 4.4: 测试 + 回归 + 提交**

```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
git add src/application/ src/reviewer.py src/validator.py src/context_extractor.py
git commit -m "refactor(app): 迁移 reviewer/validator/context_extractor → application/"
```

---

### Task 5: 结束 A-W1 里程碑

**Step 5.1: 确认 src/ 根只剩何物**

Run:
```bash
ls src/*.py
```

期望看到（仍在 src/ 根的）：`__init__.py`、5 个将删 shim + `claude_client.py` + `chat_input.py`（这两个在 A-W2 下沉）。**注意**：`orchestrator.py` `worker.py` `merger.py` `reviewer.py` `validator.py` `context_extractor.py` 此时是 shim（A-W3 才删）。

**Step 5.2: 提交里程碑（空 commit 作为锚点）**

```bash
git commit --allow-empty -m "chore(app): A-W1 完成 — 服务层全部迁入 application/ (暂保留 shim)"
git log --oneline -10
```

---

## 阶段 A-W2: infrastructure 扩展 + domain 抽取（3 个原子任务）

### Task 6: 迁移 claude_client.py → infrastructure/claude/client.py

**Files:**
- Create: `src/infrastructure/claude/__init__.py`
- Move: `src/claude_client.py` → `src/infrastructure/claude/client.py`
- Create: `src/claude_client.py` shim
- 更新所有消费方 import

**Step 6.1: 扫 claude_client 消费方**

```bash
grep -rn "from src\.claude_client\|from \.claude_client\|claude_client" --include="*.py" . | grep -v ".worktrees/" | grep -v __pycache__
```

**Step 6.2: 写失败测试**

追加到 `tests/test_application_layer.py`：
```python
    def test_claude_client_from_infrastructure(self):
        from src.infrastructure.claude.client import strip_code_fences
        self.assertTrue(callable(strip_code_fences))
```

Run 失败：
```bash
python3 -m unittest tests.test_application_layer -v 2>&1 | tail -10
```

**Step 6.3: 移动 + shim**

```bash
mkdir -p src/infrastructure/claude
: > src/infrastructure/claude/__init__.py
git mv src/claude_client.py src/infrastructure/claude/client.py
```

建 `src/claude_client.py`：
```python
"""Transitional shim — 迁移到 src.infrastructure.claude.client. A-W3 删除."""
from .infrastructure.claude.client import *  # noqa: F401,F403
```

**Step 6.4: 内部 import 修（如有）**

`src/infrastructure/claude/client.py` 内如果有 `from .xxx` 改为 `from src.xxx`。

**Step 6.5: 测试 + 回归 + 提交**

```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
git add src/infrastructure/claude/ src/claude_client.py tests/test_application_layer.py
git commit -m "refactor(infra): 迁移 claude_client.py → infrastructure/claude/client.py"
```

---

### Task 7: 迁移 chat_input.py → infrastructure/input/chat_input.py

**Files:**
- Create: `src/infrastructure/input/__init__.py`
- Move: `src/chat_input.py` → `src/infrastructure/input/chat_input.py`
- Create: `src/chat_input.py` shim

**Step 7.1-7.5:** 流程同 Task 6。

```bash
mkdir -p src/infrastructure/input
: > src/infrastructure/input/__init__.py
git mv src/chat_input.py src/infrastructure/input/chat_input.py
# 建 shim; 修 internal import
python3 -m unittest discover -s tests -q 2>&1 | tail -5
git add src/infrastructure/input/ src/chat_input.py
git commit -m "refactor(infra): 迁移 chat_input.py → infrastructure/input/chat_input.py"
```

---

### Task 8: WorkerResult 抽到 domain/worker_result.py

**Files:**
- Create: `src/domain/worker_result.py`（从 `src/application/worker.py` 抽出 `WorkerResult` dataclass）
- Modify: `src/application/worker.py` — import `from src.domain.worker_result import WorkerResult`
- Modify: 所有消费 `WorkerResult` 的文件（已在 Task 0.3 快照里）

**Step 8.1: 扫使用面**

```bash
grep -rn "WorkerResult" --include="*.py" . | grep -v ".worktrees/" | grep -v __pycache__
```

**Step 8.2: 写失败测试**

追加到 `tests/test_application_layer.py`：
```python
    def test_worker_result_in_domain(self):
        from src.domain.worker_result import WorkerResult
        r = WorkerResult(task_id="t1", success=True)
        self.assertEqual(r.task_id, "t1")

    def test_worker_result_still_reachable_via_worker(self):
        # application.worker 应 re-export,避免一次性改所有消费方
        from src.application.worker import WorkerResult
        self.assertTrue(WorkerResult.__module__.startswith("src.domain"))
```

Run 失败确认。

**Step 8.3: 创建 domain/worker_result.py**

从 `src/application/worker.py` 把 `@dataclass class WorkerResult` 剪切到：
```python
# src/domain/worker_result.py
"""纯数据 — Worker 执行结果。放在 domain 层让 application / perf / infra 皆可依赖。"""

from dataclasses import dataclass, field


@dataclass
class WorkerResult:
    """Worker 执行结果"""
    task_id: str
    success: bool
    output: str = ""
    error: str = ""
    session_id: str = ""
    cost_usd: float = 0.0
    duration_s: float = 0.0
    num_turns: int = 0
    model_used: str = ""
    worktree_path: str = ""
    stop_reason: str = ""
    retry_attempt: int = 0
    json_raw: dict = field(default_factory=dict)
```

**Step 8.4: application/worker.py re-export**

```python
# 顶部
from src.domain.worker_result import WorkerResult  # noqa: F401
```

**Step 8.5: 测试 + 回归 + 提交**

```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
git add src/domain/worker_result.py src/application/worker.py tests/test_application_layer.py
git commit -m "refactor(domain): WorkerResult 抽到 domain/worker_result.py"
```

---

## 阶段 A-W3: 删 shim + 更新入口（3 个原子任务）

### Task 9: 批量替换 shim 消费方 import

**Files:**
- Modify: 所有 `from src.{orchestrator,worker,merger,reviewer,validator,context_extractor,claude_client,chat_input,cli,monitor,task_parser,fs_utils,web_dashboard}` 消费方

**Step 9.1: 生成替换清单**

```bash
grep -rn "from src\.\(orchestrator\|worker\|merger\|reviewer\|validator\|context_extractor\|claude_client\|chat_input\|cli\|monitor\|task_parser\|fs_utils\|web_dashboard\)" --include="*.py" . | grep -v ".worktrees/" | grep -v __pycache__ | grep -v "src/\(cli\|monitor\|task_parser\|fs_utils\|web_dashboard\|orchestrator\|worker\|merger\|reviewer\|validator\|context_extractor\|claude_client\|chat_input\)\.py"
```

（排除 shim 文件自身。）

**Step 9.2: 替换规则**

| 旧 | 新 |
|---|---|
| `from src.orchestrator` | `from src.application.orchestration` |
| `from src.worker` | `from src.application.worker` |
| `from src.merger` | `from src.application.merge` |
| `from src.reviewer` | `from src.application.review` |
| `from src.validator` | `from src.application.validation` |
| `from src.context_extractor` | `from src.application.context_extraction` |
| `from src.claude_client` | `from src.infrastructure.claude.client` |
| `from src.chat_input` | `from src.infrastructure.input.chat_input` |
| `from src.cli` | `from src.app.cli` |
| `from src.monitor` | `from src.infrastructure.monitoring.rich_monitor` |
| `from src.task_parser` | `from src.domain.tasks` |
| `from src.fs_utils` | `from src.infrastructure.storage.atomic` |
| `from src.web_dashboard` | `from src.infrastructure.dashboard.server` |

**Step 9.3: 逐文件替换**

对 Step 9.1 产出的每个命中文件，用 Edit/sed 直接替换。

**Step 9.4: 更新顶层入口脚本**

- `run.py`
- `chat.py`
- `cpar`（shell wrapper，检查是否有 `from src.cli` 之类）

**Step 9.5: 测试 + 回归**

```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
python3 run.py --help 2>&1 | head -5
python3 run.py validate examples/auth-system.yaml 2>&1 | tail -10
```

**Step 9.6: 提交**

```bash
git add -u
git commit -m "refactor: 全仓替换 shim 消费方 import 到新路径"
```

---

### Task 10: 删除 5 个 shim + 原迁移 shim

**Files:** 删除 11 个 shim（5 个原 + 6 个迁移过渡）

**Step 10.1: 删除**

```bash
git rm src/cli.py src/monitor.py src/task_parser.py src/fs_utils.py src/web_dashboard.py
git rm src/orchestrator.py src/worker.py src/merger.py src/reviewer.py src/validator.py
git rm src/context_extractor.py src/claude_client.py src/chat_input.py
```

**Step 10.2: 验证没人还在用**

```bash
grep -rn "from src\.\(cli\|monitor\|task_parser\|fs_utils\|web_dashboard\|orchestrator\|worker\|merger\|reviewer\|validator\|context_extractor\|claude_client\|chat_input\)" --include="*.py" . | grep -v ".worktrees/" | grep -v __pycache__
```

Expected: 空输出

**Step 10.3: 测试 + 回归**

```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
python3 run.py --help
python3 run.py validate examples/auth-system.yaml
python3 run.py plan examples/auth-system.yaml
```

Expected: 全绿

**Step 10.4: 提交**

```bash
git commit -m "refactor: 删除 13 个 shim 文件 — src/ 根清零"
```

---

### Task 11: A-W2/W3 完成里程碑

**Step 11.1: 看 src/ 根状态**

```bash
ls src/*.py
```

Expected: 仅 `__init__.py`（若还有任何其它 .py 文件，是漏网之鱼，需要额外清理）。

**Step 11.2: 打 tag**

```bash
git commit --allow-empty -m "chore(app): A-W3 完成 — src/ 根清零,分层可读"
git tag track-a-layering-core-done
```

---

## 阶段 A-W4: perf/ 5 层切分（5 个原子任务 + 合流 Track B）

### Task 12: 建 perf/ 5 层子目录 + 透明转发

**Files:**
- Create: `src/perf/protocol/__init__.py`
- Create: `src/perf/capture/__init__.py`
- Create: `src/perf/decode/__init__.py`
- Create: `src/perf/analyze/__init__.py`
- Create: `src/perf/present/__init__.py`
- 注意：`src/perf/locate/` **已由 Track B 创建**，不重建

**Step 12.1: 确认 Track B 已合流**

```bash
git log --oneline | grep -i "locate\|track.b" | head -5
ls src/perf/locate/ 2>&1
```

如果 `locate/` 不存在，**暂停 A-W4**，等 Track B merge 到 main 后 rebase。

**Step 12.2: 创建 5 个子目录**

```bash
mkdir -p src/perf/{protocol,capture,decode,analyze,present}
for d in protocol capture decode analyze present; do : > src/perf/$d/__init__.py; done
```

**Step 12.3: 测试 + 回归**

```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
```

Expected: 无变化（空包不影响）

**Step 12.4: 提交**

```bash
git add src/perf/protocol/ src/perf/capture/ src/perf/decode/ src/perf/analyze/ src/perf/present/
git commit -m "refactor(perf): 建立 5 层子包目录 (protocol/capture/decode/analyze/present)"
```

---

### Task 13: 迁移 protocol/ 层模块

**Files:**
- Move: `src/perf/reconnect.py` → `src/perf/protocol/reconnect.py`
- Move: `src/perf/dvt_bridge.py` → `src/perf/protocol/dvt.py`
- Move: `src/perf/device_metrics.py` → `src/perf/protocol/device.py`
- 3 个 shim 过渡

**Step 13.1: 扫消费方**

```bash
grep -rn "from \.reconnect\|from .dvt_bridge\|from .device_metrics" --include="*.py" src/perf/
```

**Step 13.2: 移动 + 建 shim**

```bash
git mv src/perf/reconnect.py src/perf/protocol/reconnect.py
git mv src/perf/dvt_bridge.py src/perf/protocol/dvt.py
git mv src/perf/device_metrics.py src/perf/protocol/device.py
```

每个旧路径建 shim：
```python
# src/perf/reconnect.py
from .protocol.reconnect import *  # noqa: F401,F403
```
（dvt_bridge.py / device_metrics.py 同理）

**Step 13.3: 更新 perf 内部消费方**

把 `src/perf/session.py` / `src/perf/webcontent.py` 等里的 `from .reconnect` 改为 `from .protocol.reconnect`；`dvt_bridge` / `device_metrics` 同理。

**Step 13.4: 测试 + 回归 + 提交**

```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
git add -u
git commit -m "refactor(perf): 迁移 reconnect/dvt_bridge/device_metrics → protocol/"
```

---

### Task 14: 迁移 capture/ 与 decode/ 层模块

**Files:**
- Move: `src/perf/sampling.py` → `src/perf/capture/sampling.py`（内部拆分留到 Task 16）
- Move: `src/perf/webcontent.py` → `src/perf/capture/webcontent.py`
- Move: `src/perf/live_metrics.py` → `src/perf/capture/live_metrics.py`
- Move: `src/perf/live_log.py` → `src/perf/capture/live_log.py`
- Move: `src/perf/templates.py` → `src/perf/decode/templates.py`
- Move: `src/perf/deep_export.py` → `src/perf/decode/deep_export.py`
- Move: `src/perf/time_sync.py` → `src/perf/decode/time_sync.py`
- 7 个 shim

**Step 14.1-14.4:** 同 Task 13 模式，批量移动 + 建 shim + 更新 perf 内部消费方 + 回归 + 提交

```bash
git mv src/perf/sampling.py src/perf/capture/sampling.py
git mv src/perf/webcontent.py src/perf/capture/webcontent.py
git mv src/perf/live_metrics.py src/perf/capture/live_metrics.py
git mv src/perf/live_log.py src/perf/capture/live_log.py
git mv src/perf/templates.py src/perf/decode/templates.py
git mv src/perf/deep_export.py src/perf/decode/deep_export.py
git mv src/perf/time_sync.py src/perf/decode/time_sync.py

# 7 个 shim 逐个建
# 更新 perf/session.py / perf/integrator.py 里的 import

python3 -m unittest discover -s tests -q 2>&1 | tail -5
git add -u
git commit -m "refactor(perf): 迁移 capture/decode 层模块"
```

---

### Task 15: 迁移 analyze/ 与 present/ 层模块 + 拆 symbolicate.py 到 locate/

**Files:**
- Move: `src/perf/power_attribution.py` → `src/perf/analyze/power_attribution.py`
- Move: `src/perf/ai_diagnosis.py` → `src/perf/analyze/ai_diagnosis.py`
- Move: `src/perf/report_html.py` → `src/perf/present/report_html.py`
- **拆** `src/perf/symbolicate.py` → `src/perf/locate/dsym.py` + 合并进 `src/perf/locate/atos.py`（已存在）+ `src/perf/locate/cache.py`（已存在）
- **注意**：此步与 Track B 的 `locate/` 合流 —— 不动已存在的 `resolver.py` / `linkmap.py` / `atos.py` / `cache.py`，只添加 `dsym.py`

**Step 15.1: 拆 symbolicate.py 前的评估**

```bash
wc -l src/perf/symbolicate.py
grep -n "^def \|^class " src/perf/symbolicate.py | head -20
```

列出函数清单，分到三个目标位置：
- **`locate/dsym.py`**: `find_dsym_by_uuid`, `find_dsym_in_archives`, `find_dsym_in_derived_data`, `extract_binary_uuid`, `download_dsym_from_asc`, `auto_symbolicate`(facade)
- **`locate/atos.py`** (Track B 已建)：保持；`symbolicate_addresses` 的 subprocess 调用逻辑不复制（resolver 已经走 daemon）
- **`locate/cache.py`** (Track B 已建)：保持；`symbolicate.py` 里的 dsymutil cache 并入

**Step 15.2: 迁 analyze/ + present/**

```bash
git mv src/perf/power_attribution.py src/perf/analyze/power_attribution.py
git mv src/perf/ai_diagnosis.py src/perf/analyze/ai_diagnosis.py
git mv src/perf/report_html.py src/perf/present/report_html.py
```

建 3 shim 到原路径。

**Step 15.3: 拆 symbolicate.py 到 locate/dsym.py**

创建 `src/perf/locate/dsym.py`，把 symbolicate.py 中"找 dSYM"相关函数剪过来。

保留 `src/perf/symbolicate.py` 为 shim：
```python
"""Transitional — 迁移到 src.perf.locate.{dsym, resolver}. 将删."""
from .locate.dsym import *  # noqa: F401,F403

# auto_symbolicate 现在委托 resolver
def auto_symbolicate(*args, **kwargs):
    from .locate.dsym import auto_symbolicate as _impl
    return _impl(*args, **kwargs)
```

**Step 15.4: 更新消费方**

`src/app/perf_cli.py` 里调 `auto_symbolicate` 的位置保持不变（走 shim）。

**Step 15.5: 测试 + 回归**

```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
cpar perf --help 2>&1 | head -20
```

**Step 15.6: 提交**

```bash
git add -u
git add src/perf/analyze/ src/perf/present/ src/perf/locate/dsym.py
git commit -m "refactor(perf): 迁移 analyze/present 层 + 拆 symbolicate.py 到 locate/dsym.py"
```

---

### Task 16: sampling.py 拆分到 decode/timeprofiler.py

**Files:**
- Create: `src/perf/decode/timeprofiler.py`
- Modify: `src/perf/capture/sampling.py`（保留采集环，剥离解析）
- Test: `tests/test_decode_timeprofiler.py`

**Step 16.1: 定位要抽出的函数**

Run:
```bash
grep -n "^def parse_timeprofiler_xml\|^def aggregate_top_n\|^def export_xctrace_schema" src/perf/capture/sampling.py
```

这些函数 + 相关常量（regex 编译、helper）迁到 `decode/timeprofiler.py`。

**Step 16.2: 写失败测试**

`tests/test_decode_timeprofiler.py`：
```python
import unittest


class DecodeTimeprofilerTest(unittest.TestCase):

    def test_parse_importable_from_decode(self):
        from src.perf.decode.timeprofiler import parse_timeprofiler_xml, aggregate_top_n
        self.assertTrue(callable(parse_timeprofiler_xml))

    def test_sampling_still_exposes_for_compat(self):
        # capture/sampling.py 保留 re-export,避免一次性改所有消费方
        from src.perf.capture.sampling import parse_timeprofiler_xml
        self.assertTrue(callable(parse_timeprofiler_xml))


if __name__ == "__main__":
    unittest.main()
```

**Step 16.3: 创建 decode/timeprofiler.py**

把 `capture/sampling.py` 里 `parse_timeprofiler_xml` / `aggregate_top_n` / `export_xctrace_schema` 等解析函数剪到新文件。

**Step 16.4: capture/sampling.py 保留 re-export**

```python
# 顶部
from ..decode.timeprofiler import (
    parse_timeprofiler_xml,
    aggregate_top_n,
    export_xctrace_schema,
)
```

**Step 16.5: 测试 + 回归**

```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
wc -l src/perf/capture/sampling.py  # 应 <600
wc -l src/perf/session.py  # 应 <400 (此时可能还 >400,Task 17 处理)
```

**Step 16.6: 提交**

```bash
git add src/perf/decode/timeprofiler.py src/perf/capture/sampling.py tests/test_decode_timeprofiler.py
git commit -m "refactor(perf): sampling.py 的 parse/aggregate 抽到 decode/timeprofiler.py"
```

---

### Task 17: session.py 瘦身到 <400 行

**Files:**
- Modify: `src/perf/session.py`

**Step 17.1: 体检**

```bash
wc -l src/perf/session.py
grep -n "^def \|^class " src/perf/session.py
```

列出 session.py 当前所有公共函数，判断哪些不属于"生命周期编排"：
- 若有 export/parse 逻辑 → 搬到 `decode/`
- 若有 subprocess 直接调 xctrace → 搬到 `capture/`
- 若有报告格式化 → 搬到 `present/`

**Step 17.2: 批量搬移（逐函数，每个独立 commit）**

针对每个不属于编排的函数，重复：
1. 选定目标文件（如 `capture/xctrace_cmd.py` 新建 or 放入已有）
2. 剪切函数 + 相关常量
3. session.py 里保留 `from ..capture.xxx import <func>` re-export 过渡
4. 测试 + 回归 + 提交

**Step 17.3: 最终回归**

```bash
wc -l src/perf/session.py
# 目标 <400
python3 -m unittest discover -s tests -q 2>&1 | tail -5
# 真机冒烟 (如有设备)
cpar perf start --repo . --tag arch-smoke --sampling --device <UDID> --attach <app>
cpar perf stop --repo . --tag arch-smoke --clean
```

**Step 17.4: 提交里程碑**

```bash
git commit --allow-empty -m "chore(perf): session.py 瘦身到 <400 行"
```

---

### Task 18: 收窄 perf/__init__.py 的公共 API

**Files:**
- Modify: `src/perf/__init__.py`

**Step 18.1: 扫实际被外部使用的名字**

```bash
grep -rn "from src\.perf import " --include="*.py" . | grep -v ".worktrees/" | grep -v __pycache__ | grep -v "src/perf/"
```

列出所有在 `from src.perf import X, Y, Z` 中出现的 X/Y/Z，这就是必须保留的公共 API 清单。

**Step 18.2: 修改 perf/__init__.py**

把当前的 `from .xxx import *` 全换成显式名单：
```python
"""Perf 子系统公共 API — 仅此文件内声明的名字对外稳定。

所有 * import 已移除；加新符号请显式追加到下面的 __all__。
"""

from .config import PerfConfig
from .session import PerfSessionManager
from .integrator import PerfIntegrator
from .perf_defaults import PerfDefaults

from .protocol.reconnect import ReconnectableMixin, ReconnectPolicy
from .protocol.device import BatteryPoller, ProcessMetricsStreamer
from .capture.live_log import LiveLogAnalyzer, LogRule, DEFAULT_RULES
# ... (按 Step 18.1 产出的清单填完)

__all__ = [
    "PerfConfig",
    "PerfSessionManager",
    "PerfIntegrator",
    "PerfDefaults",
    "ReconnectableMixin",
    "ReconnectPolicy",
    "BatteryPoller",
    "ProcessMetricsStreamer",
    "LiveLogAnalyzer",
    "LogRule",
    "DEFAULT_RULES",
    # ...
]
```

**Step 18.3: 测试 + 回归**

```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
python3 run.py --help 2>&1 | head -5
cpar perf --help 2>&1 | head -20
```

**Step 18.4: 提交**

```bash
git add src/perf/__init__.py
git commit -m "refactor(perf): 收窄 __init__.py 公共 API 为显式列表 (无 * import)"
```

---

### Task 19: Track A 完成 — 最终验证

**Step 19.1: 检查所有成功标准**

```bash
# A1: src/ 根只剩 __init__.py
ls src/*.py | wc -l  # 预期: 1

# A2: perf/ 5 层存在 + session 瘦
ls -d src/perf/{protocol,capture,decode,locate,analyze,present}
wc -l src/perf/session.py  # <400

# A3: 无 * import in perf/__init__.py
grep "import \*" src/perf/__init__.py  # 预期: 空

# 回归测试
python3 -m unittest discover -s tests -q 2>&1 | tail -5

# CLI 回归
python3 run.py --help
python3 run.py validate examples/auth-system.yaml
cpar perf --help
```

**Step 19.2: 打 tag**

```bash
git commit --allow-empty -m "chore(arch): Track A 完成 — 分层骨架落成,shim 清零,perf 切 5 层"
git tag track-a-done
git log --oneline | head -25
```

---

## 完成标志

Track A 全部任务完成后：

```bash
# src/ 根只剩 __init__.py
ls src/*.py
# src/__init__.py

# 分层结构
find src -maxdepth 2 -type d | sort
# src
# src/app
# src/application
# src/domain
# src/infrastructure
# src/infrastructure/claude
# src/infrastructure/dashboard
# src/infrastructure/input
# src/infrastructure/monitoring
# src/infrastructure/storage
# src/perf
# src/perf/analyze
# src/perf/capture
# src/perf/decode
# src/perf/locate
# src/perf/present
# src/perf/protocol

# 测试全绿
python3 -m unittest discover -s tests -q
# Ran 50+ tests ... OK

# CLI 正常
python3 run.py plan examples/auth-system.yaml
cpar perf --help
```

**最终定量读数**（对齐 §1 成功标准）
- `src/` 根文件数：1（只有 `__init__.py`）
- `src/perf/session.py` 行数：<400
- `src/perf/sampling.py`（→ `capture/sampling.py`）行数：<600
- `perf/__init__.py` 无 `from .x import *`
