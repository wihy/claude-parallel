"""
Symbolicate — dSYM 符号化模块。

将 Time Profiler 采集到的十六进制原始地址解析为可读符号名，
配合 sampling.parse_timeprofiler_xml 返回的 [(symbol, weight)] 使用。

核心流程:
1. find_dsym / find_dsym_by_uuid  — 定位 dSYM 文件
2. cache_dsym_map                  — 预缓存地址→符号映射（dsymutil）
3. symbolicate_addresses           — 调用 atos 批量符号化
4. symbolicate_hotspots            — 对未符号化的热点列表批量处理
5. swift_demangle                  — Swift mangled name 还原
"""

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 常量 ──

DERIVEDDATA_DIR = Path.home() / "Library" / "Developer" / "Xcode" / "DerivedData"
ARCHIVES_DIR = Path.home() / "Library" / "Developer" / "Xcode" / "Archives"
CACHE_DIR = Path.home() / ".claude-parallel" / "symbol_cache"
ATOS_BATCH_SIZE = 500

# ── 内部辅助 ──


def _is_unsymbolicated(symbol: str) -> bool:
    """判断 symbol 是否是未符号化的十六进制地址。

    接受:
    - 纯 '0x1a2b3c4d'
    - 带偏移 '0x1a2b3c4d + 45'
    - Xcode 行内格式 '0x1a2b3c4d MyModule.function + 45' （只有地址部分）

    Returns True 当 symbol 以 0x 开头且不包含已解析的函数名。
    """
    if not symbol:
        return False
    s = symbol.strip()
    # 纯地址: '0x1a2b3c4d' 或 '0x1a2b3c4d + 45'
    if re.match(r"^0x[0-9a-fA-F]+(\s*\+\s*\d+)?$", s):
        return True
    # Xcode 行内格式但名字仍为 '?'
    if re.match(r"^0x[0-9a-fA-F]+\s+\?", s):
        return True
    return False


def _extract_address(symbol: str) -> Optional[str]:
    """从 symbol 字符串中提取十六进制地址。"""
    m = re.match(r"^(0x[0-9a-fA-F]+)", symbol.strip())
    return m.group(1) if m else None


def _ensure_cache_dir() -> Path:
    """确保缓存目录存在并返回路径。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR


def _cache_path(binary_name: str) -> Path:
    """返回指定 binary 的缓存文件路径。"""
    return CACHE_DIR / f"{binary_name}.json"


# ── dSYM 查找 ──


def find_dsym(
    app_bundle_id: str,
    build_dir: Optional[str] = None,
) -> Optional[Path]:
    """从 Xcode DerivedData 自动查找 dSYM。

    搜索策略:
    1. 若提供 build_dir，直接在其中搜索 *.dSYM
    2. 否则扫描 ~/Library/Developer/Xcode/DerivedData/*/Build/Products/
       找到匹配 app_bundle_id 的 .app，再找同名 .dSYM

    Args:
        app_bundle_id: 如 'com.example.MyApp'
        build_dir: 显式指定 build products 目录

    Returns:
        dSYM 目录路径，找不到返回 None
    """
    if build_dir:
        products = Path(build_dir)
        if products.is_dir():
            dsym = _find_dsym_in_products(products, app_bundle_id)
            if dsym:
                return dsym

    # 扫描 DerivedData
    if not DERIVEDDATA_DIR.is_dir():
        logger.warning("DerivedData 目录不存在: %s", DERIVEDDATA_DIR)
        return None

    for entry in DERIVEDDATA_DIR.iterdir():
        if not entry.is_dir():
            continue
        products_dir = entry / "Build" / "Products"
        if not products_dir.is_dir():
            continue

        dsym = _find_dsym_in_products(products_dir, app_bundle_id)
        if dsym:
            logger.info("找到 dSYM: %s (bundle=%s)", dsym, app_bundle_id)
            return dsym

    logger.info("未找到 dSYM: bundle=%s", app_bundle_id)
    return None


def _find_dsym_in_products(
    products_dir: Path,
    app_bundle_id: str,
) -> Optional[Path]:
    """在 Build/Products 目录中查找与 bundle ID 匹配的 dSYM。"""
    # 遍历所有配置子目录 (Debug-iphoneos, Release-iphoneos 等)
    for config_dir in products_dir.rglob("*"):
        if not config_dir.is_dir():
            continue

        # 查找 .app 目录匹配 bundle ID
        for app_dir in config_dir.glob("*.app"):
            # 检查 Info.plist 中的 CFBundleIdentifier
            plist_path = app_dir / "Info.plist"
            if plist_path.exists():
                bundle_id = _read_bundle_id(plist_path)
                if bundle_id == app_bundle_id:
                    # 找到匹配 app，找同名 dSYM
                    app_name = app_dir.stem
                    dsym_path = config_dir / f"{app_name}.app.dSYM"
                    if dsym_path.is_dir():
                        return dsym_path

        # 回退: 直接找 dSYM，名字中包含 bundle ID 的关键字
        # (对于没有 Info.plist 的情况)
    return None


def _read_bundle_id(plist_path: Path) -> Optional[str]:
    """用 PlistBuddy 读取 CFBundleIdentifier。"""
    try:
        result = subprocess.run(
            ["/usr/libexec/PlistBuddy", "-c",
             "Print :CFBundleIdentifier", str(plist_path)],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("PlistBuddy 读取失败 %s: %s", plist_path, exc)
    return None


def find_dsym_in_archives(
    app_bundle_id: str = "",
    app_name: str = "",
) -> Optional[Path]:
    """从 Xcode Organizer Archives 查找 dSYM。

    搜索策略:
    1. 扫描 ~/Library/Developer/Xcode/Archives/ 下所有 .xcarchive
    2. 每个 .xcarchive 中查找 dSYMs/ 目录
    3. 通过 Info.plist 匹配 bundle ID 或通过名称匹配

    适用于 Release / App Store 包，因为 Archive 包含完整 dSYM。

    Args:
        app_bundle_id: App Bundle ID (如 com.example.MyApp)
        app_name: App 名 (如 MyApp，可选但加速搜索)

    Returns:
        dSYM 目录路径，找不到返回 None
    """
    if not ARCHIVES_DIR.is_dir():
        logger.debug("Archives 目录不存在: %s", ARCHIVES_DIR)
        return None

    # 收集所有 xcarchive，按修改时间倒序 (最新的优先)
    archives = []
    for date_dir in ARCHIVES_DIR.rglob("*.xcarchive"):
        if date_dir.is_dir():
            archives.append(date_dir)
    archives.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    for archive in archives:
        # xcarchive 结构:
        #   Foo.xcarchive/dSYMs/Foo.app.dSYM/
        #   Foo.xcarchive/Info.plist (含 ApplicationProperties)
        dsyms_dir = archive / "dSYMs"
        if not dsyms_dir.is_dir():
            continue

        # 如果指定了 bundle_id，先验证 archive 是否匹配
        if app_bundle_id:
            archive_plist = archive / "Info.plist"
            if archive_plist.exists():
                archive_bundle = _read_archive_bundle_id(archive_plist)
                if archive_bundle and archive_bundle != app_bundle_id:
                    continue

        # 如果指定了 app_name，直接查找匹配的 dSYM
        if app_name:
            dsym = dsyms_dir / f"{app_name}.app.dSYM"
            if dsym.is_dir():
                logger.info("Archive 找到 dSYM: %s (archive=%s)", dsym, archive.name)
                return dsym

        # 否则遍历所有 dSYM
        for dsym_entry in dsyms_dir.glob("*.dSYM"):
            if dsym_entry.is_dir():
                # 如果有 bundle_id，验证 dSYM 对应的 app 匹配
                if app_bundle_id:
                    # 尝试从 dSYM 名字推断 app 名
                    dsym_app_name = dsym_entry.name.replace(".app.dSYM", "")
                    if not dsym_app_name:
                        continue
                    # 验证: 查找同名 .app 中的 Info.plist
                    app_in_archive = archive / "Products" / "Applications" / f"{dsym_app_name}.app"
                    if app_in_archive.is_dir():
                        plist = app_in_archive / "Info.plist"
                        if plist.exists():
                            bid = _read_bundle_id(plist)
                            if bid == app_bundle_id:
                                logger.info("Archive 找到 dSYM: %s (archive=%s)", dsym_entry, archive.name)
                                return dsym_entry
                else:
                    # 无 bundle_id 限制，返回第一个
                    logger.info("Archive 找到 dSYM: %s (archive=%s)", dsym_entry, archive.name)
                    return dsym_entry

    logger.info("Archives 中未找到 dSYM: bundle=%s name=%s", app_bundle_id, app_name)
    return None


def _read_archive_bundle_id(plist_path: Path) -> Optional[str]:
    """读取 .xcarchive Info.plist 中的 bundle ID。"""
    try:
        result = subprocess.run(
            ["/usr/libexec/PlistBuddy", "-c",
             "Print :ApplicationProperties:CFBundleIdentifier", str(plist_path)],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        # 尝试另一种路径
        result = subprocess.run(
            ["/usr/libexec/PlistBuddy", "-c",
             "Print :ApplicationProperties:Application:CFBundleIdentifier", str(plist_path)],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def find_dsym_app_store_connect(
    bundle_id: str = "",
    app_id: str = "",
    api_key_id: str = "",
    issuer_id: str = "",
    private_key_path: str = "",
    output_dir: Optional[str] = None,
) -> Optional[Path]:
    """从 App Store Connect 下载 dSYM (Release / App Store 包)。

    使用 xcodebuild -downloadPlatform 或 altool (旧版) 下载。
    需要 Apple Developer 账号配置。

    策略:
    1. 优先使用 App Store Connect API (JWT)
    2. 回退到 xcodebuild -exportArchive -downloadDsyms
    3. 最后尝试 iTMSTransporter

    Args:
        bundle_id: App Bundle ID
        app_id: Apple App ID (数字，可选)
        api_key_id: App Store Connect API Key ID
        issuer_id: App Store Connect API Issuer ID
        private_key_path: API 私钥路径 (.p8)
        output_dir: 下载目标目录 (默认 ~/.claude-parallel/dsym_cache/)

    Returns:
        下载的 dSYM 路径，失败返回 None
    """
    dsym_cache = Path(output_dir) if output_dir else (
        Path.home() / ".claude-parallel" / "dsym_cache"
    )
    dsym_cache.mkdir(parents=True, exist_ok=True)

    # 策略 1: xcodebuild -downloadDsyms (Xcode 14+)
    # 需要在 Xcode 中登录 Apple ID
    if bundle_id:
        result = _try_xcodebuild_download(bundle_id, dsym_cache)
        if result:
            return result

    # 策略 2: App Store Connect API (JWT)
    if api_key_id and issuer_id and private_key_path:
        result = _try_asc_api_download(
            bundle_id=bundle_id,
            app_id=app_id,
            api_key_id=api_key_id,
            issuer_id=issuer_id,
            private_key_path=private_key_path,
            output_dir=dsym_cache,
        )
        if result:
            return result

    logger.info(
        "App Store Connect dSYM 下载失败。"
        "请确保: 1) Xcode 已登录 Apple ID  2) 或提供 API Key 参数"
    )
    return None


def _try_xcodebuild_download(
    bundle_id: str,
    output_dir: Path,
) -> Optional[Path]:
    """使用 xcodebuild -downloadDsyms 下载 dSYM。"""
    try:
        result = subprocess.run(
            ["xcodebuild", "-downloadDsyms", "-bundleIdentifier", bundle_id],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            # xcodebuild 下载到 ~/Downloads 或默认位置
            # 搜索最近下载的 dSYM
            downloaded = _find_recently_downloaded_dsym(bundle_id, output_dir)
            if downloaded:
                logger.info("xcodebuild 下载 dSYM 成功: %s", downloaded)
                return downloaded
            # 也搜索 Downloads 目录
            downloads = Path.home() / "Downloads"
            for dsym in downloads.glob("*.dSYM"):
                logger.info("在 Downloads 找到 dSYM: %s", dsym)
                return dsym
        else:
            logger.debug(
                "xcodebuild -downloadDsyms 失败: %s",
                result.stderr[:200],
            )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("xcodebuild -downloadDsyms 异常: %s", exc)
    return None


def _try_asc_api_download(
    bundle_id: str,
    app_id: str,
    api_key_id: str,
    issuer_id: str,
    private_key_path: str,
    output_dir: Path,
) -> Optional[Path]:
    """使用 App Store Connect API 下载 dSYM (JWT 认证)。

    实现流程:
    1. 生成 JWT token
    2. 调用 /v1/apps/{id}/appInfoLocalizations 或构建版本 API
    3. 下载 dSYM zip
    4. 解压到 output_dir
    """
    try:
        import jwt  # PyJWT
    except ImportError:
        logger.debug("PyJWT 未安装，跳过 ASC API 下载: pip install PyJWT")
        return None

    key_path = Path(private_key_path)
    if not key_path.exists():
        logger.warning("API 私钥不存在: %s", key_path)
        return None

    try:
        import time as _time
        now = int(_time.time())
        payload = {
            "iss": issuer_id,
            "iat": now,
            "exp": now + 1200,  # 20 分钟
            "aud": "appstoreconnect-v1",
        }
        headers = {"alg": "ES256", "kid": api_key_id, "typ": "JWT"}
        private_key = key_path.read_text()
        token = jwt.encode(payload, private_key, algorithm="ES256", headers=headers)

        # 需要知道 app_id 来查询构建版本
        if not app_id:
            logger.debug("ASC API 下载需要 app_id 参数")
            return None

        import urllib.request
        import zipfile
        import io
        import shutil

        api_base = "https://api.appstoreconnect.apple.com/v1"
        req = urllib.request.Request(
            f"{api_base}/apps/{app_id}/builds",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())

        builds = data.get("data", [])
        if not builds:
            logger.info("ASC: 无构建版本 (app_id=%s)", app_id)
            return None

        # 取最新构建版本
        latest = builds[0]
        build_id = latest["id"]
        relationships = latest.get("relationships", {})

        # 获取 dSYM 下载链接
        dsym_link = None
        if "appClipDefaultExperience" in relationships:
            pass  # 非标准路径

        # 尝试直接获取构建的 dSYM
        dsym_req = urllib.request.Request(
            f"{api_base}/builds/{build_id}/relationships/appClipDefaultExperience",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )

        # 实际 dSYM 下载使用 /builds/{id}/resourceLinks
        resource_req = urllib.request.Request(
            f"{api_base}/builds/{build_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(resource_req, timeout=30) as resp:
            build_data = json.loads(resp.read())

        # 提取 dSYM 文件 URL
        attrs = build_data.get("data", {}).get("attributes", {})
        dsym_url = attrs.get("dSYMUrl") or attrs.get("dsymUrl")

        if not dsym_url:
            logger.info("ASC: 最新构建无 dSYM 下载链接")
            return None

        # 下载并解压
        logger.info("ASC: 下载 dSYM: %s", dsym_url)
        with urllib.request.urlopen(dsym_url, timeout=300) as resp:
            zip_bytes = resp.read()

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(output_dir)

        # 找到解压后的 dSYM
        for dsym in output_dir.rglob("*.dSYM"):
            if dsym.is_dir():
                logger.info("ASC: dSYM 解压成功: %s", dsym)
                return dsym

    except Exception as exc:
        logger.warning("ASC API 下载 dSYM 失败: %s", exc)

    return None


def _find_recently_downloaded_dsym(
    bundle_id: str,
    search_dir: Path,
) -> Optional[Path]:
    """在指定目录和 DerivedData 中搜索最近的 dSYM。"""
    # 搜索 dsym_cache
    for dsym in search_dir.rglob("*.dSYM"):
        if dsym.is_dir():
            return dsym

    # 搜索 DerivedData
    for entry in DERIVEDDATA_DIR.iterdir():
        if not entry.is_dir():
            continue
        for dsym in (entry / "Build" / "Products").rglob("*.dSYM"):
            if dsym.is_dir():
                return dsym

    return None


def extract_binary_uuid(binary_path: str) -> Optional[str]:
    """用 dwarfdump 从二进制文件提取 UUID。

    可用于从 App 包中提取 UUID，再用于 Spotlight/Archive 搜索 dSYM。

    Args:
        binary_path: App 二进制文件路径 (如 MyApp.app/MyApp)

    Returns:
        UUID 字符串 (如 '1A2B3C4D-5E6F-...')，失败返回 None
    """
    try:
        result = subprocess.run(
            ["dwarfdump", "--uuid", binary_path],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            # 输出格式: "UUID: 1A2B3C4D-5E6F-7890-ABCD-EF1234567890 (arm64) /path/to/binary"
            for line in result.stdout.strip().splitlines():
                m = re.match(r"UUID:\s+([0-9A-Fa-f-]+)", line)
                if m:
                    return m.group(1)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("dwarfdump --uuid 失败: %s", exc)
    return None


def extract_app_uuid_from_device(
    device_udid: str,
    bundle_id: str,
) -> Optional[str]:
    """从真机上的 App 提取二进制 UUID (用于 dSYM 匹配)。

    通过 devicectl 获取 App 信息，再用 dwarfdump 提取 UUID。
    需要 App 已安装并在设备上。

    Args:
        device_udid: 设备 UDID
        bundle_id: App Bundle ID

    Returns:
        UUID 字符串，失败返回 None
    """
    try:
        # 使用 devicectl 获取 App 信息
        proc = subprocess.run(
            ["xcrun", "devicectl", "device", "info", "apps",
             "--device", device_udid],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return None

        # 解析输出找到 App 路径 (需要进一步处理)
        # 这里使用另一种方法: 通过 ideviceinstaller 获取
        proc2 = subprocess.run(
            ["ideviceinstaller", "-l", "-o", "xml"],
            capture_output=True, timeout=15,
        )
        if proc2.returncode != 0:
            return None

        # 使用更直接的方法: 从 crash report 或 sysdiagnose 中获取 UUID
        # 这里提供一个占位实现，实际使用时需要设备上的 crash log
        logger.debug("extract_app_uuid_from_device 需要设备上存在 crash log 或 sysdiagnose")
        return None

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("extract_app_uuid_from_device 失败: %s", exc)
    return None


def find_dsym_by_uuid(uuid: str) -> Optional[Path]:
    """用 Spotlight (mdfind) 按 dSYM UUID 搜索。

    Args:
        uuid: dwarfdump 报告的 UUID (如 '1A2B3C4D-5E6F-7890-ABCD-EF1234567890')

    Returns:
        dSYM 路径，找不到返回 None
    """
    # 格式化 UUID (去掉破折号也可搜索)
    clean_uuid = uuid.upper().replace("-", "")

    # 尝试标准 UUID 格式
    formatted = "-".join([
        clean_uuid[0:8], clean_uuid[8:12], clean_uuid[12:16],
        clean_uuid[16:20], clean_uuid[20:32],
    ])

    queries = [
        f"com_apple_xcode_dsym_uuids == {formatted}",
        f"com_apple_xcode_dsym_uuids == {uuid}",
        f"kMDItemContentType == 'com.apple.xcode.dsym' && "
        f"com_apple_xcode_dsym_uuids == {formatted}",
    ]

    for query in queries:
        try:
            result = subprocess.run(
                ["mdfind", query],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    path = Path(line.strip())
                    # mdfind 可能返回 dSYM 内部的文件，取 .dSYM 目录
                    dsym = _find_containing_dsym(path)
                    if dsym and dsym.is_dir():
                        logger.info("Spotlight 找到 dSYM: %s (uuid=%s)", dsym, uuid)
                        return dsym
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("mdfind 搜索失败: %s", exc)
            continue

    logger.info("Spotlight 未找到 dSYM: uuid=%s", uuid)
    return None


def _find_containing_dsym(path: Path) -> Optional[Path]:
    """从路径向上查找 .dSYM 目录。"""
    current = path
    for _ in range(10):  # 最多向上 10 层
        if current.suffix == ".dSYM" and current.is_dir():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


# ── dSYM 缓存 ──


def cache_dsym_map(
    dsym_path: Path,
    binary_name: str,
) -> Dict[str, str]:
    """用 dsymutil --string-addresses 预缓存地址→符号映射。

    缓存文件写入 ~/.claude-parallel/symbol_cache/{binary_name}.json

    Args:
        dsym_path: .dSYM 目录路径
        binary_name: 二进制名 (用于缓存文件名)

    Returns:
        地址→符号映射字典
    """
    _ensure_cache_dir()
    cache_file = _cache_path(binary_name)

    # 已有缓存则直接加载
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if isinstance(cached, dict) and cached:
                logger.info(
                    "加载已有缓存: %s (%d 条目)",
                    cache_file, len(cached),
                )
                return cached
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("缓存读取失败，重建: %s", exc)

    # 找到 dSYM 内的实际二进制
    binary_in_dsym = _find_binary_in_dsym(dsym_path)
    if not binary_in_dsym:
        logger.warning("dSYM 内未找到二进制: %s", dsym_path)
        return {}

    # 调用 dsymutil --string-addresses
    addr_map: Dict[str, str] = {}
    try:
        result = subprocess.run(
            ["dsymutil", "--string-addresses", str(binary_in_dsym)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                # dsymutil 输出格式: '0x1a2b3c4d: symbol_name'
                if ":" in line:
                    addr_str, _, sym = line.partition(":")
                    addr = addr_str.strip()
                    sym = sym.strip()
                    if addr.startswith("0x") and sym:
                        addr_map[addr] = sym
        else:
            logger.warning(
                "dsymutil --string-addresses 失败 (rc=%d): %s",
                result.returncode, result.stderr[:200],
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("dsymutil 执行失败: %s", exc)

    # 写缓存
    if addr_map:
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(addr_map, f, indent=2)
            logger.info(
                "缓存写入: %s (%d 条目)", cache_file, len(addr_map),
            )
        except OSError as exc:
            logger.warning("缓存写入失败: %s", exc)

    return addr_map


def _find_binary_in_dsym(dsym_path: Path) -> Optional[Path]:
    """在 .dSYM 包内找到实际的 DWARF 二进制文件。"""
    # 标准路径: Foo.app.dSYM/Contents/Resources/DWARF/Foo
    dwarf_dir = dsym_path / "Contents" / "Resources" / "DWARF"
    if dwarf_dir.is_dir():
        for entry in dwarf_dir.iterdir():
            if entry.is_file():
                return entry

    # 宽松搜索
    for entry in dsym_path.rglob("*"):
        if entry.is_file() and not entry.name.startswith("."):
            # 跳过 plist 等非二进制
            if entry.suffix not in (".plist", ".txt", ".md"):
                return entry

    return None


def load_cached_map(binary_name: str) -> Dict[str, str]:
    """加载已有缓存（不重建）。"""
    cache_file = _cache_path(binary_name)
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


# ── atos 符号化 ──


def symbolicate_addresses(
    binary_name: str,
    addresses: List[str],
    dsym_path: Path,
    arch: str = "arm64",
    load_addr: str = "0x0",
) -> Dict[str, str]:
    """调用 atos 批量符号化地址。

    格式: atos -arch arm64 -o <binary> -l <load_addr> <addr1> <addr2> ...
    一次最多 500 个地址。

    Args:
        binary_name: 二进制文件名
        addresses: 十六进制地址列表 ['0x1a2b', ...]
        dsym_path: .dSYM 目录路径
        arch: 目标架构 (默认 'arm64')
        load_addr: 加载地址 (默认 '0x0')

    Returns:
        地址→符号名映射 {'0x1a2b': 'MyModule.function + 45', ...}
    """
    if not addresses:
        return {}

    binary_in_dsym = _find_binary_in_dsym(dsym_path)
    if not binary_in_dsym:
        logger.warning("dSYM 内未找到二进制: %s", dsym_path)
        return {}

    # 先尝试缓存
    cached = load_cached_map(binary_name)
    remaining = [a for a in addresses if a not in cached]

    result: Dict[str, str] = {}
    # 命中缓存的直接用
    for addr in addresses:
        if addr in cached:
            result[addr] = cached[addr]

    if not remaining:
        logger.info("全部命中缓存 (%d 地址)", len(addresses))
        return result

    # 分批调用 atos
    for batch_start in range(0, len(remaining), ATOS_BATCH_SIZE):
        batch = remaining[batch_start:batch_start + ATOS_BATCH_SIZE]
        batch_result = _atos_batch(binary_in_dsym, batch, arch, load_addr)
        result.update(batch_result)

    return result


def _atos_batch(
    binary_path: Path,
    addresses: List[str],
    arch: str,
    load_addr: str,
) -> Dict[str, str]:
    """单次 atos 调用。"""
    cmd = [
        "atos",
        "-arch", arch,
        "-o", str(binary_path),
        "-l", load_addr,
    ] + addresses

    logger.debug("atos 调用: %d 地址", len(addresses))

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        logger.warning("atos 超时 (%d 地址)", len(addresses))
        return {}
    except OSError as exc:
        logger.warning("atos 执行失败: %s", exc)
        return {}

    if proc.returncode != 0:
        logger.warning(
            "atos 失败 (rc=%d): %s",
            proc.returncode, proc.stderr[:200],
        )
        return {}

    # 解析输出 — atos 每行一个结果
    lines = proc.stdout.strip().splitlines()
    result: Dict[str, str] = {}
    for addr, line in zip(addresses, lines):
        line = line.strip()
        if line and line != addr:
            # atos 输出可能包含地址前缀，去掉
            # 如 'MyModule.function (in MyApp) (MyFile.swift:42)'
            result[addr] = line
        else:
            # atos 无法解析时原样返回地址
            result[addr] = addr

    return result


# ── Swift demangle ──


# 常见 Swift mangled name 正则 ($s 前缀)
_SWIFT_MANGLED_RE = re.compile(
    r"\$s[a-zA-Z0-9_]+(?:[a-zA-Z0-9_]|\.)*"
)

# Swift mangling 特征字符
_SWIFT_MANGLING_CHARS = re.compile(
    r"[0-9]+[a-zA-Z_]|fI|fP|Ma|fC|fE|O\d|C\d|V\d|"
    r"21unknownListGuaranteed|yy[a-zA-Z]*Y[a-zA-Z]*c|"
    r"7SwiftUI|So[a-zA-Z0-9]+C|S[a-q]\w*"
)


def swift_demangle(name: str) -> str:
    """将 Swift mangled name 还原为可读形式。

    优先调用 `swift demangle` 命令行工具；
    如果不可用则用正则替换常见模式。

    Args:
        name: 可能是 Swift mangled name 的字符串
            如 '$s7MyApp14ViewControllerC4load7requestyAA7RequestV_tF'

    Returns:
        还原后的可读符号名
    """
    if not name or "$s" not in name:
        return name

    # 尝试 swift demangle 命令
    try:
        proc = subprocess.run(
            ["swift", "demangle", "--simplified", name],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            demangled = proc.stdout.strip()
            # swift demangle 失败时原样返回
            if demangled and demangled != name and "$s" not in demangled:
                return demangled
    except (subprocess.TimeoutExpired, OSError):
        pass

    # 回退到正则替换
    return _swift_demangle_regex(name)


def _swift_demangle_regex(name: str) -> str:
    """正则方式简化 Swift mangled name。

    能处理的模式:
    - $s<数字><模块名><数字><类名>C — 类
    - $s<数字><模块名><数字><类名>C<数字><方法名><签名> — 方法
    """
    result = name

    # 简单替换: 去掉 $s 前缀
    if result.startswith("$s"):
        result = result[2:]

    # 模块名: 开头的数字表示长度
    result = re.sub(
        r"^(\d+)([a-zA-Z_]\w*)",
        lambda m: f"{m.group(2)}.",
        result,
    )

    # 类型名: 数字+标识符+C (class)
    result = re.sub(
        r"(\d+)([a-zA-Z_]\w*)(C|O|V|P)",
        lambda m: f"{m.group(2)}",
        result,
    )

    # 方法名: 数字+标识符
    result = re.sub(
        r"(\d+)([a-zA-Z_]\w*)",
        lambda m: f".{m.group(2)}",
        result,
    )

    # 清理连续多点
    result = re.sub(r"\.{2,}", ".", result)
    result = result.strip(".")

    # 清理残余 mangling 标记
    result = re.sub(r"^[yf]\w*", "", result)
    result = result.strip(".")

    return result if result else name


# ── 热点符号化 ──


def symbolicate_hotspots(
    hotspots_list: List[Tuple[str, float]],
    dsym_paths: Optional[Dict[str, Path]] = None,
    arch: str = "arm64",
) -> List[Tuple[str, float]]:
    """对 parse_timeprofiler_xml 返回的热点列表批量符号化。

    自动筛选未符号化的地址（0x 开头），按二进制分组，
    调用 atos 批量解析后替换原始地址。

    Args:
        hotspots_list: [(symbol, weight), ...] 列表
        dsym_paths: {binary_name: dsym_path} 映射。
            若为 None，尝试用第一个可用的缓存。
        arch: 目标架构

    Returns:
        符号化后的 [(symbol, weight), ...] 列表（保持原顺序）
    """
    if not hotspots_list:
        return []

    # 收集需要符号化的条目
    needs_work: List[Tuple[int, str, str, float]] = []  # (index, addr, symbol, weight)
    all_addresses: List[str] = []

    for idx, (symbol, weight) in enumerate(hotspots_list):
        if _is_unsymbolicated(symbol):
            addr = _extract_address(symbol)
            if addr:
                needs_work.append((idx, addr, symbol, weight))
                all_addresses.append(addr)

    if not needs_work:
        return list(hotspots_list)

    logger.info(
        "符号化: %d/%d 个地址需要解析",
        len(needs_work), len(hotspots_list),
    )

    # 确定 dsym 路径
    # 如果只有一个 dsym，所有地址都用它
    resolved_dsym: Optional[Path] = None
    binary_name = "unknown"

    if dsym_paths:
        # 取第一个可用的 dsym
        for bname, dpath in dsym_paths.items():
            if dpath and dpath.is_dir():
                resolved_dsym = dpath
                binary_name = bname
                break

    # 尝试加载缓存（即使没有 dsym_paths 也可能命中）
    cached = load_cached_map(binary_name)

    # 先看缓存能解决多少
    addr_to_sym: Dict[str, str] = {}
    remaining_addrs = []
    for idx, addr, symbol, weight in needs_work:
        if addr in cached:
            addr_to_sym[addr] = cached[addr]
        else:
            remaining_addrs.append(addr)

    # 有 dsym 且还有剩余地址，调用 atos
    if remaining_addrs and resolved_dsym:
        atos_result = symbolicate_addresses(
            binary_name=binary_name,
            addresses=list(dict.fromkeys(remaining_addrs)),  # 去重保序
            dsym_path=resolved_dsym,
            arch=arch,
        )
        addr_to_sym.update(atos_result)

    # 构建结果
    result = list(hotspots_list)
    for idx, addr, original_symbol, weight in needs_work:
        sym = addr_to_sym.get(addr, original_symbol)
        # Swift demangle
        if "$s" in sym:
            sym = swift_demangle(sym)
        result[idx] = (sym, weight)

    resolved_count = sum(
        1 for idx, addr, _, _ in needs_work
        if addr_to_sym.get(addr, "").replace(addr, "") != ""
    )
    logger.info(
        "符号化完成: %d/%d 成功解析",
        resolved_count, len(needs_work),
    )

    return result


# ── 批量入口 ──


def auto_symbolicate(
    hotspots_list: List[Tuple[str, float]],
    app_bundle_id: str = "",
    uuid: str = "",
    build_dir: str = "",
    app_name: str = "",
    device_udid: str = "",
    asc_api_key_id: str = "",
    asc_issuer_id: str = "",
    asc_private_key_path: str = "",
    asc_app_id: str = "",
    arch: str = "arm64",
) -> List[Tuple[str, float]]:
    """全自动符号化入口。

    搜索策略 (按优先级):
    1. 显式 build_dir → DerivedData
    2. Xcode Archives (Release 包)
    3. Spotlight UUID 搜索
    4. App Store Connect 下载 (需 API Key)

    Args:
        hotspots_list: [(symbol, weight), ...]
        app_bundle_id: App Bundle ID (用于 DerivedData/Archive 查找)
        uuid: dSYM UUID (用于 Spotlight 查找)
        build_dir: 显式 build 目录
        app_name: App 名 (加速 Archive 搜索)
        device_udid: 设备 UDID (用于从设备提取 UUID)
        asc_api_key_id: App Store Connect API Key ID
        asc_issuer_id: App Store Connect API Issuer ID
        asc_private_key_path: API 私钥路径
        asc_app_id: Apple App ID
        arch: 目标架构

    Returns:
        符号化后的列表
    """
    dsym_paths: Dict[str, Path] = {}

    # 策略 1: 显式 build 目录 / DerivedData
    dsym = None
    if build_dir:
        dsym = find_dsym(app_bundle_id, build_dir)
    if not dsym and app_bundle_id:
        dsym = find_dsym(app_bundle_id)

    # 策略 2: Xcode Archives (Release 包最可靠)
    if not dsym and app_bundle_id:
        dsym = find_dsym_in_archives(
            app_bundle_id=app_bundle_id,
            app_name=app_name,
        )

    # 策略 3: Spotlight UUID 搜索
    if not dsym and uuid:
        dsym = find_dsym_by_uuid(uuid)

    # 策略 4: 如果没有 UUID 但有设备，尝试提取 UUID
    if not dsym and not uuid and device_udid and app_bundle_id:
        extracted_uuid = extract_app_uuid_from_device(device_udid, app_bundle_id)
        if extracted_uuid:
            dsym = find_dsym_by_uuid(extracted_uuid)

    # 策略 5: App Store Connect 下载
    if not dsym and app_bundle_id:
        dsym = find_dsym_app_store_connect(
            bundle_id=app_bundle_id,
            app_id=asc_app_id,
            api_key_id=asc_api_key_id,
            issuer_id=asc_issuer_id,
            private_key_path=asc_private_key_path,
        )

    if dsym:
        # 尝试从 dSYM 推断 binary name
        binary_in = _find_binary_in_dsym(dsym)
        bname = binary_in.name if binary_in else "unknown"
        dsym_paths[bname] = dsym

        # 预缓存
        cache_dsym_map(dsym, bname)

    return symbolicate_hotspots(hotspots_list, dsym_paths, arch=arch)
