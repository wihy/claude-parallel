"""
src/chat_input.py 冒烟测试 — 不依赖真 TTY，覆盖 6 项关键路径。

运行:
    python3 tests/test_chat_input.py
"""

import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def t(name):
    def deco(fn):
        def wrapped():
            try:
                fn()
                print(f"  ✓ {name}")
            except AssertionError as e:
                print(f"  ✗ {name}: {e}")
                sys.exit(1)
            except Exception as e:
                print(f"  ✗ {name}: {type(e).__name__}: {e}")
                sys.exit(1)
        return wrapped
    return deco


@t("模块 import 不炸")
def test_import():
    from src import chat_input
    assert hasattr(chat_input, "ChatInputSession")
    assert hasattr(chat_input, "SLASH_COMMANDS")


@t("ChatInputSession 在无 TTY 时构造成功且 enabled=False")
def test_construct_no_tty():
    from src.chat_input import ChatInputSession
    s = ChatInputSession()
    # stdin 在 pytest 下非 TTY，应降级
    assert s.enabled is False


@t("_can_use_pt() 在 isatty=False 时返回 False")
def test_can_use_pt_no_tty():
    from src.chat_input import _can_use_pt
    assert _can_use_pt() is False


@t("SlashCompleter 对 / 返回 6 个补全")
def test_completer():
    from src.chat_input import SlashCompleter, SLASH_COMMANDS, _HAS_PT
    if not _HAS_PT:
        print(" (跳过: prompt_toolkit 未安装)", end="")
        return
    from prompt_toolkit.document import Document
    comp = SlashCompleter()

    doc = Document("/")
    got = [c.text for c in comp.get_completions(doc, None)]
    assert len(got) == len(SLASH_COMMANDS), f"expected {len(SLASH_COMMANDS)}, got {got}"

    doc = Document("你好")
    got = list(comp.get_completions(doc, None))
    assert got == []

    doc = Document("/do")
    got = [c.text for c in comp.get_completions(doc, None)]
    assert got == ["/done"]


@t("normalize_requirement 保留空行")
def test_normalize():
    import chat
    text = "第一段\n\n第二段，前面有空行\n\n第三段"
    out = chat.normalize_requirement(text)
    assert "\n\n" in out, f"空行被吃掉了: {out!r}"
    assert out.count("\n\n") == 2


@t("降级路径: /done 终止 + 保留 /undo /clear 语义")
def test_fallback_behavior():
    from src.chat_input import ChatInputSession
    s = ChatInputSession()

    sys.stdin = io.StringIO("line 1\nline 2\n/done\n")
    r = s._read_requirement_fallback("")
    assert r == "line 1\nline 2"

    sys.stdin = io.StringIO("keep1\ndrop\n/undo\nkeep2\n/done\n")
    r = s._read_requirement_fallback("")
    assert "drop" not in r
    assert "keep1" in r and "keep2" in r

    sys.stdin = io.StringIO("junk\n/clear\nreal\n/done\n")
    r = s._read_requirement_fallback("")
    assert "junk" not in r
    assert "real" in r


@t("历史文件: 首启创建 + 不可写时退到 InMemoryHistory")
def test_history():
    from src.chat_input import _make_history, _history_path, _HAS_PT
    if not _HAS_PT:
        print(" (跳过: prompt_toolkit 未安装)", end="")
        return

    # 用临时 XDG_CACHE_HOME
    with tempfile.TemporaryDirectory() as td:
        old = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = td
        try:
            h = _make_history()
            path = _history_path()
            assert path.exists(), f"历史文件未创建: {path}"
        finally:
            if old is None:
                del os.environ["XDG_CACHE_HOME"]
            else:
                os.environ["XDG_CACHE_HOME"] = old


@t("_looks_like_yaml 识别 project:/tasks:/- id: 关键字")
def test_yaml_detect():
    from src.chat_input import _looks_like_yaml
    assert _looks_like_yaml("project:\n  repo: foo")
    assert _looks_like_yaml("tasks:\n  - id: x")
    assert not _looks_like_yaml("我要做一个登录系统")
    assert not _looks_like_yaml("## 需求\n\n写一个 todo")


if __name__ == "__main__":
    tests = [
        test_import,
        test_construct_no_tty,
        test_can_use_pt_no_tty,
        test_completer,
        test_normalize,
        test_fallback_behavior,
        test_history,
        test_yaml_detect,
    ]
    print(f"执行 {len(tests)} 项冒烟测试:")
    for fn in tests:
        fn()
    print(f"\n全部通过 ({len(tests)}/{len(tests)})")
