#!/usr/bin/env python3
"""
claude-parallel 对话模式 — 用自然语言描述需求，自动拆分并执行。

流程:
1. 用户输入自然语言需求描述
2. 调用 Claude 分析需求，生成任务 YAML
3. 用户确认/修改 YAML
4. 自动校验 → 执行 → 合并

用法:
    cpar chat
    cpar chat --repo ~/myproject
    cpar chat --repo ~/myproject --auto   # 跳过确认，全自动
    cpar chat -i                           # 引导向导模式
"""

import os
import sys
import time
import tempfile
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from src.validator import TaskValidator
from src.chat_input import ChatInputSession

# Rich UI
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.syntax import Syntax
from rich.rule import Rule

console = Console()


SYSTEM_PROMPT = """你是一个任务拆分专家。用户会用自然语言描述一个软件项目需求，你需要将其拆分为多个可以并行执行的任务。

重要: 仅输出 YAML，不要输出其他分析或解释文字。一次性输出完整 YAML。

输出格式: 直接输出 YAML（不要 markdown 代码块），格式如下:

project:
  repo: __REPO__
  max_workers: 3
  default_max_turns: 15
  default_max_budget_usd: 2.0
  retry_count: 1

tasks:
  - id: 简短英文id
    description: |
      详细描述任务，包含要创建/修改的文件、函数名、接口签名等。
      描述要足够详细，让 Claude Code 能独立完成。
    files: [涉及的文件路径列表]
    allowed_tools: ["Read", "Write", "Edit", "Bash"]
    depends_on: [依赖的任务id列表]

拆分原则:
1. 每个任务应该可以独立在一个 git worktree 中完成
2. 不同任务修改的文件尽量不要重叠
3. 有依赖关系的任务通过 depends_on 指定
4. description 要详细，包含文件路径、函数签名、接口约定等
5. 优先拆出可以并行的独立模块
6. 通常 3-8 个任务为宜，不要拆得太细
7. repo 字段保持 __REPO__ 占位符，系统会自动替换
8. id 只用小写英文和连字符，不要用下划线或空格

任务类型与参数建议:
- 研究类任务 (涉及 WebSearch/WebFetch): max_turns >= 15, max_budget_usd >= 2.0
- 重型代码任务 (多文件创建/重构):     max_turns >= 12, max_budget_usd >= 1.5
- 轻量任务 (单文件修改/文档/测试):     可使用 project 默认值，无需单独指定
- 如果某个任务需要联网搜索、API 调用等耗时操作，allowed_tools 中加入 WebSearch 和 WebFetch
- 汇总/报告类任务 depends_on 其他所有任务，放在最后执行
"""


_CHAT_INPUT: ChatInputSession | None = None


def _chat_input() -> ChatInputSession:
    """模块级单例，共享历史记录."""
    global _CHAT_INPUT
    if _CHAT_INPUT is None:
        _CHAT_INPUT = ChatInputSession()
    return _CHAT_INPUT


def get_user_input(prompt: str) -> str:
    """获取用户输入，支持多行（prompt_toolkit 富编辑；缺依赖时降级到 input()）。

    终止方式:
    - Esc Enter (prompt_toolkit 模式) / 空行 (降级模式) / /done
    - Ctrl+D (EOF)
    - Ctrl+C 清空 buffer 继续，二次退出
    """
    return _chat_input().read_requirement(prompt)


def read_requirement_from_stdin() -> str:
    """当 stdin 非交互时，读取整段输入。"""
    try:
        data = sys.stdin.read()
    except Exception:
        return ""
    return (data or "").strip()


def normalize_requirement(text: str, max_chars: int = 8000) -> str:
    """规范化用户需求文本，避免超长/空白异常。"""
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "\n\n[...已截断，输入过长...]"
    return cleaned


def extract_yaml(text: str) -> str:
    """从 Claude 输出中提取纯 YAML 部分。

    Claude 经常在 YAML 前面输出一段思考/解释文字，需要自动剥离。
    策略: 找到第一个顶级 YAML 键 (project: / tasks:) 并截取从这里开始的内容。
    """
    if not text:
        return text

    lines = text.split("\n")
    yaml_start = -1

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped in ("```yaml", "```yml", "```"):
            continue
        if stripped.startswith("project:") or stripped.startswith("tasks:"):
            yaml_start = i
            break

    if yaml_start < 0:
        return text

    extracted = "\n".join(lines[yaml_start:])

    result_lines = []
    for line in extracted.split("\n"):
        stripped = line.strip()
        if not stripped:
            result_lines.append(line)
            continue
        if (not line[0:1].isspace()
                and not stripped.startswith("-")
                and not stripped.startswith("#")
                and ":" not in stripped
                and len(stripped) > 20
                and not stripped.startswith("project:")
                and not stripped.startswith("tasks:")
                and not stripped.startswith("repo:")
                and not stripped.startswith("default_")):
            break
        result_lines.append(line)

    return "\n".join(result_lines).strip()


# ── 交互式选择器 (InquirerPy + 降级) ──────────────────────

def _can_use_inquirer():
    """检查是否可以使用 InquirerPy（需要 TTY）。"""
    if not sys.stdin.isatty():
        return False
    try:
        from InquirerPy import inquirer  # noqa: F401
        return True
    except ImportError:
        return False


def ask_choice(prompt: str, options: list, default: str = "") -> str:
    """单选提示。options 为 [(key, label), ...]，返回选中的 key。"""
    if _can_use_inquirer():
        from InquirerPy import inquirer
        choices = [{"name": label, "value": key} for key, label in options]
        try:
            return inquirer.select(
                message=prompt,
                choices=choices,
                default=default,
            ).execute()
        except (KeyboardInterrupt, EOFError):
            return default

    # 降级: 纯文本选择
    console.print(f"  {prompt}")
    for key, label in options:
        marker = " [dim](默认)[/]" if key == default else ""
        console.print(f"    [{key}] {label}{marker}")
    try:
        answer = input("  > ").strip().lower()
    except EOFError:
        answer = ""
    if not answer and default:
        return default
    for key, label in options:
        if answer == key or answer == label:
            return key
    try:
        idx = int(answer) - 1
        if 0 <= idx < len(options):
            return options[idx][0]
    except (ValueError, IndexError):
        pass
    return default if default else (options[0][0] if options else "")


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    """是/否提示。"""
    if _can_use_inquirer():
        from InquirerPy import inquirer
        try:
            return inquirer.confirm(message=prompt, default=default).execute()
        except (KeyboardInterrupt, EOFError):
            return default

    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"  {prompt} {hint} ").strip().lower()
    except EOFError:
        answer = ""
    if not answer:
        return default
    return answer in ("y", "yes", "是")


def ask_text(prompt: str, default: str = "") -> str:
    """单行文本输入。"""
    if _can_use_inquirer():
        from InquirerPy import inquirer
        try:
            return inquirer.text(message=prompt, default=default).execute()
        except (KeyboardInterrupt, EOFError):
            return default

    hint = f" (默认: {default})" if default else ""
    try:
        answer = input(f"  {prompt}{hint}: ").strip()
    except EOFError:
        answer = ""
    return answer if answer else default


# ── 引导向导 ──────────────────────────────────────────────

def interactive_step_wizard() -> dict:
    """逐步引导问答式向导，收集任务拆分所需的全部信息。"""
    console.print()
    console.print(Panel(
        "[bold cyan]Claude Parallel[/] — [bold]引导模式[/]\n\n"
        "我会通过几个问题帮你理清需求，然后自动拆分并执行。\n"
        "随时可以按 Enter 使用默认值。",
        border_style="cyan",
        padding=(1, 2),
    ))
    console.print()

    # Step 1: 项目路径
    console.print(Rule("[bold]Step 1/7: 项目路径[/]", style="cyan"))
    repo = ask_text("项目所在目录", default=os.getcwd())
    console.print()

    # Step 2: 需求描述
    console.print(Rule("[bold]Step 2/7: 需求描述[/]", style="cyan"))
    console.print("请描述你想让 Claude 帮你完成什么 (多行输入，空行或 /done 结束):")
    requirement = get_user_input("")
    if not requirement:
        requirement = ask_text("一句话描述", default="")
    console.print()

    # Step 3: 技术栈
    console.print(Rule("[bold]Step 3/7: 技术栈[/]", style="cyan"))
    tech = ask_choice(
        "项目使用的主要技术栈?",
        [
            ("python", "Python (Flask/Django/FastAPI/...)"),
            ("js", "JavaScript/TypeScript (React/Vue/Node/...)"),
            ("go", "Go"),
            ("rust", "Rust"),
            ("java", "Java (Spring/...)"),
            ("mixed", "混合/其他"),
        ],
        default="python",
    )
    console.print()

    # Step 4: 目标文件
    console.print(Rule("[bold]Step 4/7: 目标文件 [dim](可选)[/][/]", style="cyan"))
    console.print("[dim]是否有明确要创建或修改的文件路径? 没有可以直接回车跳过。[/]")
    target_files = ask_text("文件路径（多个用逗号分隔）", default="")
    console.print()

    # Step 5: 附加选项
    console.print(Rule("[bold]Step 5/7: 附加选项[/]", style="cyan"))
    want_tests = ask_yes_no("是否需要自动生成单元测试?", default=True)
    want_docs = ask_yes_no("是否需要生成/更新文档?", default=False)
    console.print()

    # Step 6: 执行策略
    console.print(Rule("[bold]Step 6/7: 执行策略[/]", style="cyan"))
    workers_choice = ask_choice(
        "并行执行数?",
        [
            ("2", "2 个 (稳妥，适合小项目)"),
            ("3", "3 个 (推荐)"),
            ("4", "4 个 (激进，适合大项目)"),
        ],
        default="3",
    )
    want_merge = ask_yes_no("完成后是否自动合并到主分支?", default=True)
    console.print()

    # Step 7: 预算
    console.print(Rule("[bold]Step 7/7: 预算[/]", style="cyan"))
    budget_str = ask_text("总预算上限 ($)", default="5.0")
    try:
        budget = float(budget_str)
    except ValueError:
        budget = 5.0
    console.print()

    # 汇总确认 — Rich Table
    console.print(Rule("[bold]确认汇总[/]", style="green"))
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="bold cyan", width=12)
    table.add_column("Value")
    table.add_row("项目路径", repo)
    req_preview = requirement[:80] + ("..." if len(requirement) > 80 else "")
    table.add_row("需求", req_preview)
    table.add_row("技术栈", tech)
    if target_files:
        table.add_row("目标文件", target_files)
    table.add_row("单元测试", "[green]是[/]" if want_tests else "[dim]否[/]")
    table.add_row("生成文档", "[green]是[/]" if want_docs else "[dim]否[/]")
    table.add_row("并行数", str(workers_choice))
    table.add_row("自动合并", "[green]是[/]" if want_merge else "[dim]否[/]")
    table.add_row("总预算", f"${budget:.1f}")
    console.print(table)
    console.print()

    confirm = ask_yes_no("确认以上信息，开始拆分?", default=True)
    if not confirm:
        console.print("[yellow]已取消。[/]")
        return {}

    # 组装 enriched requirement
    enriched_parts = [requirement]
    if tech and tech != "mixed":
        stack_names = {
            "python": "Python",
            "js": "JavaScript/TypeScript",
            "go": "Go",
            "rust": "Rust",
            "java": "Java",
        }
        enriched_parts.append(f"技术栈: {stack_names.get(tech, tech)}")

    if target_files:
        enriched_parts.append(f"涉及的文件: {target_files}")

    if want_tests:
        enriched_parts.append("需要为新增代码编写单元测试")

    if want_docs:
        enriched_parts.append("需要生成或更新相关文档")

    return {
        "repo": repo,
        "requirement": "\n".join(enriched_parts),
        "tech_stack": tech,
        "target_files": target_files,
        "want_tests": want_tests,
        "want_docs": want_docs,
        "want_merge": want_merge,
        "max_workers": int(workers_choice),
        "budget": budget,
    }


# ── 错误检测工具 ──────────────────────────────────────────

def _is_budget_error(text: str) -> bool:
    t = (text or "").lower()
    return (
        "exceeded usd budget" in t
        or ("budget" in t and "exceed" in t)
    )


def _is_quota_error(text: str) -> bool:
    t = (text or "").lower()
    return (
        "usage_limit_reached" in t
        or "insufficient_quota" in t
        or ("quota" in t and "exceed" in t)
    )


def _is_retryable_error(text: str) -> bool:
    t = (text or "").lower()
    patterns = [
        "rate limit", "429", "503", "overloaded", "timeout",
        "connection", "econnreset", "temporary", "try again",
    ]
    return any(p in t for p in patterns)


def _is_turns_error(text: str) -> bool:
    t = (text or "").lower()
    return "max turns" in t or "max_turns" in t


def parse_model_chain(models_arg: str) -> list:
    """解析逗号分隔的模型链参数。空字符串表示使用 CLI 默认模型。"""
    if not models_arg:
        return [""]
    models = [m.strip() for m in models_arg.split(",") if m.strip()]
    return models if models else [""]


# ── Claude 调用 ───────────────────────────────────────────

def call_claude(
    prompt: str,
    system: str = "",
    planner_budget: float = 0.8,
    planner_retries: int = 2,
    planner_models: list = None,
) -> tuple:
    """调用 claude CLI，返回 (success, output, error_message)

    自动重试策略:
    - 预算不足: 自动提高预算重试 (最多 3.0)
    - 临时错误: 退避重试
    - 模型回退: 当前模型失败后切换下一个模型重试
    """
    full_prompt = prompt
    if system:
        full_prompt = f"{system}\n\n---\n\n{prompt}"

    max_attempts = max(1, planner_retries + 1)
    model_chain = planner_models or [""]
    last_error = "未知错误"

    for model_idx, model_name in enumerate(model_chain):
        current_budget = max(0.1, float(planner_budget))
        if model_name:
            console.print(f"  [cyan][planner][/cyan] 使用模型: [bold]{model_name}[/] ({model_idx + 1}/{len(model_chain)})")
        else:
            console.print(f"  [cyan][planner][/cyan] 使用默认模型 ({model_idx + 1}/{len(model_chain)})")

        for attempt in range(1, max_attempts + 1):
            cmd = [
                "claude", "-p", full_prompt,
                "--output-format", "text",
                "--max-turns", "6",
                "--max-budget-usd", f"{current_budget:.3f}",
                "--dangerously-skip-permissions",
            ]
            if model_name:
                cmd.extend(["--model", model_name])

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )

                stdout_text = (result.stdout or "").strip()
                stderr_text = (result.stderr or "").strip()
                output = stdout_text if stdout_text else stderr_text

                # 调试输出
                console.print(f"  [dim]\\[debug] returncode={result.returncode} stdout={len(stdout_text)}c stderr={len(stderr_text)}c[/]")
                if not output:
                    console.print(f"  [dim]\\[debug] 无输出[/]")

                if result.returncode == 0 and output:
                    # 去除可能的 markdown 代码块包裹
                    if output.startswith("```yaml"):
                        output = output[7:]
                    if output.startswith("```"):
                        output = output[3:]
                    if output.endswith("```"):
                        output = output[:-3]

                    cleaned = output.strip()
                    if cleaned.lower().startswith("error:"):
                        console.print(f"  [dim]\\[debug] 被识别为错误: {cleaned[:200]}[/]")
                        last_error = cleaned
                    else:
                        return (True, cleaned, "")
                else:
                    last_error = output or f"claude exit code={result.returncode}"
                    console.print(f"  [dim]\\[debug] 失败原因: {last_error[:200]}[/]")

                # 是否继续重试
                if attempt < max_attempts:
                    if _is_quota_error(last_error):
                        break

                    if _is_turns_error(last_error):
                        break

                    if _is_budget_error(last_error):
                        next_budget = min(current_budget * 1.8, 3.0)
                        if next_budget > current_budget + 0.01:
                            console.print(
                                f"  [yellow][重试][/yellow] 规划预算不足，"
                                f"第 {attempt + 1}/{max_attempts} 次尝试预算提升到 ${next_budget:.2f}"
                            )
                            current_budget = next_budget
                            time.sleep(1.0)
                            continue
                        break

                    if _is_retryable_error(last_error):
                        backoff = min(3 * (2 ** (attempt - 1)), 20)
                        console.print(f"  [yellow][重试][/yellow] 临时错误，第 {attempt + 1}/{max_attempts} 次尝试，等待 {backoff}s")
                        time.sleep(backoff)
                        continue

                break

            except subprocess.TimeoutExpired:
                last_error = "调用超时 (180s)"
                if attempt < max_attempts:
                    backoff = min(3 * (2 ** (attempt - 1)), 20)
                    console.print(f"  [yellow][重试][/yellow] 调用超时，第 {attempt + 1}/{max_attempts} 次尝试，等待 {backoff}s")
                    time.sleep(backoff)
                    continue
                break
            except FileNotFoundError:
                console.print("[bold red]  claude CLI 未找到，请先安装: npm install -g @anthropic-ai/claude-code[/]")
                sys.exit(1)

        # 当前模型失败，尝试下一个模型
        if model_idx < len(model_chain) - 1:
            console.print("  [cyan][回退][/cyan] 当前模型失败，切换下一个模型继续尝试...")

    return (False, "", last_error)


# ── 辅助函数 ──────────────────────────────────────────────

def save_yaml(yaml_text: str) -> str:
    """保存 YAML 到临时文件。"""
    tmpfile = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False,
        dir=str(SCRIPT_DIR / "examples"),
        prefix="chat-",
    )
    tmpfile.write(yaml_text)
    tmpfile.close()
    return tmpfile.name


def cleanup_chat_yaml_files(keep: int = 30) -> int:
    """清理历史 chat-*.yaml，仅保留最近 keep 个。返回删除数量。"""
    examples_dir = SCRIPT_DIR / "examples"
    files = sorted(
        examples_dir.glob("chat-*.yaml"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if len(files) <= keep:
        return 0

    removed = 0
    for f in files[keep:]:
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def build_user_prompt(repo_path: str, requirement: str, max_workers: int, compact: bool = False) -> str:
    """构建任务拆分 prompt。compact=True 时使用短提示，降低 token 和预算压力。"""
    if compact:
        return f"""把下面需求拆成可并行执行的 YAML 任务配置，仅输出 YAML。

repo: {repo_path}
max_workers: {max_workers}

需求:
{requirement}

必须包含: project 和 tasks。"""

    return f"""请将以下项目需求拆分为多个可并行执行的任务:

项目路径: {repo_path}

需求:
{requirement}

请先分析项目现有结构 (如果存在)，然后输出任务 YAML。
max_workers 设为 {max_workers}。"""


# ── 主流程 ────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="claude-parallel 对话模式")
    parser.add_argument("--repo", "-r", help="项目仓库路径 (默认当前目录)")
    parser.add_argument("--message", "-m", default="",
                        help="直接提供自然语言需求（单行/短文本）")
    parser.add_argument("--message-file", default="",
                        help="从文件读取需求描述（适合长文本）")
    parser.add_argument("--auto", "-a", action="store_true", help="跳过确认，全自动执行")
    parser.add_argument("--interactive-step", "-i", action="store_true",
                        help="逐步引导问答式交互 (7 步向导)")
    parser.add_argument("--max-workers", type=int, default=3, help="最大并行数")
    parser.add_argument("--budget", type=float, default=5.0, help="总预算上限 $")
    parser.add_argument("--planner-budget", type=float, default=0.8,
                        help="任务拆分阶段预算上限 $ (默认 0.8)")
    parser.add_argument("--planner-retries", type=int, default=2,
                        help="任务拆分阶段自动重试次数 (默认 2)")
    parser.add_argument("--keep-chat-files", type=int, default=30,
                        help="最多保留多少份 chat-*.yaml 历史文件 (默认 30)")
    parser.add_argument("--planner-models", default="",
                        help="拆分阶段模型回退链，逗号分隔 (例: claude-sonnet-4-5,claude-3-5-haiku)")
    parser.add_argument("--no-merge", action="store_true", help="不自动合并")
    args = parser.parse_args()

    repo_path = str(Path(args.repo).expanduser().resolve()) if args.repo else os.getcwd()
    do_merge = not args.no_merge

    removed = cleanup_chat_yaml_files(max(1, args.keep_chat_files))
    model_chain = parse_model_chain(args.planner_models)

    # 获取用户需求
    requirement = ""
    max_workers = args.max_workers
    budget = args.budget

    if args.interactive_step and sys.stdin.isatty():
        wizard = interactive_step_wizard()
        if not wizard:
            return
        requirement = wizard["requirement"]
        wizard_repo = wizard.get("repo", "").strip()
        repo_path = str(Path(wizard_repo).expanduser().resolve()) if wizard_repo else os.getcwd()
        max_workers = wizard["max_workers"]
        budget = wizard["budget"]
        do_merge = wizard["want_merge"]
        args = argparse.Namespace(**{**vars(args), "auto": True})

    elif args.interactive_step and not sys.stdin.isatty():
        console.print("[yellow]  --interactive-step 需要交互式终端 (TTY)，已自动切换为普通模式[/]")
        console.print("[dim]  请通过管道或 -m 传入需求[/]\n")
        requirement = read_requirement_from_stdin()

    elif args.message_file:
        msg_file = Path(args.message_file).expanduser().resolve()
        if not msg_file.exists():
            console.print(f"[bold red]  message 文件不存在: {msg_file}[/]")
            return
        requirement = msg_file.read_text(encoding="utf-8").strip()
    elif args.message:
        requirement = args.message.strip()
    elif not sys.stdin.isatty():
        requirement = read_requirement_from_stdin()
    else:
        console.print("  请描述你的需求:")
        console.print("[dim]  例如:[/]")
        console.print("[dim]    - 给现有 Flask 项目增加 JWT 登录/注册接口，并补单元测试[/]")
        console.print("[dim]    - 为 React 项目增加用户管理页面，联调后端 API[/]")
        console.print("[dim]  " + "─" * 50 + "[/]")
        requirement = get_user_input("  > ")

    requirement = normalize_requirement(requirement)

    if not requirement:
        console.print("[yellow]  未输入需求，退出。[/]")
        console.print("  你可以这样调用:")
        console.print("[cyan]    cpar chat -m \"给项目增加登录功能和测试\"[/]")
        console.print("[cyan]    cpar chat -i               # 引导向导模式[/]")
        console.print("[cyan]    cat requirement.txt | cpar chat --repo ~/myproject[/]")
        return

    # 显示汇总（非向导模式）
    if not args.interactive_step:
        console.print()
        console.print(Panel(
            "[bold]Claude Parallel[/] — 对话模式",
            border_style="cyan",
            padding=(0, 2),
        ))
        console.print()

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="bold", width=12)
        table.add_column("Value")
        table.add_row("目标项目", repo_path)
        table.add_row("并行数", str(max_workers))
        table.add_row("预算上限", f"${budget}")
        if args.planner_models:
            table.add_row("拆分模型链", ", ".join(model_chain))
        if removed > 0:
            table.add_row("已清理", f"{removed} 份历史 YAML")
        console.print(table)

    console.print(f"  [green]✓[/green] 已接收需求: {len(requirement)} 字符")

    # 前置检查: 确保目标是 git 仓库
    repo_dir = Path(repo_path)
    if not (repo_dir / ".git").exists():
        console.print()
        console.print(f"  [yellow]⚠ {repo_path} 不是 Git 仓库[/]")
        console.print("  claude-parallel 依赖 git worktree 隔离任务。")
        answer = ask_yes_no("是否自动初始化 git 仓库?", default=True)
        if answer:
            import subprocess as _sp
            console.print(f"  正在初始化 {repo_path} ...")
            _sp.run(["git", "init"], cwd=repo_path, capture_output=True)
            real_files = [
                p for p in repo_dir.rglob("*")
                if p.is_file()
                and not p.relative_to(repo_dir).parts[0].startswith(".git")
            ]
            if not real_files:
                readme = repo_dir / "README.md"
                readme.write_text(f"# {repo_dir.name}\n")
                console.print("  [dim][自动] 目录为空，已创建 README.md[/]")
            _sp.run(["git", "add", "-A"], cwd=repo_path, capture_output=True)
            commit_result = _sp.run(
                ["git", "commit", "-m", "init (auto)"],
                cwd=repo_path, capture_output=True, text=True,
            )
            if commit_result.returncode == 0:
                console.print("  [green]✓ Git 仓库已初始化并完成首次提交。[/]")
            else:
                console.print(f"  [yellow]⚠ git commit 失败: {(commit_result.stderr or '').strip()[:200]}[/]")
                console.print(f"  请手动执行: cd {repo_path} && git add -A && git commit -m 'init'")
        else:
            console.print("  [yellow]已取消。[/]请手动执行:")
            console.print(f"    cd {repo_path} && git init && git add -A && git commit -m 'init'")
            return
    console.print()
    console.print(Rule("[bold][1/4] 分析需求，生成任务拆分方案[/]", style="blue"))

    # 构建 prompt
    user_prompt = build_user_prompt(
        repo_path=repo_path,
        requirement=requirement,
        max_workers=max_workers,
        compact=False,
    )

    ok_plan, yaml_text, plan_error = call_claude(
        user_prompt,
        SYSTEM_PROMPT,
        planner_budget=args.planner_budget,
        planner_retries=args.planner_retries,
        planner_models=model_chain,
    )

    # 兜底降级: 第一次失败后，自动缩短 prompt 再试一次
    if not ok_plan and not _is_quota_error(plan_error):
        console.print(f"  [yellow][降级][/yellow] 首次拆分失败: {plan_error[:200]}")
        console.print("  [yellow][降级][/yellow] 使用精简提示再试一次...")
        compact_prompt = build_user_prompt(
            repo_path=repo_path,
            requirement=requirement,
            max_workers=max_workers,
            compact=True,
        )
        ok_plan, yaml_text, plan_error = call_claude(
            compact_prompt,
            SYSTEM_PROMPT,
            planner_budget=max(0.4, args.planner_budget * 0.9),
            planner_retries=max(1, args.planner_retries),
            planner_models=model_chain,
        )

    if not ok_plan:
        console.print(Panel(
            f"[bold red]任务拆分失败[/]\n\n{plan_error}",
            border_style="red",
        ))
        console.print("  [bold]建议:[/]")
        if _is_quota_error(plan_error):
            console.print("[cyan]    1)[/] 当前账号额度不足，请稍后重试或切换可用模型/账号")
            console.print("[cyan]    2)[/] 如在 Team/订阅限额窗口，等待额度恢复")
        else:
            console.print("[cyan]    1)[/] 提高 --planner-budget (如 1.2)")
            console.print("[cyan]    2)[/] 增加 --planner-retries (如 3)")
            console.print("[cyan]    3)[/] 缩短需求描述，先拆第一阶段")
        return

    # 提取纯 YAML
    yaml_text = extract_yaml(yaml_text)

    # 替换 repo 占位符
    yaml_text = yaml_text.replace("__REPO__", repo_path)

    # 确保有 project 段
    if "project:" not in yaml_text:
        console.print("  [yellow][降级][/yellow] 输出缺少 project 段，尝试强约束重试...")
        hard_prompt = (
            "仅输出 YAML，必须包含 project: 和 tasks: 顶层字段。\n"
            f"repo: {repo_path}\nmax_workers: {max_workers}\n\n需求:\n{requirement}"
        )
        ok_plan2, yaml_text2, plan_error2 = call_claude(
            hard_prompt,
            SYSTEM_PROMPT,
            planner_budget=max(0.3, args.planner_budget * 0.8),
            planner_retries=1,
            planner_models=model_chain,
        )
        if ok_plan2 and "project:" in yaml_text2:
            yaml_text = yaml_text2
        else:
            console.print(Panel(
                "[bold red]生成的 YAML 格式不正确[/] (缺少 project 段)",
                border_style="red",
            ))
            preview = (yaml_text2 if ok_plan2 else plan_error2)[:500]
            console.print(f"\n  [dim]Claude 原始输出:\n  {preview}[/]")
            if "budget" in preview.lower() or "exceeded usd" in preview.lower():
                console.print("\n  [yellow]检测到预算不足，建议提高 --planner-budget (例如 1.2)[/]")
            return

    # YAML 预览 — Rich Syntax 高亮
    console.print()
    console.print(Rule("[bold][2/4] 生成的任务方案[/]", style="blue"))
    syntax = Syntax(yaml_text, "yaml", theme="monokai", line_numbers=True)
    console.print(Panel(syntax, border_style="green", padding=(0, 1)))

    # 保存 YAML
    yaml_file = save_yaml(yaml_text)

    # 校验
    console.print()
    console.print(Rule("[bold][3/4] 校验配置[/]", style="blue"))
    validator = TaskValidator(yaml_file)
    ok = validator.validate()
    validator.print_report()

    if not ok:
        console.print()
        console.print(Panel(
            f"[yellow]配置有误[/]\n\nYAML 已保存到: {yaml_file}\n你可以手动修改后用 cpar run 执行",
            border_style="yellow",
        ))
        return

    # 确认执行
    if not args.auto:
        console.print()
        answer = ask_choice(
            "是否执行?",
            [
                ("y", "是，立即执行"),
                ("e", "编辑 YAML 后手动执行"),
                ("n", "取消"),
            ],
            default="y",
        )

        if answer == "e":
            editor = os.environ.get("EDITOR", "vim")
            subprocess.run([editor, yaml_file])
            console.print(f"\n  编辑完成，YAML: {yaml_file}")
            console.print("  用以下命令执行:")
            merge_flag = "--merge" if do_merge else ""
            console.print(f"[cyan]    cpar run {yaml_file} {merge_flag}[/]")
            return
        elif answer in ("n", "no"):
            console.print(f"\n  [yellow]已取消。[/]YAML 已保存: {yaml_file}")
            console.print(f"  你可以稍后执行: [cyan]cpar run {yaml_file}[/]")
            return

    # 执行
    console.print()
    console.print(Rule("[bold][4/4] 开始执行[/]", style="blue"))
    console.print()

    run_cmd = [
        sys.executable, str(SCRIPT_DIR / "run.py"),
        "run", yaml_file,
        "--total-budget", str(budget),
    ]
    if do_merge:
        run_cmd.append("--merge")

    os.execvp(sys.executable, run_cmd)


if __name__ == "__main__":
    main()
