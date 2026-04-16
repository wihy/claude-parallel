# chat 模式输入体验优化 — 设计

日期: 2026-04-16
状态: 已确认，进入实现

## 背景

`chat.py:get_user_input()` 基于纯 `input()` 逐行读取，支持 `/done /clear /undo /show` 命令，空行结束。用户反馈五个痛点：

1. 粘贴含空行的多段文本会被误截断（空行触发结束）
2. 没有真正的多行编辑——回车后只能 `/undo` 整行删
3. 没有跨会话历史记录
4. 没有可视反馈（Markdown/YAML 无高亮）
5. 斜杠命令无补全/提示

## 方案

采用 `prompt_toolkit` 重写主输入器。选此方案是因为它原生解决五项痛点、是 InquirerPy 的传递依赖（无需新增包）、在 macOS/Linux 终端成熟稳定。

提交键绑定：`Esc` 然后 `Enter` 提交；`Enter` 换行。

## §1 架构

**新文件** `src/chat_input.py`，对外接口：

```python
class ChatInputSession:
    def __init__(self, history_path: Optional[Path] = None): ...
    def read_requirement(self, prompt: str = "") -> str
    def read_line(self, prompt: str, default: str = "") -> str
```

**改造** `chat.py`：
- `get_user_input` 改成 `ChatInputSession().read_requirement` 的包装
- `ask_text` 改走 `read_line`
- 其他 InquirerPy 调用保持不动

**依赖判定** `_can_use_pt()`：
1. `sys.stdin.isatty() == False` → 走 `read_requirement_from_stdin`
2. `prompt_toolkit` import 失败 → 回退现有 `input()` 实现
3. `$TERM == "dumb"` 或 `$CLAUDE_PARALLEL_NO_PT=1` → 降级
4. 否则启用

## §2 按键行为

| 按键 | 行为 |
|---|---|
| `Enter` | 插入换行 |
| `Esc` 然后 `Enter` | 提交 |
| `Ctrl+D` | 空 buffer 取消；非空提交 |
| `Ctrl+C` | 非空清空继续；空则 `KeyboardInterrupt` |
| `Tab` | 行首 `/` 触发补全，其他位置插入 Tab |
| `↑ / ↓` | 行内移动；边界切历史 |
| `Ctrl+R` | 历史模糊搜索 |

**空行不再触发提交** — 这是痛点 #1 的根因修复。

**斜杠命令兼容**：`/done /end` 单独占一行时等同提交；`/clear` 清空；`/undo` 删最后一行；`/help` 打印快捷键面板；`/paste` 下一行关闭命令拦截。`/show` 保留但多行编辑器已让它过时。

## §3 历史 / 补全 / 高亮

**历史**：`FileHistory($XDG_CACHE_HOME/claude-parallel/chat_history)`，默认 `~/.cache/...`。提交成功后追加，取消/清空不记。上限 500 条，启动时回写裁剪。`HistorySearchMode.AUTO`：按光标前缀筛选历史。

**补全**：`SlashCompleter(Completer)` 只在行首、首字符 `/` 时触发。列表：
- `/done` 提交
- `/clear` 清空
- `/undo` 删上一行
- `/help` 帮助
- `/paste` 下一行禁用命令

**高亮**：`PygmentsLexer`，默认 Markdown；检测前几行含 `project:` / `tasks:` / `- id:` 时切 YAML lexer；`monokai` 配色；终端不支持 256 色自动关。

**底栏**：`bottom_toolbar` 动态渲染快捷键 + 行数/字数/历史数计数；粘贴中显示 "Pasting..."；终端 <40 列隐藏底栏。

## §4 降级与错误处理

| 场景 | 行为 |
|---|---|
| 历史文件损坏 | 警告一行，退到 `InMemoryHistory` |
| 历史目录不可写 | 同上 |
| 终端 <40 列 | 隐藏底栏 |
| 粘贴超 8000 字 | 沿用 `normalize_requirement` 截断 |
| 补全菜单中按 Ctrl+C | 仅关菜单（prompt_toolkit 默认） |
| Windows 终端不支持 | import 失败则降级 |

## 测试

**冒烟** `tests/test_chat_input.py`：
1. 模块 import 不炸
2. `ChatInputSession` 在有/无 prompt_toolkit 两条路径都能构造
3. `_can_use_pt()` 在 `isatty()=False` 时返回 False
4. 斜杠补全器对 `/` 返回补全、对 `你好` 返回 0
5. `normalize_requirement` 粘贴含空行的多段文本保留空行
6. 历史文件首启自动创建；追加后行数 +1

**手工验收**：
- 粘贴含空行的 Markdown 大段需求完整保留
- 方向键在多行间自由移动
- ↑ 调出上次输入
- `/` + Tab 出补全菜单
- 底栏可见
- `CLAUDE_PARALLEL_NO_PT=1 cpar chat` 回到旧行为
- `echo '需求' | cpar chat` 非交互路径正常
