"""独立 Java 渲染器封装。

渲染引擎来自开源项目 koishi-plugin-litematic-renderer（MIT）的
``litematic-standalone-renderer``：一个不依赖 Minecraft 客户端、直接读取
Litematica 投影 + 原版资源包（blockstate/模型/纹理）的软件渲染器，
输出带真实材质、方块模型（台阶/楼梯/栏杆/屋顶等）和光照的等距预览图。

本模块只做三件事：
1. 定位 Java（Java 21+）与随插件附带的 jar / 原版资源包；
2. 用 subprocess 调用渲染器（含超时、内存不足自动加大堆重试、进程清理）；
3. 结果缓存（按文件内容 SHA-256 + 渲染参数 + 资源指纹），避免重复渲染。

注意：Java 渲染器只接受 ``.litematic`` 文件；其它格式由插件先用
``litematic_writer`` 转换后再调用本模块。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict, replace
from typing import Any

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(PLUGIN_DIR, "assets")
DEFAULT_JAR = os.path.join(
    ASSETS_DIR, "standalone-renderer", "litematic-standalone-renderer-0.2.8.jar"
)
DEFAULT_RESOURCES = os.path.join(
    ASSETS_DIR, "vanilla-resources", "Minecraft-26.2-Vanilla-Resources.zip"
)
BUILTIN_RESOURCEPACKS_DIR = os.path.join(ASSETS_DIR, "resourcepacks")
MIN_JAVA_MAJOR = 21
_RENDERER_VERSION = "0.2.8"

# Windows 上不弹出控制台窗口
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_JAVA_VERSION_RE = re.compile(r'(?:java|openjdk)\s+version\s+"(\d+)(?:[.\d]*)?"', re.IGNORECASE)
_MEMORY_ERROR_RE = re.compile(
    r"outofmemoryerror|java heap space|gc overhead limit|cannot reserve enough space"
    r"|native memory allocation|insufficient memory|malloc failed|os::commit_memory",
    re.IGNORECASE,
)
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


class JavaRendererError(Exception):
    """Java 渲染失败（Java 缺失、资源缺失、渲染器返回非 0、超时等），消息可直接展示给用户。"""


@dataclass
class JavaRenderOptions:
    """Java 渲染参数（由插件配置注入）。"""

    output_size: int = 1024
    supersampling: int = 1
    rotation: float = 135.0
    slant: float = 36.0
    fill: float = 0.85
    background: str = "#000000"
    transparent_background: bool = True
    heap_mb: int = 512
    timeout: float = 60.0
    resource_packs: tuple[str, ...] = ()
    send_both_views: bool = False
    java_path: str = ""
    jar_path: str = ""
    resources_path: str = ""
    cache_dir: str = ""
    cache_max_mb: int = 300

    def normalized(self) -> "JavaRenderOptions":
        """把参数夹到渲染器支持的范围内，避免坏配置直接炸进程。"""
        bg = str(self.background or "#000000").strip()
        if not _HEX_COLOR_RE.match(bg):
            bg = "#000000"
        return JavaRenderOptions(
            output_size=max(256, min(8192, int(self.output_size))),
            supersampling=max(1, min(4, int(self.supersampling))),
            rotation=float(self.rotation) % 360.0,
            slant=max(-89.0, min(89.0, float(self.slant))),
            fill=max(0.10, min(1.0, float(self.fill))),
            background=bg,
            transparent_background=bool(self.transparent_background),
            heap_mb=max(128, min(32768, int(self.heap_mb))),
            timeout=max(10.0, min(1800.0, float(self.timeout))),
            resource_packs=tuple(str(p) for p in (self.resource_packs or ()) if str(p).strip()),
            send_both_views=bool(self.send_both_views),
            java_path=str(self.java_path or ""),
            jar_path=str(self.jar_path or ""),
            resources_path=str(self.resources_path or ""),
            cache_dir=str(self.cache_dir or ""),
            cache_max_mb=max(0, int(self.cache_max_mb)),
        )

    def fingerprint(self) -> dict[str, Any]:
        """参与缓存 key 的渲染参数（不含超时、缓存目录等运行参数）。"""
        return {
            "renderer": _RENDERER_VERSION,
            "size": self.output_size,
            "ss": self.supersampling,
            "rotation": round(float(self.rotation), 3),
            "slant": round(float(self.slant), 3),
            "fill": round(float(self.fill), 4),
            "background": self.background,
            "transparent": self.transparent_background,
            "packs": [
                (
                    os.path.basename(p),
                    os.path.getsize(p) if os.path.isfile(p) else 0,
                    int(os.path.getmtime(p)) if os.path.isfile(p) else 0,
                )
                for p in self.resource_packs
            ],
        }


@dataclass
class JavaRenderResult:
    images: dict[str, bytes]  # name -> PNG bytes；至少包含 isometric
    diagnostics: dict = field(default_factory=dict)
    elapsed: float = 0.0
    cached: bool = False
    java_path: str = ""
    java_major: int = 0
    heap_mb: int = 0
    supersampling: int = 1
    notes: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""

    def image_names(self, both_views: bool = False) -> list[str]:
        names = ["isometric"]
        if both_views and "isometric-reverse" in self.images:
            names.append("isometric-reverse")
        return [n for n in names if n in self.images]

    @property
    def unsupported_blocks(self) -> int:
        try:
            return int(self.diagnostics.get("unsupportedBlockCount") or 0)
        except (TypeError, ValueError):
            return 0


# --------------------------------------------------------------------------- #
# Java 定位
# --------------------------------------------------------------------------- #

_JAVA_CACHE: dict[str, tuple[str | None, int]] = {}
_JAVA_VERSION_CACHE: dict[str, int | None] = {}


def _decode_output(data: bytes) -> str:
    """Java 在中文 Windows 上默认输出 GBK，这里做容错解码。"""
    if not data:
        return ""
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def java_major_version(java_cmd: str) -> int | None:
    """执行 ``java -version`` 并解析主版本号（失败返回 None）。"""
    key = os.path.normcase(java_cmd)
    if key in _JAVA_VERSION_CACHE:
        return _JAVA_VERSION_CACHE[key]
    try:
        proc = subprocess.run(
            [java_cmd, "-version"],
            capture_output=True,
            timeout=15,
            creationflags=_CREATE_NO_WINDOW,
        )
        text = _decode_output(proc.stdout + proc.stderr)
        match = _JAVA_VERSION_RE.search(text)
        version = int(match.group(1)) if match else None
    except Exception:
        version = None
    _JAVA_VERSION_CACHE[key] = version
    return version


def _iter_java_candidates(configured: str = ""):
    """按优先级产出可能的 Java 可执行文件路径。"""
    exe = "java.exe" if os.name == "nt" else "java"
    seen: set[str] = set()

    def push(path: str | None):
        if not path:
            return
        norm = os.path.normcase(os.path.abspath(path))
        if norm not in seen:
            seen.add(norm)
            yield path

    if configured and configured.strip():
        path = configured.strip().strip('"')
        if os.path.isdir(path):
            path = os.path.join(path, "bin", exe)
        yield from push(path)

    java_home = os.environ.get("JAVA_HOME", "")
    if java_home:
        yield from push(os.path.join(java_home, "bin", exe))

    which = shutil.which("java")
    if which:
        yield from push(which)

    if os.name == "nt":
        program_files = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramW6432", r"C:\Program Files"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        roots: list[str] = []
        for base in program_files:
            if not base:
                continue
            roots.extend([
                os.path.join(base, "Java"),
                os.path.join(base, "Eclipse Adoptium"),
                os.path.join(base, "Microsoft", "jdk"),
                os.path.join(base, "Zulu"),
                os.path.join(base, "BellSoft"),
                os.path.join(base, "Programs", "Eclipse Adoptium"),
            ])
        for root in roots:
            if not os.path.isdir(root):
                continue
            try:
                entries = sorted(os.listdir(root), reverse=True)
            except OSError:
                continue
            for name in entries:
                candidate = os.path.join(root, name, "bin", exe)
                if os.path.isfile(candidate):
                    yield from push(candidate)
        # Minecraft 自带运行时（官方启动器 runtime/ 下）
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            import glob

            patterns = [
                os.path.join(appdata, ".minecraft", "runtime", "*", "*", "*", "bin", exe),
                os.path.join(appdata, ".minecraft", "runtime", "*", "*", "bin", exe),
                os.path.join(appdata, ".minecraft", "runtime", "*", "bin", exe),
            ]
            for pattern in patterns:
                for candidate in sorted(glob.glob(pattern), reverse=True):
                    yield from push(candidate)

    yield from push(exe)  # 最后交给 PATH 解析（可能是 shell 别名）


def find_java(configured: str = "", *, refresh: bool = False) -> tuple[str, int]:
    """找到可用的 Java 21+，返回 (路径, 主版本号)。找不到抛 JavaRendererError。"""
    cache_key = (configured or "").strip()
    if not refresh and cache_key in _JAVA_CACHE:
        cached_path, cached_major = _JAVA_CACHE[cache_key]
        if cached_path and cached_major >= MIN_JAVA_MAJOR:
            return cached_path, cached_major

    tried: list[str] = []
    for candidate in _iter_java_candidates(configured):
        major = java_major_version(candidate)
        if major is None:
            continue
        tried.append(f"{candidate} (Java {major})")
        if major >= MIN_JAVA_MAJOR:
            _JAVA_CACHE[cache_key] = (candidate, major)
            return candidate, major
    _JAVA_CACHE[cache_key] = (None, 0)
    detail = "；".join(tried[:6]) or "PATH / JAVA_HOME / Program Files 中均未找到 java"
    raise JavaRendererError(
        f"未找到 Java {MIN_JAVA_MAJOR}+（当前已检查：{detail}）。"
        f"请安装 Java {MIN_JAVA_MAJOR} 或更高版本，或在插件配置里填写 java_path。"
    )


# --------------------------------------------------------------------------- #
# 资源定位 / 命令构建
# --------------------------------------------------------------------------- #


def resolve_assets(opts: JavaRenderOptions) -> tuple[str, str]:
    """返回 (渲染器 jar 路径, 原版资源包路径)，缺失时抛 JavaRendererError。"""
    jar = (opts.jar_path or "").strip() or DEFAULT_JAR
    resources = (opts.resources_path or "").strip() or DEFAULT_RESOURCES
    if not os.path.isfile(jar):
        raise JavaRendererError(
            f"找不到 Java 渲染器 jar：{jar}（插件 assets/standalone-renderer/ 下应包含该文件）"
        )
    if not os.path.isfile(resources):
        raise JavaRendererError(
            f"找不到原版资源包：{resources}。请把自己 Minecraft 的原版资源包路径填到 "
            f"java_resources_path，或把资源包 zip 放到插件数据目录 resources/ 下；"
            f"发布版不内置 Mojang 原版资源，可用 tools/build_vanilla_resource_pack.py 生成。"
        )
    return jar, resources


def build_render_command(
    java_cmd: str,
    jar: str,
    resources: str,
    input_path: str,
    output_dir: str,
    opts: JavaRenderOptions,
    *,
    heap_mb: int | None = None,
    extra_packs: list[str] | None = None,
    supersampling: int | None = None,
) -> list[str]:
    """构建 Java 渲染命令（独立函数，便于单元测试）。"""
    o = opts.normalized()
    heap = max(128, min(32768, int(heap_mb if heap_mb is not None else o.heap_mb)))
    cmd: list[str] = [
        java_cmd,
        "-Xms128m",
        f"-Xmx{heap}m",
        "-XX:+UseG1GC",
        "-XX:+UseStringDeduplication",
        "-XX:+ExitOnOutOfMemoryError",
        "-Djava.awt.headless=true",
        "-jar",
        jar,
        "--input",
        os.path.abspath(input_path),
        "--output",
        os.path.abspath(output_dir),
        "--minecraft-jar",
        os.path.abspath(resources),
        "--resolution",
        str(o.output_size),
        "--supersampling",
        str(supersampling if supersampling is not None else o.supersampling),
        "--rotation",
        f"{o.rotation:g}",
        "--slant",
        f"{o.slant:g}",
        "--fill",
        f"{o.fill:g}",
        "--background",
        o.background,
    ]
    if o.transparent_background:
        cmd.append("--transparent-background")
    for pack in (extra_packs if extra_packs is not None else o.resource_packs):
        cmd.extend(["--resource-pack", os.path.abspath(pack)])
    return cmd


# --------------------------------------------------------------------------- #
# 缓存
# --------------------------------------------------------------------------- #


def _asset_fingerprint(jar: str, resources: str) -> str:
    parts = []
    for path in (jar, resources):
        try:
            stat = os.stat(path)
            parts.append(f"{os.path.basename(path)}:{stat.st_size}:{int(stat.st_mtime)}")
        except OSError:
            parts.append(f"{os.path.basename(path)}:missing")
    return "|".join(parts)


def cache_key(data: bytes, opts: JavaRenderOptions, jar: str, resources: str) -> str:
    payload = json.dumps(
        {"opts": opts.fingerprint(), "assets": _asset_fingerprint(jar, resources)},
        sort_keys=True,
        ensure_ascii=False,
    )
    digest = hashlib.sha256()
    digest.update(data)
    digest.update(payload.encode("utf-8"))
    return digest.hexdigest()[:40]


def _cache_read(cache_dir: str, key: str) -> JavaRenderResult | None:
    entry = os.path.join(cache_dir, key)
    info_path = os.path.join(entry, "info.json")
    iso_path = os.path.join(entry, "isometric.png")
    if not (os.path.isfile(info_path) and os.path.isfile(iso_path)):
        return None
    try:
        with open(info_path, "r", encoding="utf-8") as f:
            info = json.load(f)
        images: dict[str, bytes] = {}
        for name in ("isometric", "isometric-reverse"):
            path = os.path.join(entry, f"{name}.png")
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    images[name] = f.read()
        if "isometric" not in images:
            return None
        return JavaRenderResult(
            images=images,
            diagnostics=info.get("diagnostics") or {},
            elapsed=float(info.get("elapsed") or 0.0),
            cached=True,
            java_path=str(info.get("java_path") or ""),
            java_major=int(info.get("java_major") or 0),
            heap_mb=int(info.get("heap_mb") or 0),
            supersampling=int(info.get("supersampling") or 1),
            notes=list(info.get("notes") or []),
        )
    except Exception:
        return None


def _cache_write(cache_dir: str, key: str, result: JavaRenderResult) -> None:
    if not cache_dir:
        return
    entry = os.path.join(cache_dir, key)
    try:
        os.makedirs(entry, exist_ok=True)
        for name, png in result.images.items():
            tmp = os.path.join(entry, f".{name}.{uuid.uuid4().hex}.tmp")
            with open(tmp, "wb") as f:
                f.write(png)
            os.replace(tmp, os.path.join(entry, f"{name}.png"))
        info = {
            "diagnostics": result.diagnostics,
            "elapsed": round(result.elapsed, 3),
            "java_path": result.java_path,
            "java_major": result.java_major,
            "heap_mb": result.heap_mb,
            "supersampling": result.supersampling,
            "notes": result.notes,
            "created_at": time.time(),
        }
        tmp = os.path.join(entry, f".info.{uuid.uuid4().hex}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(entry, "info.json"))
    except OSError:
        pass


def _cache_evict(cache_dir: str, max_mb: int) -> None:
    if not cache_dir or max_mb <= 0 or not os.path.isdir(cache_dir):
        return
    entries: list[tuple[float, int, str]] = []
    total = 0
    try:
        for name in os.listdir(cache_dir):
            path = os.path.join(cache_dir, name)
            if not os.path.isdir(path):
                continue
            size = 0
            mtime = 0.0
            try:
                for fname in os.listdir(path):
                    fpath = os.path.join(path, fname)
                    if os.path.isfile(fpath):
                        stat = os.stat(fpath)
                        size += stat.st_size
                        mtime = max(mtime, stat.st_mtime)
            except OSError:
                continue
            total += size
            entries.append((mtime, size, path))
    except OSError:
        return
    limit = max_mb * 1024 * 1024
    if total <= limit:
        return
    entries.sort(key=lambda item: item[0])
    target = int(limit * 0.8)
    for mtime, size, path in entries:
        if total <= target:
            break
        shutil.rmtree(path, ignore_errors=True)
        total -= size


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #


def probe(opts: JavaRenderOptions | None = None) -> dict[str, Any]:
    """检查 Java 渲染器是否可用，返回诊断信息（不抛异常）。"""
    opts = (opts or JavaRenderOptions()).normalized()
    info: dict[str, Any] = {
        "available": False,
        "java_path": "",
        "java_major": 0,
        "jar": "",
        "resources": "",
        "min_java": MIN_JAVA_MAJOR,
        "error": "",
    }
    try:
        jar, resources = resolve_assets(opts)
        info["jar"] = jar
        info["resources"] = resources
    except JavaRendererError as exc:
        info["error"] = str(exc)
        return info
    try:
        java_cmd, major = find_java(opts.java_path)
        info["java_path"] = java_cmd
        info["java_major"] = major
        info["available"] = True
    except JavaRendererError as exc:
        info["error"] = str(exc)
    return info


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """强制结束 Java 进程树。

    Windows 上 PATH 里的 ``java.exe`` 往往是 Oracle 的 javapath 启动器，
    真正的 JVM 是它的子进程；只 kill 父进程会留下孤儿 JVM，必须整棵树杀。
    """
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=20,
                creationflags=_CREATE_NO_WINDOW,
            )
        else:  # pragma: no cover - 非 Windows 环境
            import signal

            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=15)
    except Exception:
        pass


def _run_java(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "creationflags": _CREATE_NO_WINDOW,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except FileNotFoundError as exc:
        raise JavaRendererError(f"无法启动 Java：{exc}") from exc
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc.returncode or 0, _decode_output(stdout), _decode_output(stderr)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            proc.communicate(timeout=10)
        except Exception:
            pass
        raise JavaRendererError(
            f"渲染超时（>{timeout:.0f}s），已终止 Java 进程树。"
            f"投影可能过大，可调大 java_render_timeout_seconds 或裁剪投影。"
        ) from None


_RAM_CACHE: int | None = None


def builtin_resource_packs() -> list[str]:
    """列出插件内置的材质包（assets/resourcepacks/*.zip，按文件名排序）。"""
    try:
        names = sorted(os.listdir(BUILTIN_RESOURCEPACKS_DIR))
    except OSError:
        return []
    return [
        os.path.join(BUILTIN_RESOURCEPACKS_DIR, name)
        for name in names
        if name.lower().endswith(".zip")
        and os.path.isfile(os.path.join(BUILTIN_RESOURCEPACKS_DIR, name))
    ]


def unsupported_summary(diagnostics: dict | None, limit: int = 5) -> tuple[int, str]:
    """从渲染诊断信息里提取缺失模型/材质的方块。

    返回 ``(数量, 示例文本)``；示例文本形如
    ``minecraft:heavy_core[waterlogged=false] × 1、...``。
    """
    if not diagnostics:
        return 0, ""
    total = 0
    try:
        total = int(diagnostics.get("unsupportedBlockCount") or 0)
    except (TypeError, ValueError):
        total = 0
    merged: dict[str, int] = {}
    for item in diagnostics.get("blocks") or []:
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or item.get("block") or "?")
        try:
            count = int(item.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        merged[state] = merged.get(state, 0) + count
    if merged:
        total = sum(merged.values())
    ordered = sorted(merged.items(), key=lambda pair: (-pair[1], pair[0]))
    parts = [f"{state} × {count:,}" for state, count in ordered[: max(1, limit)]]
    if len(ordered) > limit:
        parts.append(f"等 {len(ordered)} 种")
    return total, "、".join(parts)


def _total_ram_mb() -> int:
    """获取本机物理内存（MB），失败返回 0。仅用标准库/ctypes。"""
    global _RAM_CACHE
    if _RAM_CACHE is not None:
        return _RAM_CACHE
    total = 0
    try:
        if os.name == "nt":
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                total = int(status.ullTotalPhys / 1024 / 1024)
        else:  # pragma: no cover - 非 Windows
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total = int(line.split()[1]) // 1024
                        break
    except Exception:
        total = 0
    _RAM_CACHE = total
    return total


def _heap_cap_mb() -> int:
    """JVM 堆的安全上限：本机内存的 50%（最少 1024MB，最多 16GB）。"""
    total = _total_ram_mb()
    if total <= 0:
        return 12288
    return max(1024, min(16384, int(total * 0.50)))


def _predicted_heap_mb(output_size: int, supersampling: int = 1, data_size: int = 0) -> int:
    """按「输出分辨率 × 超采样」的像素量 + 投影文件大小估算需要的最小堆。"""
    ss = max(1, min(8, int(supersampling)))
    pixels = (max(256, int(output_size)) * ss) ** 2
    estimate = 256 + int(pixels / 1_000_000 * 28)
    if data_size > 0:
        # 投影文件越大，方块/几何数据越多（压缩比差异大，这里按 24MB/文件MB 粗估，封顶 4GB）
        estimate += min(4096, int(data_size / 1_000_000 * 24))
    return max(512, estimate)


def _effective_supersampling(opts: JavaRenderOptions, data_size: int = 0) -> int:
    """内存不足时自动降低超采样倍率（4→3→2→1），避免直接 OOM。"""
    ss = max(1, min(8, int(opts.supersampling)))
    cap = _heap_cap_mb()
    while ss > 1 and _predicted_heap_mb(opts.output_size, ss, data_size) > cap:
        ss -= 1
    return ss


def _effective_heap_mb(opts: JavaRenderOptions, data_size: int = 0,
                       supersampling: int | None = None) -> int:
    ss = opts.supersampling if supersampling is None else supersampling
    predicted = _predicted_heap_mb(opts.output_size, ss, data_size)
    return max(512, min(_heap_cap_mb(), max(int(opts.heap_mb), predicted)))


def _effective_timeout(opts: JavaRenderOptions, supersampling: int | None = None) -> float:
    """高分辨率/超采样渲染耗时随像素增长，自动放宽超时（用户配置更大时采用用户值）。"""
    ss = opts.supersampling if supersampling is None else supersampling
    pixels = (max(256, int(opts.output_size)) * max(1, int(ss))) ** 2
    estimate = 60.0 + pixels / 1_000_000 * 1.5
    return max(float(opts.timeout), min(1800.0, estimate))


def render_litematic_bytes(
    data: bytes,
    filename: str,
    opts: JavaRenderOptions | None = None,
) -> JavaRenderResult:
    """同步渲染一个 .litematic 文件的字节内容，返回 PNG 字节与诊断信息。

    该函数会阻塞（JVM 启动 + 软件渲染），请放在线程池里调用。
    """
    if not data:
        raise JavaRendererError("文件内容为空，无法渲染。")
    opts = (opts or JavaRenderOptions()).normalized()
    started = time.time()
    jar, resources = resolve_assets(opts)
    java_cmd, java_major = find_java(opts.java_path)

    for pack in opts.resource_packs:
        if not os.path.isfile(pack):
            raise JavaRendererError(f"材质包不存在：{pack}")

    effective_ss = _effective_supersampling(opts, len(data))
    cache_opts = replace(opts, supersampling=effective_ss)
    key = cache_key(data, cache_opts, jar, resources)
    if opts.cache_dir:
        cached = _cache_read(opts.cache_dir, key)
        if cached is not None:
            return cached

    tmp_root = tempfile.mkdtemp(prefix="astrbot_java_render_")
    try:
        input_path = os.path.join(tmp_root, "input.litematic")
        with open(input_path, "wb") as f:
            f.write(data)
        output_dir = os.path.join(tmp_root, "out")
        os.makedirs(output_dir, exist_ok=True)

        heap = _effective_heap_mb(opts, len(data), effective_ss)
        effective_timeout = _effective_timeout(opts, effective_ss)
        heap_cap = _heap_cap_mb()
        max_attempts = 3
        attempt = 0
        tried_heaps: list[int] = []
        last_error = ""
        while True:
            attempt += 1
            tried_heaps.append(heap)
            cmd = build_render_command(
                java_cmd, jar, resources, input_path, output_dir, opts,
                heap_mb=heap, supersampling=effective_ss,
            )
            returncode, stdout, stderr = _run_java(cmd, effective_timeout)
            if returncode == 0:
                break
            combined = f"{stdout}\n{stderr}"
            memory_failure = returncode in (3, 137) or bool(_MEMORY_ERROR_RE.search(combined))
            # 内存不足时自动加大堆重试（最多 3 次，不超过本机内存安全上限）
            if memory_failure and attempt < max_attempts and heap < heap_cap:
                next_heap = min(heap_cap, max(heap + 1024, heap * 2))
                if next_heap > heap:
                    heap = next_heap
                    shutil.rmtree(output_dir, ignore_errors=True)
                    os.makedirs(output_dir, exist_ok=True)
                    continue
            if memory_failure:
                last_error = (
                    f"Java 内存不足：输出 {int(opts.output_size)}px × 超采样 {effective_ss}×，"
                    f"已尝试堆内存 {', '.join(str(h) + 'MB' for h in tried_heaps)}"
                    f"（本机内存约 {_total_ram_mb() or '?'}MB，安全上限 {heap_cap}MB）。"
                    f"请降低 java_output_size / java_quad_view_size / java_supersampling，"
                    f"或调大 java_heap_mb（当前配置 {int(opts.heap_mb)}MB）。"
                )
            else:
                last_error = _pick_error(stderr or stdout, returncode)
            break

        if last_error:
            raise JavaRendererError(last_error)

        images: dict[str, bytes] = {}
        for name in ("isometric", "isometric-reverse"):
            path = os.path.join(output_dir, f"{name}.png")
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    images[name] = f.read()
        if "isometric" not in images:
            raise JavaRendererError("Java 渲染器未生成 isometric.png（输出目录为空或渲染失败）。")

        diagnostics: dict = {}
        diag_path = os.path.join(output_dir, "render-diagnostics.json")
        if os.path.isfile(diag_path):
            try:
                with open(diag_path, "r", encoding="utf-8") as f:
                    diagnostics = json.load(f)
            except Exception:
                diagnostics = {}

        notes: list[str] = []
        if effective_ss < int(opts.supersampling):
            notes.append(
                f"内存限制：超采样已自动从 {int(opts.supersampling)}× 降为 {effective_ss}×"
                f"（本机内存约 {_total_ram_mb() or '?'}MB）"
            )
        result = JavaRenderResult(
            images=images,
            diagnostics=diagnostics,
            elapsed=time.time() - started,
            cached=False,
            java_path=java_cmd,
            java_major=java_major,
            heap_mb=heap,
            supersampling=effective_ss,
            notes=notes,
            stdout=stdout,
            stderr=stderr,
        )
        if opts.cache_dir:
            _cache_write(opts.cache_dir, key, result)
            _cache_evict(opts.cache_dir, opts.cache_max_mb)
        return result
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _pick_error(output: str, returncode: int) -> str:
    """从 Java 输出里抽出最能说明问题的一行。"""
    if output:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        interesting = [
            line
            for line in lines
            if any(tag in line for tag in ("Exception", "Error", "error:", "Caused by", "缺少", "无法", "失败"))
        ]
        if interesting:
            return f"Java 渲染器退出码 {returncode}：{interesting[-1][:300]}"
        tail = lines[-1] if lines else ""
        return f"Java 渲染器退出码 {returncode}：{tail[:300]}"
    return f"Java 渲染器退出码 {returncode}"


def describe(result: JavaRenderResult, both_views: bool = False) -> str:
    """生成一段简短的渲染结果描述（用于日志/回复文案）。"""
    views = len(result.image_names(both_views))
    engine = "缓存命中" if result.cached else f"Java {result.java_major or ''}".strip()
    return (
        f"engine={engine}, views={views}, heap={result.heap_mb}MB, "
        f"unsupported={result.unsupported_blocks}, elapsed={result.elapsed:.2f}s"
    )
