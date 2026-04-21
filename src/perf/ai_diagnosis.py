"""
ai_diagnosis — AI 辅助性能诊断模块。

通过 LLM 分析性能采集数据，生成:
- 问题诊断报告（调用栈热点、syslog 告警、功耗趋势等）
- WebKit 功耗专项优化建议
- 前后 session 回归分析

依赖:
- urllib.request (不依赖 requests)
- 环境变量: OPENAI_API_KEY, OPENAI_BASE_URL (可选), OPENAI_MODEL (可选)
- 无 API key 时自动降级为 offline 模式（只收集上下文 + 生成 prompt）
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# ── 常量 ──

# 1 token ≈ 1.5 中文字符 或 0.75 英文单词
MAX_CONTEXT_TOKENS = 8000
CHARS_PER_TOKEN_ZH = 1.5
CHARS_PER_TOKEN_EN = 4  # 平均英文单词长度约 4-5 chars ≈ 0.75 word ≈ 1 token

# 焦点区域配置
FOCUS_AREAS = ("general", "webkit", "power", "memory", "gpu")


# ── 数据结构 ──

@dataclass
class DiagnosisContext:
    """从 session 目录收集的所有诊断上下文。"""
    session_dir: str
    hotspots: str = ""           # 调用栈热点文本
    syslog_alerts: str = ""      # syslog 告警文本
    power_data: str = ""         # 功耗/电池趋势文本
    process_data: str = ""       # 进程 CPU/内存文本
    deep_schemas: str = ""       # 深度 schema 数据文本
    webcontent_data: str = ""    # WebContent 热点文本
    meta_summary: str = ""       # session meta 摘要
    timeline_summary: str = ""   # timeline 事件摘要


@dataclass
class DiagnosisResult:
    """LLM 诊断结果。"""
    problems: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    priority: List[Dict[str, Any]] = field(default_factory=list)
    raw_response: str = ""
    offline: bool = False
    prompt: str = ""


# ── 上下文收集 ──

def collect_diagnosis_context(session_dir: str) -> DiagnosisContext:
    """
    从 session 目录收集所有诊断数据，拼接为文本上下文。

    Args:
        session_dir: perf session 根目录路径

    Returns:
        DiagnosisContext 实例，各字段为格式化后的文本
    """
    root = Path(session_dir).expanduser().resolve()
    ctx = DiagnosisContext(session_dir=str(root))

    if not root.exists():
        logger.warning("Session 目录不存在: %s", root)
        return ctx

    logs_dir = root / "logs"
    exports_dir = root / "exports"
    meta_file = root / "meta.json"
    timeline_file = root / "timeline.json"

    # ── meta 摘要 ──
    ctx.meta_summary = _read_meta_summary(meta_file)

    # ── 调用栈热点 ──
    hotspots_file = logs_dir / "hotspots.jsonl"
    if hotspots_file.exists():
        try:
            from .sampling import read_hotspots_jsonl, format_hotspots_text
            snapshots = read_hotspots_jsonl(hotspots_file, aggregate=True)
            ctx.hotspots = format_hotspots_text(snapshots, top_n=15)
        except Exception as e:
            logger.debug("读取 hotspots 失败: %s", e)
            ctx.hotspots = _fallback_read_jsonl_text(hotspots_file)

    # ── WebContent 热点 ──
    wc_hotspots = logs_dir / "webcontent_hotspots.jsonl"
    if wc_hotspots.exists():
        try:
            from .webcontent import read_webcontent_hotspots, format_webcontent_hotspots
            snaps = read_webcontent_hotspots(wc_hotspots, last_n=5)
            ctx.webcontent_data = format_webcontent_hotspots(snaps, top_n=10)
        except Exception as e:
            logger.debug("读取 webcontent hotspots 失败: %s", e)

    # ── syslog 告警 ──
    alert_log = logs_dir / "alert_log.jsonl"
    syslog_full = logs_dir / "syslog_full.log"
    if alert_log.exists():
        ctx.syslog_alerts = _read_alert_log(alert_log)
    elif syslog_full.exists():
        ctx.syslog_alerts = _tail_file(syslog_full, 100)

    # ── 功耗/电池趋势 ──
    battery_jsonl = logs_dir / "battery.jsonl"
    if battery_jsonl.exists():
        try:
            from .device_metrics import read_battery_jsonl, format_battery_text
            records = read_battery_jsonl(battery_jsonl, last_n=50)
            ctx.power_data = format_battery_text(records)
        except Exception as e:
            logger.debug("读取 battery 失败: %s", e)
            ctx.power_data = _fallback_read_jsonl_text(battery_jsonl, max_lines=30)

    # ── 进程 CPU/内存 ──
    proc_jsonl = logs_dir / "process_metrics.jsonl"
    if proc_jsonl.exists():
        try:
            from .device_metrics import read_process_metrics_jsonl, format_process_metrics_text
            records = read_process_metrics_jsonl(proc_jsonl, last_n=30)
            ctx.process_data = format_process_metrics_text(records)
        except Exception as e:
            logger.debug("读取 process_metrics 失败: %s", e)
            ctx.process_data = _fallback_read_jsonl_text(proc_jsonl, max_lines=20)

    # ── 深度 schema 数据 ──
    ctx.deep_schemas = _collect_deep_schemas(exports_dir, root, meta_file)

    # ── timeline 摘要 ──
    if timeline_file.exists():
        ctx.timeline_summary = _read_timeline_summary(timeline_file)

    return ctx


# ── Prompt 构建 ──

def build_diagnosis_prompt(
    context: DiagnosisContext,
    focus_area: str = "general",
) -> str:
    """
    构建 LLM 诊断 prompt。

    Args:
        context:      DiagnosisContext 实例
        focus_area:   焦点区域: 'general'|'webkit'|'power'|'memory'|'gpu'

    Returns:
        完整 prompt 字符串
    """
    if focus_area not in FOCUS_AREAS:
        focus_area = "general"

    # ── 角色设定 ──
    role = _ROLE_PROMPTS.get(focus_area, _ROLE_PROMPTS["general"])

    # ── 拼接数据概要 ──
    data_sections = []

    if context.meta_summary:
        data_sections.append(f"## Session 元信息\n{context.meta_summary}")

    if context.hotspots:
        data_sections.append(f"## 调用栈热点 (Time Profiler)\n{context.hotspots}")

    if context.webcontent_data:
        data_sections.append(f"## WebContent 进程热点\n{context.webcontent_data}")

    if context.syslog_alerts:
        data_sections.append(f"## Syslog 告警\n{context.syslog_alerts}")

    if context.power_data:
        data_sections.append(f"## 功耗/电池趋势\n{context.power_data}")

    if context.process_data:
        data_sections.append(f"## 进程 CPU/内存指标\n{context.process_data}")

    if context.deep_schemas:
        data_sections.append(f"## 深度 Schema 数据\n{context.deep_schemas}")

    if context.timeline_summary:
        data_sections.append(f"## Timeline 事件\n{context.timeline_summary}")

    data_block = "\n\n".join(data_sections)

    # ── 截断到 token 上限 ──
    data_block = _truncate_to_tokens(data_block, MAX_CONTEXT_TOKENS - 800)

    # ── 分析要求 ──
    analysis_req = _ANALYSIS_REQUIREMENTS.get(focus_area, _ANALYSIS_REQUIREMENTS["general"])

    prompt = f"""{role}

# 性能诊断数据

{data_block}

# 分析要求

{analysis_req}

请按以下格式输出:

## 发现的问题
(列出每个问题，编号，包含: 现象、根因分析、影响程度)

## 优化建议
(列出每条建议，编号，包含: 具体措施、预期收益、实施难度)

## 优先级排序
(按 P0/P1/P2 排列所有问题和建议)

## 总结
(一段话总结整体性能状况和最关键的优化方向)
"""
    return prompt


# ── LLM 调用 ──

def call_llm(
    prompt: str,
    model: Optional[str] = None,
) -> str:
    """
    调用 LLM API 生成诊断报告。

    使用 OpenAI 兼容 API (POST /v1/chat/completions)。
    环境变量: OPENAI_API_KEY, OPENAI_BASE_URL (可选), OPENAI_MODEL (可选)

    Args:
        prompt: 完整 prompt
        model:  模型名称 (覆盖 OPENAI_MODEL)

    Returns:
        LLM 返回文本

    Raises:
        RuntimeError: API key 未配置
        URLError:     网络错误
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY 未设置。请设置环境变量后重试，"
            "或使用 offline 模式 (只收集上下文 + 生成 prompt)。"
        )

    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com").strip().rstrip("/")
    model_name = model or os.environ.get("OPENAI_MODEL", "gpt-4o").strip()

    url = f"{base_url}/v1/chat/completions"

    payload = json.dumps({
        "model": model_name,
        "messages": [
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    req = Request(url, data=payload, headers=headers, method="POST")

    timeout_sec = int(os.environ.get("OPENAI_TIMEOUT", "120"))
    with urlopen(req, timeout=timeout_sec) as resp:
        body = resp.read().decode("utf-8")

    result = json.loads(body)

    # 提取 content
    choices = result.get("choices", [])
    if not choices:
        raise RuntimeError(f"LLM 返回无 choices: {body[:500]}")

    content = choices[0].get("message", {}).get("content", "")
    if not content:
        raise RuntimeError(f"LLM 返回空 content: {body[:500]}")

    return content


# ── 响应解析 ──

def parse_diagnosis_response(response_text: str) -> DiagnosisResult:
    """
    解析 LLM 返回文本，提取问题列表、优化建议、优先级排序。

    Args:
        response_text: LLM 返回的原始文本

    Returns:
        DiagnosisResult 实例
    """
    result = DiagnosisResult(raw_response=response_text)

    # ── 提取问题列表 ──
    problems_section = _extract_section(response_text, "发现的问题")
    result.problems = _extract_numbered_items(problems_section)

    # ── 提取优化建议 ──
    recs_section = _extract_section(response_text, "优化建议")
    result.recommendations = _extract_numbered_items(recs_section)

    # ── 提取优先级排序 ──
    priority_section = _extract_section(response_text, "优先级排序")
    result.priority = _extract_priority_items(priority_section)

    # 如果正则提取失败，回退到按段落切分
    if not result.problems and not result.recommendations:
        result.problems = _fallback_extract_paragraphs(response_text, "问题")
        result.recommendations = _fallback_extract_paragraphs(response_text, "建议")

    return result


# ── 回归分析 ──

def generate_regression_analysis(
    before_session: str,
    after_session: str,
) -> str:
    """
    对比前后两次 session 数据，生成回归分析摘要。

    Args:
        before_session: 基线 session 目录
        after_session:  新 session 目录

    Returns:
        回归分析摘要文本
    """
    before_ctx = collect_diagnosis_context(before_session)
    after_ctx = collect_diagnosis_context(after_session)

    before_meta = _load_meta_json(before_session)
    after_meta = _load_meta_json(after_session)

    lines: List[str] = []
    lines.append("=" * 60)
    lines.append("  回归分析报告")
    lines.append("=" * 60)
    lines.append("")

    # ── 基本对比 ──
    b_tag = before_meta.get("tag", "baseline")
    a_tag = after_meta.get("tag", "current")
    lines.append(f"  基线: {b_tag}")
    lines.append(f"  当前: {a_tag}")
    lines.append("")

    # ── 功耗对比 ──
    b_power = _extract_power_summary(before_meta, before_ctx)
    a_power = _extract_power_summary(after_meta, after_ctx)
    if b_power or a_power:
        lines.append("  ── 功耗对比 ──")
        for key in set(list(b_power.keys()) + list(a_power.keys())):
            bv = b_power.get(key, "N/A")
            av = a_power.get(key, "N/A")
            delta = ""
            if isinstance(bv, (int, float)) and isinstance(av, (int, float)):
                d = av - bv
                pct = (d / bv * 100) if bv else 0
                arrow = "↑" if d > 0 else ("↓" if d < 0 else "→")
                delta = f"  {arrow} {d:+.2f} ({pct:+.1f}%)"
            lines.append(f"    {key}: {bv} → {av}{delta}")
        lines.append("")

    # ── 热点对比 ──
    b_hot = _extract_top_symbols(before_ctx.hotspots)
    a_hot = _extract_top_symbols(after_ctx.hotspots)
    if b_hot or a_hot:
        lines.append("  ── 热点函数对比 (Top 5) ──")
        b_sym_set = set(b_hot.keys())
        a_sym_set = set(a_hot.keys())

        # 新增热点
        new_syms = a_sym_set - b_sym_set
        if new_syms:
            lines.append("    [新增热点]")
            for s in sorted(new_syms, key=lambda x: a_hot.get(x, 0), reverse=True)[:5]:
                lines.append(f"      + {s}  ({a_hot[s]:.1f}%)")
            lines.append("")

        # 消失热点
        gone_syms = b_sym_set - a_sym_set
        if gone_syms:
            lines.append("    [消失热点]")
            for s in sorted(gone_syms, key=lambda x: b_hot.get(x, 0), reverse=True)[:5]:
                lines.append(f"      - {s}  (was {b_hot[s]:.1f}%)")
            lines.append("")

        # 共有热点变化
        common = b_sym_set & a_sym_set
        if common:
            lines.append("    [权重变化]")
            for s in sorted(common, key=lambda x: abs(a_hot[x] - b_hot[x]), reverse=True)[:8]:
                delta = a_hot[s] - b_hot[s]
                arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
                lines.append(f"      {arrow} {s}: {b_hot[s]:.1f}% → {a_hot[s]:.1f}%")
            lines.append("")

    # ── 告警对比 ──
    b_alert_count = before_ctx.syslog_alerts.count("\n")
    a_alert_count = after_ctx.syslog_alerts.count("\n")
    lines.append("  ── Syslog 告警对比 ──")
    lines.append(f"    基线告警行数: {b_alert_count}")
    lines.append(f"    当前告警行数: {a_alert_count}")
    if a_alert_count > b_alert_count:
        lines.append(f"    ⚠ 告警增加了 {a_alert_count - b_alert_count} 行")
    elif a_alert_count < b_alert_count:
        lines.append(f"    ✓ 告警减少了 {b_alert_count - a_alert_count} 行")
    lines.append("")

    # ── 回归判定 ──
    lines.append("  ── 回归判定 ──")
    regressions = []
    # 检查功耗回归
    for key in ("display_avg", "cpu_avg", "networking_avg"):
        bv = b_power.get(key)
        av = a_power.get(key)
        if isinstance(bv, (int, float)) and isinstance(av, (int, float)):
            if bv > 0:
                pct_change = (av - bv) / bv * 100
                if pct_change > 10:
                    regressions.append(
                        f"    ⚠ {key} 回归: {bv:.2f} → {av:.2f} (+{pct_change:.1f}%)"
                    )

    # 检查新增热点
    if new_syms:
        regressions.append(f"    ⚠ 新增 {len(new_syms)} 个热点函数")

    # 检查告警增加
    if a_alert_count > b_alert_count * 1.5 and a_alert_count > 5:
        regressions.append(f"    ⚠ 告警数显著增加: {b_alert_count} → {a_alert_count}")

    if regressions:
        lines.append("  存在性能回归:")
        for r in regressions:
            lines.append(r)
    else:
        lines.append("  ✓ 未检测到显著性能回归")
    lines.append("")

    return "\n".join(lines)


# ── WebKit 专项报告 ──

def generate_webkit_report(session_dir: str) -> str:
    """
    WebKit 功耗专项报告，结合 WebKit 架构知识生成优化建议。

    Args:
        session_dir: session 根目录

    Returns:
        WebKit 专项报告文本
    """
    ctx = collect_diagnosis_context(session_dir)

    lines: List[str] = []
    lines.append("=" * 60)
    lines.append("  WebKit 功耗专项诊断报告")
    lines.append("=" * 60)
    lines.append("")

    # ── 数据概览 ──
    lines.append("  ── 数据概览 ──")
    if ctx.webcontent_data:
        lines.append("  WebContent 进程热点:")
        lines.append(ctx.webcontent_data)
    else:
        lines.append("  (无 WebContent 热点数据)")
    lines.append("")

    if ctx.hotspots:
        lines.append("  App 主进程热点 (可能含 WebKit 调用):")
        lines.append(ctx.hotspots)
    lines.append("")

    if ctx.power_data:
        lines.append("  功耗趋势:")
        lines.append(ctx.power_data)
    lines.append("")

    if ctx.process_data:
        lines.append("  进程资源指标:")
        lines.append(ctx.process_data)
    lines.append("")

    if ctx.syslog_alerts:
        lines.append("  相关 syslog 告警:")
        lines.append(ctx.syslog_alerts)
    lines.append("")

    # ── WebKit 架构知识 + 常见问题 ──
    lines.append("  ── WebKit 架构知识与常见优化点 ──")
    lines.append("")

    webkit_patterns = _detect_webkit_patterns(ctx)

    if webkit_patterns:
        lines.append("  检测到的 WebKit 相关模式:")
        for pattern in webkit_patterns:
            lines.append(f"    • {pattern}")
        lines.append("")

    lines.append("  常见 WebKit 功耗问题及优化建议:")
    lines.append("")

    suggestions = _get_webkit_suggestions(ctx)
    for i, (title, desc) in enumerate(suggestions, 1):
        lines.append(f"  {i}. {title}")
        lines.append(f"     {desc}")
        lines.append("")

    return "\n".join(lines)


# ── 格式化输出 ──

def format_diagnosis_report(diagnosis: DiagnosisResult) -> str:
    """
    格式化输出诊断报告。

    Args:
        diagnosis: DiagnosisResult 实例

    Returns:
        格式化的报告文本
    """
    lines: List[str] = []
    lines.append("=" * 60)
    lines.append("  AI 性能诊断报告")
    lines.append("=" * 60)
    lines.append("")

    if diagnosis.offline:
        lines.append("  [OFFLINE 模式 — 未调用 LLM，以下为本地分析结果]")
        lines.append("")

    if diagnosis.problems:
        lines.append("  ── 发现的问题 ──")
        lines.append("")
        for i, p in enumerate(diagnosis.problems, 1):
            lines.append(f"  {i}. {p}")
        lines.append("")

    if diagnosis.recommendations:
        lines.append("  ── 优化建议 ──")
        lines.append("")
        for i, r in enumerate(diagnosis.recommendations, 1):
            lines.append(f"  {i}. {r}")
        lines.append("")

    if diagnosis.priority:
        lines.append("  ── 优先级排序 ──")
        lines.append("")
        for item in diagnosis.priority:
            level = item.get("level", "?")
            text = item.get("text", "")
            lines.append(f"  [{level}] {text}")
        lines.append("")

    if not diagnosis.problems and not diagnosis.recommendations:
        lines.append("  (未提取到结构化诊断结果，请查看原始响应)")
        lines.append("")

    if diagnosis.raw_response:
        lines.append("  ── LLM 原始响应 ──")
        lines.append("")
        # 截断显示
        raw = diagnosis.raw_response
        if len(raw) > 3000:
            raw = raw[:3000] + "\n... (截断)"
        lines.append(raw)
        lines.append("")

    return "\n".join(lines)


# ── 一键诊断 ──

def run_diagnosis(
    session_dir: str,
    focus_area: str = "general",
    model: Optional[str] = None,
    offline: bool = False,
) -> DiagnosisResult:
    """
    一键完成: 收集上下文 → 构建 prompt → 调用 LLM → 解析结果。

    Args:
        session_dir:  session 根目录
        focus_area:   焦点区域
        model:        LLM 模型名称
        offline:      True 只收集上下文不调用 LLM

    Returns:
        DiagnosisResult 实例
    """
    # 1. 收集上下文
    ctx = collect_diagnosis_context(session_dir)

    # 2. 构建 prompt
    prompt = build_diagnosis_prompt(ctx, focus_area=focus_area)

    if offline:
        return DiagnosisResult(
            problems=[],
            recommendations=[],
            priority=[],
            raw_response="(offline 模式 — 未调用 LLM)",
            offline=True,
            prompt=prompt,
        )

    # 3. 调用 LLM
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return DiagnosisResult(
            problems=[],
            recommendations=[],
            priority=[],
            raw_response="OPENAI_API_KEY 未设置，无法调用 LLM。",
            offline=True,
            prompt=prompt,
        )

    try:
        response_text = call_llm(prompt, model=model)
    except (RuntimeError, URLError, OSError) as e:
        logger.error("LLM 调用失败: %s", e)
        return DiagnosisResult(
            problems=[],
            recommendations=[],
            priority=[],
            raw_response=f"LLM 调用失败: {e}",
            offline=True,
            prompt=prompt,
        )

    # 4. 解析结果
    result = parse_diagnosis_response(response_text)
    result.prompt = prompt
    return result


# ══════════════════════════════════════════════════════════════
# 内部辅助函数
# ══════════════════════════════════════════════════════════════

# ── 角色设定模板 ──

_ROLE_PROMPTS: Dict[str, str] = {
    "general": (
        "你是一位资深的 iOS 性能优化专家，擅长分析 Instruments trace 数据、"
        "syslog 告警、功耗趋势和调用栈热点。请根据以下数据，给出专业的性能诊断。"
    ),
    "webkit": (
        "你是一位专注于 WebKit/iOS WebView 性能优化的专家。你深入了解 WKWebView "
        "架构、WebContent 进程模型、JavaScriptCore 引擎、网络缓存策略、以及 "
        "WebKit 渲染管线。请根据以下数据，给出 WebKit 功耗和性能专项诊断。"
    ),
    "power": (
        "你是一位 iOS 功耗优化专家，精通 Instruments Power Profiler、"
        "电池 drain 分析、thermal management、CPU/GPU/Display 功耗归因。"
        "请根据以下数据，给出功耗专项诊断。"
    ),
    "memory": (
        "你是一位 iOS 内存优化专家，精通 Jetsam 机制、vm_compressor、"
        "内存压力监控、AutoreleasePool 分析、循环引用检测。"
        "请根据以下数据，给出内存专项诊断。"
    ),
    "gpu": (
        "你是一位 iOS GPU/Metal 性能专家，精通 Metal shader 优化、"
        "帧耗时分析、CA::Render 管线、CoreAnimation commit 分析。"
        "请根据以下数据，给出 GPU 渲染专项诊断。"
    ),
}

# ── 分析要求模板 ──

_ANALYSIS_REQUIREMENTS: Dict[str, str] = {
    "general": (
        "1. 从调用栈热点中识别 CPU 密集型函数\n"
        "2. 从 syslog 告警中识别关键异常（内存压力、热管理、崩溃等）\n"
        "3. 从功耗数据中分析能耗趋势和异常\n"
        "4. 从进程指标中识别 CPU/内存异常\n"
        "5. 给出整体性能评估和优化优先级"
    ),
    "webkit": (
        "1. 分析 WebContent 进程的 CPU 热点（JavaScript 执行、布局、渲染）\n"
        "2. 检查 WebKit 相关的 syslog 告警（崩溃、OOM、网络错误）\n"
        "3. 分析 WebView 加载和渲染的功耗消耗\n"
        "4. 评估 JavaScript 调用频率和执行耗时\n"
        "5. 检查网络请求模式（大量小请求 vs 少量大请求）\n"
        "6. 给出 WebKit 架构层面的优化建议"
    ),
    "power": (
        "1. 分析 Display/CPU/Networking 各子系统的功耗分布\n"
        "2. 检查 thermal 降频事件和功耗突变点\n"
        "3. 识别后台高功耗时段\n"
        "4. 分析电池 drain 速率和趋势\n"
        "5. 给出功耗优化建议和预期收益"
    ),
    "memory": (
        "1. 分析进程内存使用趋势（RSS、虚拟内存）\n"
        "2. 检查 Jetsam 和内存压力告警\n"
        "3. 识别可能的内存泄漏模式\n"
        "4. 分析 vm_compressor 活动\n"
        "5. 给出内存优化建议"
    ),
    "gpu": (
        "1. 分析 GPU 帧耗时分布（平均、P95、P99）\n"
        "2. 检查掉帧和渲染超时\n"
        "3. 分析 Metal shader 执行耗时\n"
        "4. 检查 CoreAnimation commit 耗时\n"
        "5. 给出 GPU 渲染优化建议"
    ),
}


# ── 文件读取辅助 ──

def _read_meta_summary(meta_file: Path) -> str:
    """读取 meta.json 并生成摘要文本。"""
    if not meta_file.exists():
        return "(无 meta.json)"
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("meta.json 解析失败: %s", e)
        return "(meta.json 解析失败)"

    lines = []
    lines.append(f"  Tag: {meta.get('tag', '?')}")
    lines.append(f"  Status: {meta.get('status', '?')}")
    lines.append(f"  Device: {meta.get('device', '?')}")
    lines.append(f"  Attach: {meta.get('attach', '?')}")

    started = meta.get("started_at", 0)
    ended = meta.get("ended_at", 0)
    if started:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))
        lines.append(f"  Started: {ts}")
    if ended and started:
        dur = round(ended - started, 1)
        lines.append(f"  Duration: {dur}s")

    templates = meta.get("templates", [])
    if templates:
        lines.append(f"  Templates: {', '.join(str(t) for t in templates)}")

    errors = meta.get("errors", [])
    if errors:
        lines.append(f"  Errors: {'; '.join(errors[:5])}")

    sampling = meta.get("sampling", {})
    if sampling.get("enabled"):
        lines.append(
            f"  Sampling: interval={sampling.get('interval_sec', '?')}s, "
            f"top_n={sampling.get('top_n', '?')}"
        )

    return "\n".join(lines)


def _read_timeline_summary(timeline_file: Path) -> str:
    """读取 timeline 事件摘要。"""
    try:
        data = json.loads(timeline_file.read_text(encoding="utf-8"))
        events = data.get("events", [])
    except Exception as e:
        logger.debug("timeline 解析失败: %s", e)
        return "(timeline 解析失败)"

    if not events:
        return "(无 timeline 事件)"

    lines = [f"  共 {len(events)} 个事件:"]
    for evt in events[-20:]:
        ts = evt.get("ts", 0)
        ts_str = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "?"
        name = evt.get("event", "?")
        detail = evt.get("detail", "")
        line = f"    [{ts_str}] {name}"
        if detail:
            line += f" — {detail[:60]}"
        lines.append(line)

    return "\n".join(lines)


def _read_alert_log(alert_log: Path, max_items: int = 30) -> str:
    """读取 alert_log.jsonl 告警。"""
    try:
        lines = alert_log.read_text(encoding="utf-8").strip().splitlines()
    except Exception as e:
        logger.debug("告警日志读取失败: %s", e)
        return "(告警日志读取失败)"

    alerts = []
    for line in lines:
        try:
            alerts.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not alerts:
        return "(无告警)"

    # 按级别分组统计
    level_counts: Dict[str, int] = {}
    for a in alerts:
        lvl = a.get("level", "info")
        level_counts[lvl] = level_counts.get(lvl, 0) + 1

    result_lines = [f"  共 {len(alerts)} 条告警: {dict(level_counts)}"]
    result_lines.append("")

    # 只显示最近 max_items 条
    for a in alerts[-max_items:]:
        ts = a.get("ts", 0)
        ts_str = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "?"
        rule = a.get("rule", "?")
        level = a.get("level", "info")
        desc = a.get("description", "")
        match_text = a.get("match", "")[:80]
        line = f"  [{ts_str}] [{level.upper()}] {rule}"
        if desc:
            line += f" — {desc}"
        if match_text:
            line += f"\n    匹配: {match_text}"
        result_lines.append(line)

    return "\n".join(result_lines)


def _tail_file(filepath: Path, lines: int = 50) -> str:
    """读取文件末尾 N 行。"""
    try:
        all_lines = filepath.read_text(errors="replace").splitlines()
        tailed = all_lines[-lines:]
        return "\n".join(tailed)
    except Exception as e:
        logger.debug("文件尾部读取失败 %s: %s", filepath, e)
        return "(文件读取失败)"


def _fallback_read_jsonl_text(filepath: Path, max_lines: int = 10) -> str:
    """JSONL 回退：直接格式化每行的 JSON。"""
    try:
        raw_lines = filepath.read_text(encoding="utf-8").strip().splitlines()
    except Exception as e:
        logger.debug("JSONL 读取失败 %s: %s", filepath, e)
        return ""
    if not raw_lines:
        return ""

    result = []
    for line in raw_lines[-max_lines:]:
        try:
            obj = json.loads(line)
            result.append(json.dumps(obj, ensure_ascii=False, indent=2)[:500])
        except json.JSONDecodeError:
            result.append(line[:200])
    return "\n".join(result)


def _collect_deep_schemas(
    exports_dir: Path,
    root: Path,
    meta_file: Path,
) -> str:
    """收集深度 schema 数据。"""
    if not exports_dir.exists():
        return ""

    sections = []

    # 查找所有导出的 XML
    schema_files = {
        "gpu-frame-time": exports_dir / "gpu-frame-time.xml",
        "network-connection-stat": exports_dir / "network-connection-stat.xml",
        "vm-tracking": exports_dir / "vm-tracking.xml",
        "metal-performance": exports_dir / "metal-performance.xml",
    }

    for name, xml_path in schema_files.items():
        if xml_path.exists() and xml_path.stat().st_size > 100:
            try:
                from .deep_export import (
                    DEEP_SCHEMAS,
                    parse_gpu_frame_time,
                    parse_network_stat,
                    parse_vm_tracking,
                    parse_metal_performance,
                    format_deep_report,
                )
                parsers = {
                    "gpu-frame-time": parse_gpu_frame_time,
                    "network-connection-stat": parse_network_stat,
                    "vm-tracking": parse_vm_tracking,
                    "metal-performance": parse_metal_performance,
                }
                parser = parsers.get(name)
                if parser:
                    data = parser(xml_path)
                    report = format_deep_report(data, name)
                    sections.append(report)
            except Exception as e:
                logger.debug("解析 schema %s 失败: %s", name, e)

    # 也检查从 trace 导出的 power 指标
    power_xml = exports_dir / "SystemPowerLevel.xml"
    if power_xml.exists() and power_xml.stat().st_size > 100:
        try:
            text = power_xml.read_text(errors="replace")
            # 简要提取数值
            vals = re.findall(r"<c[^>]*>([^<]+)</c>", text)
            if vals:
                sections.append(f"## SystemPowerLevel\n  样本数: {len(vals) // 3}")
        except Exception as e:
            logger.debug("SystemPowerLevel XML 解析失败: %s", e)

    return "\n\n".join(sections)


def _load_meta_json(session_dir: str) -> Dict[str, Any]:
    """加载 session meta.json。"""
    meta_file = Path(session_dir).expanduser().resolve() / "meta.json"
    if not meta_file.exists():
        return {}
    try:
        return json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("meta.json 加载失败 %s: %s", session_dir, e)
        return {}


# ── 文本截断 ──

def _estimate_tokens(text: str) -> int:
    """
    估算文本的 token 数。

    规则: 1 token ≈ 1.5 中文字符 或 0.75 英文单词。
    简化实现: 统计中文字符和英文单词数，加权求和。
    """
    # 中文字符
    zh_chars = len(re.findall(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]', text))
    # 英文单词 (含数字)
    en_words = len(re.findall(r'[a-zA-Z0-9_]+', text))
    # 其他字符按英文估算
    other_chars = len(text) - zh_chars - sum(len(w) for w in re.findall(r'[a-zA-Z0-9_]+', text))

    zh_tokens = zh_chars / CHARS_PER_TOKEN_ZH
    en_tokens = en_words / 0.75  # 1 token ≈ 0.75 word
    other_tokens = other_chars / CHARS_PER_TOKEN_EN

    return int(zh_tokens + en_tokens + other_tokens)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """截断文本到指定 token 数以内。"""
    est = _estimate_tokens(text)
    if est <= max_tokens:
        return text

    # 按 token 比例截断字符数
    ratio = max_tokens / est
    target_chars = int(len(text) * ratio * 0.95)  # 留 5% 余量

    truncated = text[:target_chars]
    # 在最后一个换行符处截断，避免截断行
    last_nl = truncated.rfind("\n")
    if last_nl > target_chars // 2:
        truncated = truncated[:last_nl]

    return truncated + "\n\n... (数据已截断以适应 token 限制)"


# ── 响应解析辅助 ──

def _extract_section(text: str, heading: str) -> str:
    """从 Markdown 格式文本中提取指定章节内容。"""
    # 匹配 ## xxx 到下一个 ## 或文本结尾
    pattern = rf"##\s+{re.escape(heading)}.*?\n(.*?)(?=\n##\s|\Z)"
    m = re.search(pattern, text, re.S | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


def _extract_numbered_items(text: str) -> List[str]:
    """提取编号列表项 (1. xxx / 1) xxx / - xxx / • xxx)。"""
    if not text:
        return []

    items = []
    # 匹配编号列表: "1. xxx" 或 "1) xxx"
    for m in re.finditer(
        r'(?:^\d+[.)]\s*|^[•\-]\s*)(.+?)(?=(?:^\d+[.)]\s*|^[•\-]\s*)|\Z)',
        text, re.S | re.M,
    ):
        item = m.group(1).strip()
        if item:
            items.append(item)

    return items


def _extract_priority_items(text: str) -> List[Dict[str, Any]]:
    """提取优先级排序项。"""
    if not text:
        return []

    items = []
    # 匹配 [P0/P1/P2] 或 P0/P1/P2 标记
    for m in re.finditer(
        r'\[?(P[012])\]?\s*[:：]?\s*(.+?)(?=\[?P[012]|$)',
        text, re.S | re.IGNORECASE,
    ):
        level = m.group(1).upper()
        content = m.group(2).strip()
        if content:
            items.append({"level": level, "text": content[:200]})

    # 如果没有 P0/P1/P2 标记，按行解析
    if not items:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            # 尝试检测优先级关键词
            level = "P2"
            if any(kw in line.lower() for kw in ("critical", "严重", "高优", "必须", "crash")):
                level = "P0"
            elif any(kw in line.lower() for kw in ("important", "重要", "建议", "should")):
                level = "P1"
            items.append({"level": level, "text": line[:200]})

    return items


def _fallback_extract_paragraphs(text: str, keyword: str) -> List[str]:
    """回退: 按段落提取包含关键词的内容。"""
    items = []
    for para in text.split("\n\n"):
        para = para.strip()
        if para and len(para) > 20:
            items.append(para[:300])
    return items[:10]


# ── 回归分析辅助 ──

def _extract_power_summary(
    meta: Dict[str, Any],
    ctx: DiagnosisContext,
) -> Dict[str, Any]:
    """从 meta 和 context 提取功耗指标摘要。"""
    result: Dict[str, Any] = {}

    # 从 meta 提取 report 中的 metrics
    # (report.json 中有 display_avg, cpu_avg 等)
    report_file = Path(ctx.session_dir) / "report.json"
    if report_file.exists():
        try:
            report = json.loads(report_file.read_text(encoding="utf-8"))
            metrics = report.get("metrics", {})
            for key in ("display_avg", "cpu_avg", "networking_avg"):
                if metrics.get(key) is not None:
                    result[key] = metrics[key]
        except Exception as e:
            logger.debug("report.json 读取失败: %s", e)

    # 从电池数据提取摘要
    if ctx.power_data:
        # 尝试提取电池百分比变化
        pct_match = re.findall(r'(\d+)%', ctx.power_data)
        if len(pct_match) >= 2:
            result["battery_start_pct"] = int(pct_match[0])
            result["battery_end_pct"] = int(pct_match[-1])
            result["battery_drain"] = int(pct_match[0]) - int(pct_match[-1])

    return result


def _extract_top_symbols(hotspots_text: str, top_n: int = 10) -> Dict[str, float]:
    """从热点文本中提取 Top N 符号及权重。"""
    result: Dict[str, float] = {}
    if not hotspots_text:
        return result

    # 匹配格式: "  1. symbol_name   12.5%  (xxx samples)"
    for m in re.finditer(
        r'\d+\.\s+(\S+(?:\s+\S+){0,3}?)\s+(\d+\.?\d*)%',
        hotspots_text,
    ):
        sym = m.group(1).strip()
        pct = float(m.group(2))
        if sym and sym not in ("──", "(无"):
            result[sym] = pct

    return dict(list(result.items())[:top_n])


# ── WebKit 专项辅助 ──

def _detect_webkit_patterns(ctx: DiagnosisContext) -> List[str]:
    """从上下文中检测 WebKit 相关性能模式。"""
    patterns = []

    combined = "\n".join([
        ctx.hotspots,
        ctx.webcontent_data,
        ctx.syslog_alerts,
        ctx.process_data,
    ]).lower()

    if "jsc_" in combined or "javascriptcore" in combined or "jit_" in combined:
        patterns.append(
            "JavaScriptCore (JSC) 引擎热点 — JS 执行消耗大量 CPU"
        )

    if "webcore" in combined or "render" in combined:
        patterns.append(
            "WebCore 渲染管线热点 — 布局/绘制消耗较大"
        )

    if "network" in combined or "url" in combined:
        patterns.append(
            "网络活动频繁 — 检查请求合并和缓存策略"
        )

    if "wkwebview" in combined or "webprocess" in combined:
        patterns.append(
            "WKWebView 进程活动 — 检查 IPC 通信频率"
        )

    if "thermal" in combined:
        patterns.append(
            "热管理告警 — WebKit 渲染可能导致设备过热"
        )

    if "memory" in combined or "jetsam" in combined or "oom" in combined:
        patterns.append(
            "内存压力 — WebContent 进程可能占用过多内存"
        )

    if "composit" in combined or "layer" in combined or "ca::" in combined:
        patterns.append(
            "合成/图层热点 — 检查 CSS 动画和图层合并策略"
        )

    if "layout" in combined or "style" in combined:
        patterns.append(
            "布局/样式计算热点 — 减少 DOM 复杂度和强制同步布局"
        )

    return patterns


def _get_webkit_suggestions(ctx: DiagnosisContext) -> List[tuple]:
    """生成 WebKit 优化建议列表。"""
    suggestions = [
        (
            "减少 JavaScript 主线程阻塞",
            "使用 requestAnimationFrame 替代 setTimeout 进行动画; "
            "将长任务拆分为 <50ms 的片段 (scheduler.yield()); "
            "使用 Web Worker 处理计算密集型任务。"
        ),
        (
            "优化网络请求策略",
            "合并小请求为批处理; 使用 HTTP/2 多路复用; "
            "启用 Service Worker 缓存; 预加载关键资源 (rel=preload); "
            "减少第三方脚本数量。"
        ),
        (
            "减少 DOM 复杂度和重排",
            "使用虚拟滚动 (virtual scrolling) 处理长列表; "
            "避免频繁读取 layout 属性 (offsetHeight 等) 导致强制同步布局; "
            "使用 CSS contain 属性限制布局影响范围; "
            "批量 DOM 操作使用 DocumentFragment。"
        ),
        (
            "优化图片和媒体资源",
            "使用 WebP/AVIF 格式减少传输大小; "
            "实现懒加载 (loading=lazy); "
            "使用 srcset 提供多分辨率适配; "
            "视频使用 autoplay + muted 替代 GIF。"
        ),
        (
            "控制 WebContent 进程内存",
            "定期释放不再使用的 JS 对象引用; "
            "使用 WeakRef/WeakMap 管理 DOM 引用; "
            "限制离屏内容 (如隐藏 tab) 的资源消耗; "
            "使用 structuredClone 替代 JSON.parse/stringify。"
        ),
        (
            "WKWebView 原生侧优化",
            "减少 evaluateJavaScript 调用频率 (批处理); "
            "使用 WKUserScript 提前注入 JS; "
            "使用 WKContentRuleList 拦截不必要的请求; "
            "设置合适的 WKWebViewConfiguration 缓存策略; "
            "使用 snapshotting API 替代截图通信。"
        ),
        (
            "CSS 动画和合成层优化",
            "优先使用 transform/opacity 做动画 (GPU 合成); "
            "使用 will-change 提示浏览器优化 (但不要滥用); "
            "减少 box-shadow/filter 等触发 paint 的属性动画; "
            "使用 CSS containment (contain: layout style paint)。"
        ),
    ]

    # 根据检测结果动态补充
    combined = "\n".join([ctx.hotspots, ctx.webcontent_data]).lower()

    if "jsc_" in combined or "javascriptcore" in combined:
        suggestions.append((
            "JavaScriptCore 编译优化",
            "检查是否存在频繁的 JIT 编译/去优化循环; "
            "避免在热路径中使用 eval/with/try-catch (阻碍 JIT); "
            "保持函数参数类型稳定 (monomorphic IC); "
            "考虑使用 Wasm 替代计算密集型 JS。"
        ))

    if "composit" in combined or "layer" in combined:
        suggestions.append((
            "CoreAnimation 合成优化",
            "减少隐式合成层的数量; "
            "检查图层树深度 (>30 层时性能下降); "
            "使用 shouldRasterize 缓存复杂合成层; "
            "避免离屏渲染 (cornerRadius + masksToBounds)。"
        ))

    return suggestions
