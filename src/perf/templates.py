"""
TemplateLibrary — Instruments 录制模板注册与扩展。

功能:
- 内置常用 Instruments 模板定义
- 支持自定义 .instruments 模板文件
- 多模板并行录制（每个模板独立 trace 文件）
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

    cmd.extend(["--time", f"{int(duration_sec)}s"])

    if output_path:
        cmd.extend(["--output", output_path])

    cmd.append("--no-prompt")

    if extra_args:
        cmd.extend(extra_args)

    return cmd


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
