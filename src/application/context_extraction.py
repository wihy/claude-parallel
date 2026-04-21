"""
智能上下文提取器 — 从 Claude 输出中提取接口/API 签名，支持多语言。

支持:
- Python (def, class, async def, @app.route 等)
- JavaScript/TypeScript (function, const, export, class, arrow functions)
- Go (func, type, interface)
- Rust (fn, impl, trait, struct, pub)
- Java/C# (public class, public void, interface)

提取策略:
1. 从 Markdown 代码块中提取
2. 正则匹配语言特定的接口声明
3. 如果以上都不适用，截取输出摘要
"""

import re
from pathlib import Path
from typing import Optional, List


# ── 语言特定模式 ──

LANG_PATTERNS = {
    "python": [
        # 函数/类定义
        r'(?:^|\n)((?:async\s+)?def\s+\w+[^:]*:)',
        r'(?:^|\n)(class\s+\w+[^:]*:)',
        # 装饰器 + 函数
        r'(?:^|\n)@\w+(?:\.[\w.]+)*\s*\n((?:async\s+)?def\s+\w+[^:]*:)',
        # FastAPI/Flask
        r'(?:^|\n)@(?:app|router)\.\w+\([^)]*\)\s*\n((?:async\s+)?def\s+\w+[^:]*:)',
    ],
    "javascript": [
        r'(?:^|\n)(export\s+(?:default\s+)?(?:async\s+)?function\s+\w+[^{]*)',
        r'(?:^|\n)((?:export\s+)?const\s+\w+\s*=\s*(?:async\s+)?\([^)]*\)\s*=>)',
        r'(?:^|\n)((?:export\s+)?class\s+\w+[^{]*)',
        r'(?:^|\n)((?:export\s+)?interface\s+\w+[^{]*)',
        r'(?:^|\n)((?:export\s+)?type\s+\w+\s*=)',
        r'(?:^|\n)((?:export\s+)?enum\s+\w+[^{]*)',
    ],
    "typescript": [
        # 复用 JavaScript 模式
    ],
    "go": [
        r'(?:^|\n)(func\s+(?:\([^)]*\)\s*)?\w+\s*\([^)]*\)[^{]*)',
        r'(?:^|\n)(type\s+\w+\s+(?:struct|interface))',
        r'(?:^|\n)(var\s+\w+\s+)',
    ],
    "rust": [
        r'(?:^|\n)((?:pub\s+)?fn\s+\w+[^{]*)',
        r'(?:^|\n)((?:pub\s+)?struct\s+\w+[^{]*)',
        r'(?:^|\n)((?:pub\s+)?enum\s+\w+[^{]*)',
        r'(?:^|\n)((?:pub\s+)?trait\s+\w+[^{]*)',
        r'(?:^|\n)(impl[^{]*)',
    ],
    "java": [
        r'(?:^|\n)((?:public|private|protected)\s+(?:static\s+)?(?:class|interface|enum)\s+\w+[^{]*)',
        r'(?:^|\n)((?:public|private|protected)\s+(?:static\s+)?(?:\w+(?:<[^>]+>)?)\s+\w+\s*\([^)]*\))',
    ],
}

# TypeScript 复用 JavaScript 模式
LANG_PATTERNS["typescript"] = LANG_PATTERNS["javascript"]


def detect_language(text: str) -> str:
    """
    从代码块或文件路径中检测语言。
    优先检查 markdown 代码块标注，然后回退到内容特征。
    """
    # 检查代码块语言标注
    lang = re.search(r'```(\w+)\n', text)
    if lang:
        label = lang.group(1).lower()
        mapping = {
            "py": "python", "python": "python",
            "js": "javascript", "javascript": "javascript",
            "ts": "typescript", "typescript": "typescript",
            "tsx": "typescript", "jsx": "javascript",
            "go": "go", "golang": "go",
            "rust": "rust", "rs": "rust",
            "java": "java",
        }
        return mapping.get(label, label)

    # 内容特征检测
    if re.search(r'\b(def |class |import |\bfrom\s+\w+\s+import\b)', text):
        return "python"
    if re.search(r'\b(function |const |let |var |export |import .* from)', text):
        return "javascript"
    if re.search(r'\bfunc\s+\w+|package\s+main', text):
        return "go"
    if re.search(r'\bfn\s+\w+|impl\s+\w+|pub\s+fn', text):
        return "rust"

    return "unknown"


def extract_signatures(text: str, max_length: int = 6000) -> str:
    """
    从文本中提取接口/API 签名。

    策略优先级:
    1. 代码块提取 (```...```)
    2. 语言特定的签名匹配
    3. 尾部截断摘要
    """
    if not text:
        return ""

    # 策略1: 提取代码块
    code_blocks = re.findall(r'```(?:\w+)?\n(.*?)```', text, re.DOTALL)
    if code_blocks:
        # 优先选择包含定义的代码块
        api_blocks = []
        other_blocks = []
        for block in code_blocks:
            lang = detect_language(block)
            patterns = LANG_PATTERNS.get(lang, [])
            has_sig = any(re.search(p, block) for p in patterns)
            if has_sig:
                api_blocks.append(block)
            else:
                other_blocks.append(block)

        chosen = api_blocks if api_blocks else other_blocks[:3]
        context = "\n\n".join(f"```\n{b}\n```" for b in chosen[:6])
        if len(context) > max_length:
            context = context[:max_length] + "\n... (截断)"
        return context

    # 策略2: 语言特定签名
    lang = detect_language(text)
    patterns = LANG_PATTERNS.get(lang, [])
    if patterns:
        signatures = []
        for pattern in patterns:
            matches = re.findall(pattern, text)
            signatures.extend(matches)
        if signatures:
            # 去重
            seen = set()
            unique = []
            for s in signatures:
                s = s.strip()
                if s not in seen:
                    seen.add(s)
                    unique.append(s)
            result = "接口/类型定义:\n" + "\n".join(unique[:25])
            if len(result) > max_length:
                result = result[:max_length] + "\n... (截断)"
            return result

    # 策略3: 如果文本中有 "签名"、"接口"、"API" 等关键词，截取周围内容
    keyword_patterns = [
        r'(?:(?:接口|签名|定义|函数列表|API\s*endpoints?)[：:]\s*\n?)(.*?)(?:\n\n|\Z)',
    ]
    for kp in keyword_patterns:
        match = re.search(kp, text, re.DOTALL | re.IGNORECASE)
        if match and match.group(1).strip():
            excerpt = match.group(1).strip()[:max_length]
            return excerpt

    # 最终回退: 截取尾部
    return text[-min(3000, max_length):]


def extract_context_for_downstream(
    output: str,
    task_id: str,
    files: Optional[List[str]] = None,
) -> str:
    """
    高级上下文提取: 综合分析 Claude 输出，生成给下游任务的上下文。

    步骤:
    1. 提取接口签名
    2. 提取文件清单 (Claude 通常会列出创建/修改的文件)
    3. 组合为结构化上下文
    """
    if not output:
        return ""

    parts = []

    # 提取文件清单
    file_section = _extract_file_list(output)
    if file_section:
        parts.append(f"创建/修改的文件:\n{file_section}")

    # 提取签名
    signatures = extract_signatures(output, max_length=4000)
    if signatures and signatures != output[-3000:]:
        parts.append(f"接口/代码:\n{signatures}")

    if not parts:
        # 回退到截断
        parts.append(f"产出摘要:\n{output[:2000]}")

    return "\n\n".join(parts)


def _extract_file_list(text: str) -> str:
    """从 Claude 输出中提取文件清单"""
    # 常见模式:
    # - "创建了文件: xxx, yyy"
    # - "修改了: xxx"
    # - "- src/foo.py"
    # - "Created: src/foo.py"
    patterns = [
        r'(?:创建|修改|Created|Modified|Changed|Files?)[：:]\s*\n((?:[-*]\s+.*\n?)+)',
        r'((?:[-*]\s+`?[\w./-]+\.\w+`?\n?){1,20})',
    ]
    for p in patterns:
        match = re.search(p, text)
        if match:
            files = match.group(1).strip()
            if len(files) > 10:
                return files[:1000]
    return ""
