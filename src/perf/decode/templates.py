"""
TemplateLibrary — Instruments 录制模板注册与扩展。

功能:
- 内置常用 Instruments 模板定义
- 支持自定义 .instruments 模板文件
- **Composite 模式**: 多 instrument 合并到单个 xctrace 录制 (解决 iOS 互斥限制)
- 多模板并行录制（每个模板独立 trace 文件）— 旧模式, 已被 composite 取代
- 模板→xctrace 命令行参数映射
- trace 文件命名规范
"""

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, List, Any


# ── 模板定义 ──

@dataclass
class InstrumentTemplate:
    """单个 Instruments 录制模板"""
    name: str                           # 模板显示名
    template_arg: str                   # --template 参数值
    schemas: List[str] = field(default_factory=list)  # 该模板可导出的 XML schema
    description: str = ""
    alias: str = ""                     # 短别名 (CLI 用)
    requires_attach: bool = True        # 是否需要 --attach
    custom_path: str = ""               # 自定义 .instruments 模板文件路径

    def trace_filename(self, tag: str = "perf") -> str:
        """生成 trace 文件名"""
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', self.alias or self.name)
        return f"{tag}_{safe_name}.trace"


# ── 内置模板 ──

BUILTIN_TEMPLATES: Dict[str, InstrumentTemplate] = {
    "power": InstrumentTemplate(
        name="Power Profiler",
        template_arg="Power Profiler",
        alias="power",
        schemas=["SystemPowerLevel", "ProcessSubsystemPowerImpact"],
        description="功耗分析: Display/CPU/Networking 功耗",
    ),
    "time": InstrumentTemplate(
        name="Time Profiler",
        template_arg="Time Profiler",
        alias="time",
        schemas=["time-profile", "TimeProfiler"],
        description="CPU 时间分析: 函数调用热点",
    ),
    "network": InstrumentTemplate(
        name="Network",
        template_arg="Network",
        alias="network",
        schemas=["networking"],
        description="网络流量: 连接数/字节/延迟",
    ),
    "allocations": InstrumentTemplate(
        name="Allocations",
        template_arg="Allocations",
        alias="allocations",
        schemas=["Allocations"],
        description="内存分配: malloc/free 追踪",
        requires_attach=False,
    ),
    "leaks": InstrumentTemplate(
        name="Leaks",
        template_arg="Leaks",
        alias="leaks",
        schemas=["Leaks"],
        description="内存泄漏检测",
        requires_attach=False,
    ),
    "gpu": InstrumentTemplate(
        name="GPU",
        template_arg="GPU",
        alias="gpu",
        schemas=["GPU"],
        description="GPU 负载分析",
    ),
    "coreanimation": InstrumentTemplate(
        name="Core Animation",
        template_arg="Core Animation",
        alias="coreanim",
        schemas=["CoreAnimationFPS"],
        description="渲染帧率 / Core Animation 性能",
    ),
    "metal": InstrumentTemplate(
        name="Metal System Trace",
        template_arg="Metal System Trace",
        alias="metal",
        schemas=["MetalGPU", "MetalIO"],
        description="Metal 渲染管线追踪",
    ),
    "filesystem": InstrumentTemplate(
        name="File System",
        template_arg="File System",
        alias="fs",
        schemas=["FileSystem"],
        description="文件 I/O 追踪",
    ),
    "systemtrace": InstrumentTemplate(
        name="System Trace",
        template_arg="System Trace",
        alias="systrace",
        schemas=[
            "system-load",
            "device-thermal-state-intervals",
            "time-profile",
            "cpu-state",
            "context-switch",
            "thread-state",
        ],
        description="全系统追踪: CPU 负载+温度状态+调用栈+线程状态 综合",
    ),
    # ── 单独 instrument (仅用于 composite --instrument) ──
    "caf": InstrumentTemplate(
        name="Core Animation FPS",
        template_arg="Core Animation FPS",
        alias="caf",
        schemas=["CoreAnimationFPS"],
        description="Core Animation 帧率 (轻量)",
    ),
    "thermal": InstrumentTemplate(
        name="Thermal State",
        template_arg="Thermal State",
        alias="thermal",
        schemas=["device-thermal-state-intervals"],
        description="设备热管理状态追踪",
    ),
    "sysload": InstrumentTemplate(
        name="System Load",
        template_arg="System Load",
        alias="sysload",
        schemas=["system-load"],
        description="系统级 CPU/IO 负载概览",
    ),
    "actmon": InstrumentTemplate(
        name="Activity Monitor",
        template_arg="Activity Monitor",
        alias="actmon",
        schemas=["cpu-track", "physical-memory-track"],
        description="进程级 CPU/Memory 活动监控",
        requires_attach=False,
    ),
}

# ── Composite 预置组合 ──
# 每个组合定义: base_template + 附加 instrument 列表 + 聚合 schema
# 用途: 一个 xctrace 进程录制所有 instrument, 解决 iOS 互斥限制

COMPOSITE_PRESETS: Dict[str, Dict[str, Any]] = {
    "full": {
        "description": "全能组合: 功耗+CPU热点+帧率+网络+GPU+活动监控+热管理+系统负载",
        "base_template": "power",
        "instruments": [
            "Time Profiler",
            "Core Animation FPS",
            "Network",
            "GPU",
            "Activity Monitor",
            "Thermal State",
            "System Load",
        ],
        "schemas": [
            "SystemPowerLevel", "ProcessSubsystemPowerImpact",
            "time-profile", "TimeProfiler",
            "CoreAnimationFPS",
            "networking",
            "GPU",
            "cpu-track", "physical-memory-track",
            "device-thermal-state-intervals",
            "system-load",
        ],
    },
    "power_cpu": {
        "description": "功耗+CPU热点: Power Profiler + Time Profiler",
        "base_template": "power",
        "instruments": ["Time Profiler"],
        "schemas": [
            "SystemPowerLevel", "ProcessSubsystemPowerImpact",
            "time-profile", "TimeProfiler",
        ],
    },
    "webperf": {
        "description": "Web渲染性能: 功耗+CPU热点+帧率+网络",
        "base_template": "power",
        "instruments": ["Time Profiler", "Core Animation FPS", "Network"],
        "schemas": [
            "SystemPowerLevel", "ProcessSubsystemPowerImpact",
            "time-profile", "TimeProfiler",
            "CoreAnimationFPS",
            "networking",
        ],
    },
    "gpu_full": {
        "description": "GPU 完整分析: 功耗+GPU+Metal+帧率",
        "base_template": "gpu",
        "instruments": ["Core Animation FPS", "Metal System Trace", "Power Profiler"],
        "schemas": [
            "GPU", "MetalGPU", "MetalIO",
            "CoreAnimationFPS",
            "SystemPowerLevel",
        ],
    },
    "memory": {
        "description": "内存分析: Allocations + Leaks + Activity Monitor",
        "base_template": "allocations",
        "instruments": ["Leaks", "Activity Monitor"],
        "schemas": [
            "Allocations", "Leaks",
            "cpu-track", "physical-memory-track",
        ],
    },
}


class TemplateLibrary:
    """
    Instruments 模板注册中心。

    用法:
        lib = TemplateLibrary()
        lib.list_templates()
        tpl = lib.get("power")
        cmd = tpl.build_xctrace_cmd(device, attach, duration, output_path)
    """

    def __init__(self, custom_templates_dir: Optional[str] = None):
        self.templates: Dict[str, InstrumentTemplate] = dict(BUILTIN_TEMPLATES)
        self.custom_dir = Path(custom_templates_dir) if custom_templates_dir else None
        if self.custom_dir and self.custom_dir.exists():
            self._load_custom_templates()

    # ── 查询 ──

    def get(self, key: str) -> Optional[InstrumentTemplate]:
        """通过别名或名称获取模板"""
        return self.templates.get(key)

    def resolve(self, name: str) -> Optional[InstrumentTemplate]:
        """模糊匹配: 别名 > 内置名 > 显示名"""
        # 1. 精确匹配别名
        if name in self.templates:
            return self.templates[name]
        # 2. 按显示名匹配
        for tpl in self.templates.values():
            if tpl.name.lower() == name.lower():
                return tpl
        # 3. 按别名前缀匹配
        for key, tpl in self.templates.items():
            if tpl.alias and key.startswith(name.lower()):
                return tpl
        return None

    def list_templates(self) -> List[Dict[str, Any]]:
        """列出所有可用模板"""
        return [
            {
                "alias": key,
                "name": tpl.name,
                "description": tpl.description,
                "schemas": tpl.schemas,
                "requires_attach": tpl.requires_attach,
                "custom": bool(tpl.custom_path),
            }
            for key, tpl in self.templates.items()
        ]

    def resolve_multi(self, spec: str) -> List[InstrumentTemplate]:
        """
        解析逗号分隔的模板规格, 返回模板列表。
        spec 格式: "power,time,gpu"
        特殊值: "all" -> 所有内置模板
        """
        if spec.lower() == "all":
            return list(self.templates.values())

        results = []
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            tpl = self.resolve(part)
            if tpl:
                results.append(tpl)
        return results

    # ── 自定义模板 ──

    def register(self, template: InstrumentTemplate):
        """注册新模板"""
        key = template.alias or template.name.lower().replace(" ", "_")
        self.templates[key] = template

    def _load_custom_templates(self):
        """从自定义目录加载模板定义文件"""
        if not self.custom_dir or not self.custom_dir.exists():
            return

        for f in sorted(self.custom_dir.iterdir()):
            if f.suffix not in (".json", ".yaml", ".yml"):
                continue
            try:
                text = f.read_text(errors="replace")
                if f.suffix == ".json":
                    data = json.loads(text)
                else:
                    try:
                        import yaml
                        data = yaml.safe_load(text)
                    except ImportError:
                        continue

                for item in data.get("templates", []):
                    tpl = InstrumentTemplate(
                        name=item.get("name", "custom"),
                        template_arg=item.get("template_arg", item.get("name", "")),
                        alias=item.get("alias", ""),
                        schemas=item.get("schemas", []),
                        description=item.get("description", ""),
                        requires_attach=item.get("requires_attach", True),
                        custom_path=str(f),
                    )
                    self.register(tpl)
            except Exception:
                continue


# ── xctrace 录制命令构建 ──

def build_xctrace_record_cmd(
    template: InstrumentTemplate,
    device: str,
    attach: str = "",
    duration_sec: int = 1800,
    output_path: str = "",
    extra_args: Optional[List[str]] = None,
) -> List[str]:
    """
    构建 xcrun xctrace record 命令。
    """
    cmd = ["xcrun", "xctrace", "record"]

    if template.custom_path:
        cmd.extend(["--template", template.custom_path])
    else:
        cmd.extend(["--template", template.template_arg])

    cmd.extend(["--device", device])

    if template.requires_attach and attach:
        cmd.extend(["--attach", attach])

    cmd.extend(["--time-limit", f"{int(duration_sec)}s"])

    if output_path:
        cmd.extend(["--output", output_path])

    cmd.append("--no-prompt")

    if extra_args:
        cmd.extend(extra_args)

    return cmd


def build_composite_record_cmd(
    base_template: InstrumentTemplate,
    instruments: List[str],
    device: str,
    attach: str = "",
    duration_sec: int = 1800,
    output_path: str = "",
    extra_args: Optional[List[str]] = None,
) -> List[str]:
    """
    构建 composite 模式的 xctrace record 命令。

    使用 --template 指定基础模板, 然后用多个 --instrument 叠加。
    所有 instrument 在单个 xctrace 进程中录制, 产出单个 .trace 文件。
    解决 iOS 设备同一时刻只允许一个 xctrace 录制的限制。

    Args:
        base_template: 基础模板 (决定录制的主要 instrument 和显示设置)
        instruments: 附加 instrument 名称列表 (对应 xctrace list instruments 输出)
        device: 设备 UDID
        attach: 目标进程名
        duration_sec: 录制时长 (秒)
        output_path: .trace 输出路径
        extra_args: 额外 xctrace 参数

    Returns:
        完整的命令行参数列表

    Example:
        >>> cmd = build_composite_record_cmd(
        ...     base_template=tpl, instruments=["Time Profiler", "Network"],
        ...     device="00008120-...", attach="Soul_New", duration_sec=300,
        ...     output_path="/tmp/composite.trace",
        ... )
        >>> " ".join(cmd)
        'xcrun xctrace record --template Power Profiler --instrument Time Profiler
         --instrument Network --device 00008120-... --attach Soul_New
         --time-limit 300s --output /tmp/composite.trace --no-prompt'
    """
    cmd = ["xcrun", "xctrace", "record"]

    # 基础模板
    if base_template.custom_path:
        cmd.extend(["--template", base_template.custom_path])
    else:
        cmd.extend(["--template", base_template.template_arg])

    # 附加 instruments (每个 --instrument 叠加一个)
    for inst_name in instruments:
        cmd.extend(["--instrument", inst_name])

    cmd.extend(["--device", device])

    if base_template.requires_attach and attach:
        cmd.extend(["--attach", attach])

    cmd.extend(["--time-limit", f"{int(duration_sec)}s"])

    if output_path:
        cmd.extend(["--output", output_path])

    cmd.append("--no-prompt")

    if extra_args:
        cmd.extend(extra_args)

    return cmd


def resolve_composite(
    spec: str, lib: Optional["TemplateLibrary"] = None
) -> Optional[Dict[str, Any]]:
    """
    解析 composite 规格字符串, 返回构建命令所需的完整信息。

    支持格式:
    - 预置名: "full", "webperf", "power_cpu", "gpu_full", "memory"
    - 自由组合: "power+time+network" (第一个为 base, 其余为附加 instrument)

    Returns:
        {
            "base_template": InstrumentTemplate,
            "instruments": ["Time Profiler", ...],   # 附加 instrument 显示名
            "schemas": [...],                         # 聚合可导出 schema
            "preset": "full" or "",                   # 使用的预置名
        }
        或 None (无法解析)
    """
    if lib is None:
        lib = TemplateLibrary()

    # 1. 尝试匹配预置名
    preset = COMPOSITE_PRESETS.get(spec.lower())
    if preset:
        base_tpl = lib.get(preset["base_template"])
        if base_tpl is None:
            return None
        return {
            "base_template": base_tpl,
            "instruments": list(preset["instruments"]),
            "schemas": list(preset["schemas"]),
            "preset": spec.lower(),
        }

    # 2. 尝试 "+" 分隔的自由组合: "power+time+network"
    if "+" in spec:
        parts = [p.strip() for p in spec.split("+") if p.strip()]
        if len(parts) < 2:
            return None

        # 第一个作为 base template
        base_tpl = lib.resolve(parts[0])
        if base_tpl is None:
            return None

        # 其余映射为 instrument 显示名
        extra_instruments = []
        extra_schemas = list(base_tpl.schemas)
        for part in parts[1:]:
            resolved = lib.resolve(part)
            if resolved:
                extra_instruments.append(resolved.name)
                extra_schemas.extend(resolved.schemas)
            else:
                # 直接当作 instrument 名传入 (用户可能用 xctrace list instruments 中的名字)
                extra_instruments.append(part)

        return {
            "base_template": base_tpl,
            "instruments": extra_instruments,
            "schemas": extra_schemas,
            "preset": "",
        }

    return None


# ── 设备探测 ──

def list_available_devices() -> List[Dict[str, str]]:
    """列出 xctrace 可用设备"""
    try:
        proc = subprocess.run(
            ["xcrun", "xctrace", "list", "devices"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=15,
        )
    except Exception:
        return []

    devices = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # 格式示例: "== Devices ==,"  或  "  iPhone 14 Pro (00008120-00164C893AEB401E)"
        m = re.match(r'(.+?)\s*\(([^)]+)\)', line)
        if m:
            name = m.group(1).strip()
            udid = m.group(2).strip()
            devices.append({"name": name, "udid": udid})
    return devices


def list_available_templates() -> List[Dict[str, str]]:
    """列出 xctrace 可用的内置模板"""
    try:
        proc = subprocess.run(
            ["xcrun", "xctrace", "list", "templates"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=15,
        )
    except Exception:
        return []

    templates = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line and not line.startswith("=") and not line.startswith("=="):
            templates.append({"name": line})
    return templates
