"""
chat 模式富输入器 — 基于 prompt_toolkit 的多行编辑/历史/补全/高亮体验。

对外入口:
    session = ChatInputSession()
    text = session.read_requirement(prompt="请描述需求")
    line = session.read_line(prompt="repo 路径", default="~/proj")

降级策略 (按顺序判定):
    1. 非 TTY                              → 调用方应走 read_requirement_from_stdin
    2. prompt_toolkit 不可用               → 回退到 input() + readline 多行
    3. $TERM=dumb 或 $CLAUDE_PARALLEL_NO_PT → 同上降级

空行不再触发提交 (痛点 #1 的根因修复)。提交靠 Esc+Enter / Ctrl+D。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable, List, Optional

# ── prompt_toolkit 可选依赖 ───────────────────────────────
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.filters import Condition
    _HAS_PT = True
except ImportError:
    _HAS_PT = False

try:
    from prompt_toolkit.lexers import PygmentsLexer
    from pygments.lexers.markup import MarkdownLexer
    from pygments.lexers.data import YamlLexer
    _HAS_LEXER = True
except ImportError:
    _HAS_LEXER = False


SLASH_COMMANDS: List[tuple[str, str]] = [
    ("/done", "提交输入"),
    ("/end", "提交输入"),
    ("/clear", "清空已输入内容"),
    ("/undo", "删除最后一行"),
    ("/help", "显示快捷键面板"),
    ("/paste", "下一行禁用命令拦截（粘贴以 / 开头的文本）"),
]

_HISTORY_MAX_LINES = 500
_MIN_TOOLBAR_COLS = 40
_INSTALL_HINT_SHOWN = False


def _cache_dir() -> Path:
    """XDG 兼容的缓存目录."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "claude-parallel"


def _history_path() -> Path:
    return _cache_dir() / "chat_history"


def _trim_history_file(path: Path, max_lines: int = _HISTORY_MAX_LINES) -> None:
    """把历史文件裁剪到 max_lines；损坏则删除重建。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return
    if len(lines) <= max_lines * 3:  # prompt_toolkit 每条记录多行，留足冗余
        return
    try:
        path.write_text("".join(lines[-max_lines * 3:]), encoding="utf-8")
    except OSError:
        pass


def _make_history():
    """优先 FileHistory，失败退 InMemoryHistory."""
    if not _HAS_PT:
        return None
    path = _history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.touch()
        _trim_history_file(path)
        return FileHistory(str(path))
    except OSError as e:
        print(f"  [提示] 历史文件不可用 ({e!s})，仅会话内记忆", file=sys.stderr)
        return InMemoryHistory()


def _maybe_print_install_hint() -> None:
    """首次降级时提示用户安装可选依赖."""
    global _INSTALL_HINT_SHOWN
    if _INSTALL_HINT_SHOWN:
        return
    _INSTALL_HINT_SHOWN = True
    print(
        "  [提示] 安装 prompt_toolkit + pygments 可启用多行编辑/历史/补全:\n"
        "         pip install prompt_toolkit pygments",
        file=sys.stderr,
    )


def _can_use_pt() -> bool:
    """判定是否启用 prompt_toolkit 富输入."""
    if not _HAS_PT:
        return False
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    if os.environ.get("CLAUDE_PARALLEL_NO_PT", "") not in ("", "0", "false", "False"):
        return False
    return True


# ── 斜杠命令补全器 ───────────────────────────────────────

if _HAS_PT:

    class SlashCompleter(Completer):
        """只在行首首字符是 / 时触发补全."""

        def get_completions(self, document, complete_event) -> Iterable[Completion]:
            # 当前行首到光标
            line = document.current_line_before_cursor
            if not line.startswith("/"):
                return
            token = line  # 还没空格分割之前的整个 token
            for cmd, desc in SLASH_COMMANDS:
                if cmd.startswith(token):
                    yield Completion(
                        cmd,
                        start_position=-len(token),
                        display=cmd,
                        display_meta=desc,
                    )

else:
    SlashCompleter = None  # type: ignore


# ── 主入口 ───────────────────────────────────────────────

class ChatInputSession:
    """封装 chat 模式下的富输入会话。

    无 prompt_toolkit 时所有方法降级到 input() 实现，但接口一致。
    """

    def __init__(self, history_path: Optional[Path] = None):
        self.enabled = _can_use_pt()
        self._session = None
        self._history = None
        self._paste_once = False  # /paste 后临时关闭一次命令拦截

        if not self.enabled:
            if _HAS_PT is False and sys.stdin.isatty():
                _maybe_print_install_hint()
            return

        # 构造 prompt_toolkit session
        self._history = _make_history()

        kb = KeyBindings()

        @kb.add("escape", "enter")
        def _(event):
            """Esc Enter 提交."""
            event.current_buffer.validate_and_handle()

        @kb.add("c-d")
        def _(event):
            """Ctrl+D: 空则取消，非空提交."""
            buf = event.current_buffer
            if buf.text:
                buf.validate_and_handle()
            else:
                event.app.exit(exception=EOFError)

        @kb.add("c-c")
        def _(event):
            """Ctrl+C: 非空清空继续，空则抛 KeyboardInterrupt."""
            buf = event.current_buffer
            if buf.text:
                buf.text = ""
                buf.cursor_position = 0
            else:
                event.app.exit(exception=KeyboardInterrupt)

        def _bottom_toolbar():
            try:
                cols = os.get_terminal_size().columns
            except OSError:
                cols = 80
            if cols < _MIN_TOOLBAR_COLS:
                return ""
            return (
                "Esc+Enter=提交  Enter=换行  Ctrl+C=清空  "
                "Ctrl+D=取消  Tab=补全  /help"
            )

        lexer = None
        if _HAS_LEXER:
            # 默认 Markdown；内容像 YAML 时切换（后面在 read_requirement 里动态切）
            lexer = PygmentsLexer(MarkdownLexer)

        completer = SlashCompleter() if SlashCompleter else None

        self._session = PromptSession(
            multiline=True,
            history=self._history,
            completer=completer,
            complete_while_typing=False,  # 只在 Tab 时触发，避免噪音
            key_bindings=kb,
            bottom_toolbar=_bottom_toolbar,
            lexer=lexer,
            enable_history_search=True,
            mouse_support=False,
        )
        self._markdown_lexer = lexer
        self._yaml_lexer = PygmentsLexer(YamlLexer) if _HAS_LEXER else None

    # ── 对外 API ─────────────────────────────────────────

    def read_requirement(self, prompt_text: str = "") -> str:
        """读取多行需求描述。"""
        if not self.enabled:
            return self._read_requirement_fallback(prompt_text)

        if prompt_text:
            print(prompt_text)
        print(
            "  (多行编辑; Esc+Enter 提交; /help 帮助)",
            flush=True,
        )

        while True:
            try:
                text = self._session.prompt("  > ")
            except KeyboardInterrupt:
                return ""
            except EOFError:
                return ""

            # 处理斜杠命令（在输入完成后检查整体文本）
            handled, processed = self._handle_slash_in_submission(text)
            if handled is None:
                # 命令要求重新输入
                continue
            if handled is False:
                # 无命令，按普通文本处理
                processed = text

            # 动态切 lexer（下次输入用）
            if self._markdown_lexer and self._yaml_lexer:
                self._session.lexer = (
                    self._yaml_lexer if _looks_like_yaml(processed) else self._markdown_lexer
                )

            return processed.strip()

    def read_line(self, prompt_text: str, default: str = "") -> str:
        """单行输入（用于引导向导的字段收集）。"""
        if not self.enabled:
            return self._read_line_fallback(prompt_text, default)

        hint = f" (默认: {default})" if default else ""
        try:
            answer = self._session.prompt(
                f"  {prompt_text}{hint}: ",
                multiline=False,
                default="",
                key_bindings=None,
            )
        except (KeyboardInterrupt, EOFError):
            return default
        answer = answer.strip()
        return answer if answer else default

    # ── 内部: 斜杠命令处理 ───────────────────────────────

    def _handle_slash_in_submission(self, text: str) -> tuple[Optional[bool], str]:
        """
        处理提交文本中的斜杠命令。
        返回 (handled, processed_text):
            handled=True  → 命令已执行（例如 /done 当作提交）
            handled=False → 不是命令，按普通文本
            handled=None  → 命令消费了提交，需要重新输入（如 /clear）
        """
        if self._paste_once:
            self._paste_once = False
            return False, text

        stripped = text.strip()
        # 整体输入只有一个命令
        if stripped in ("/done", "/end"):
            return True, ""  # 空提交视为取消
        if stripped == "/clear":
            print("  [已清空]")
            return None, ""
        if stripped == "/undo":
            # 删最后一行，重新进入 prompt（prompt_toolkit 自身有行内删除，这里是兼容旧肌肉）
            lines = text.splitlines()
            if len(lines) > 1:
                print(f"  [删除最后一行: {lines[-1][:60]}]")
                # 写回 history 作为下次↑回填不现实，直接重进
            return None, ""
        if stripped == "/help":
            _print_help()
            return None, ""
        if stripped == "/paste":
            self._paste_once = True
            print("  [下次提交将不拦截斜杠命令]")
            return None, ""

        # 文本结尾是单独一行 /done / /end —— 老用户兼容
        lines = text.splitlines()
        if lines and lines[-1].strip() in ("/done", "/end"):
            return True, "\n".join(lines[:-1])

        return False, text

    # ── 降级路径 ─────────────────────────────────────────

    def _read_requirement_fallback(self, prompt_text: str) -> str:
        """无 prompt_toolkit 时的旧式多行输入（保留原有体验）。"""
        try:
            import readline  # noqa: F401
        except ImportError:
            pass

        if prompt_text:
            print(prompt_text)
        print("  (多行输入; 空行或 /done 结束; /clear 清空 /undo 删上一行 /show 预览)")
        lines: List[str] = []
        while True:
            try:
                prompt_str = f"  [{len(lines) + 1}] " if lines else "  > "
                line = input(prompt_str)
            except EOFError:
                break
            except KeyboardInterrupt:
                if lines:
                    print()
                    print(f"  [已清空 {len(lines)} 行，重新输入]")
                    lines = []
                    continue
                return ""

            text = line.strip()
            if text in ("/done", "/end"):
                break
            if text == "/clear":
                lines = []
                print("  [已清空]")
                continue
            if text == "/undo":
                if lines:
                    removed = lines.pop()
                    print(f"  [删除: {removed}]")
                continue
            if text == "/show":
                for i, l in enumerate(lines, 1):
                    print(f"    {i}| {l}")
                continue
            if text == "" and not lines:
                continue
            if text == "":
                break
            lines.append(line)
        return "\n".join(lines).strip()

    def _read_line_fallback(self, prompt_text: str, default: str) -> str:
        hint = f" (默认: {default})" if default else ""
        try:
            answer = input(f"  {prompt_text}{hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return default
        return answer if answer else default


# ── helpers ──────────────────────────────────────────────

def _looks_like_yaml(text: str) -> bool:
    """粗略判定文本是否为 YAML（用于切换 lexer）。"""
    head = "\n".join(text.splitlines()[:5])
    return any(key in head for key in ("project:", "tasks:", "- id:"))


def _print_help() -> None:
    print("  ── 快捷键 ──")
    print("    Esc Enter       提交输入")
    print("    Enter           插入换行")
    print("    Ctrl+C          非空清空 / 空则退出")
    print("    Ctrl+D          非空提交 / 空则退出")
    print("    Tab             斜杠命令补全")
    print("    ↑ / ↓           行内移动；边界切历史")
    print("    Ctrl+R          历史模糊搜索")
    print("  ── 斜杠命令 ──")
    for cmd, desc in SLASH_COMMANDS:
        print(f"    {cmd:<10} {desc}")
