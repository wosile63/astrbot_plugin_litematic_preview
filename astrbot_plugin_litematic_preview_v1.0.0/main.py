"""Minecraft 投影/结构文件预览渲染插件（AstrBot 4.x）。

用户在 QQ 里发送 Minecraft 投影文件（`.litematic` / `.schem` / `.schematic` / `.nbt` /
Axiom 蓝图 `.bp`）并 @机器人 后，插件自动解析文件、渲染成等距（isometric）预览图并回复。

渲染引擎（可在配置中切换）：
1. **Java 真实材质渲染（默认，推荐）**：调用内置的独立 Java 渲染器
   （来自 MIT 项目 koishi-plugin-litematic-renderer），读取原版资源包里的
   blockstate/模型/纹理，输出带真实材质与光照的图片；只接受 `.litematic`，
   其它格式（含 Axiom `.bp`）会先用 ``litematic_writer`` 转成 litematic 再渲染。
2. **简化 Python 渲染（回退）**：纯 numpy + Pillow 的等距方块渲染，
   在没有 Java 21+ / 渲染失败 / 明确配置为 python 时使用。

触发方式（默认）：
1. 群聊：先发/上传文件（SnowLuma 会把这作为一条带 `file` 消息段的普通群消息下发，
   插件先把文件缓存起来），然后在「待渲染窗口」内 @机器人（或引用文件消息再 @机器人）
   即可触发渲染；本条消息直接带文件也支持。
2. 私聊：直接把文件发给机器人即可渲染。
3. 可选：群文件上传后自动渲染（auto_render_group_upload=true），无需 @。

为了避免影响正常聊天，本插件的全局监听器只在命中「文件 / 待渲染缓存 / 相关关键词」
时才发送消息并阻止事件继续传播，其余消息原样放行。
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
import urllib.request
from dataclasses import dataclass, replace
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools
import astrbot.api.message_components as Comp

from . import java_renderer, material_table, material_xlsx, materials
from .compose import compose_views, trim_png
from .renderer import RenderError, RenderOptions, render_from_parsed
from .schematic_parser import (
    SUPPORTED_EXTENSIONS,
    SchematicParseError,
    parse_schematic,
)

try:  # 由解析器扩展任务提供；缺失时非 litematic 文件回退到 Python 渲染
    from .litematic_writer import write_litematic
except Exception:  # pragma: no cover - 兼容未完成 writer 的开发阶段
    write_litematic = None

PLUGIN_NAME = "astrbot_plugin_litematic_preview"
PLUGIN_VERSION = "1.0.0"

_EXT_RE = re.compile(
    r"\.(" + "|".join(ext.lstrip(".") for ext in SUPPORTED_EXTENSIONS) + r")$",
    re.IGNORECASE,
)

_HELP_HINT = (
    "📐 Minecraft 投影预览\n"
    "用法：发送 .litematic/.schem/.schematic/.nbt/Axiom .bp 文件后 @我"
    "（群聊里可以先发文件、再 @我，或者引用文件消息 @我；私聊直接发文件即可），"
    "我会把它渲染成等距预览图。"
)

_KEYWORDS = ("投影", "litematic", "schematic", "渲染", "预览", "蓝图", "结构文件")


def _matches_ext(filename: str) -> bool:
    return bool(_EXT_RE.search(filename or ""))


def _pretty_size(size: int) -> str:
    if size <= 0:
        return "?"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _short_error(text: str, limit: int = 120) -> str:
    text = str(text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class _PendingFile:
    """待渲染文件：可能来自 File 消息段，也可能来自原始 OneBot file 段。"""

    name: str
    group_id: str = ""
    user_id: str = ""
    source: str = "component"  # component / raw / group_upload / offline_file
    timestamp: float = 0.0
    size: int = 0
    file_id: str = ""
    file_hash: str = ""
    busid: int | str = 0
    url: str = ""
    component: Any = None


class LitematicPreviewPlugin(Star):
    """Minecraft 投影文件预览渲染插件。

    说明：AstrBot v3.5.19+ 会自动识别继承自 ``Star`` 的类作为插件类，
    无需再使用已废弃的 ``@register`` 装饰器；插件元信息由 ``metadata.yaml`` 提供。
    """

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config: dict = config if config is not None else {}
        # (group_id, user_id) -> _PendingFile
        self._pending: dict[tuple[str, str], _PendingFile] = {}
        # 渲染冷却：user_id -> 上次渲染时间
        self._last_render: dict[str, float] = {}
        self._render_sem: asyncio.Semaphore | None = None
        self._java_sem: asyncio.Semaphore | None = None
        self._last_pending_gc = 0.0
        self._data_dir: str | None = None
        self._java_probe: dict | None = None
        self._java_probe_key: tuple | None = None
        self.logger.info(
            f"[{PLUGIN_NAME}] 已加载 v{PLUGIN_VERSION}，支持格式: {', '.join(SUPPORTED_EXTENSIONS)}"
        )

    async def initialize(self) -> None:
        """插件被激活时调用：探测 Java 渲染器并记录到日志。"""
        try:
            info = await self._java_probe_async(refresh=True)
            if info.get("available"):
                self.logger.info(
                    f"[{PLUGIN_NAME}] Java 渲染引擎可用：Java {info.get('java_major')} "
                    f"({info.get('java_path')})"
                )
            else:
                self.logger.warning(
                    f"[{PLUGIN_NAME}] Java 渲染引擎不可用，将使用简化 Python 渲染：{info.get('error')}"
                )
        except Exception:
            self.logger.exception(f"[{PLUGIN_NAME}] Java 渲染器探测失败")

    # ------------------------------------------------------------------ #
    # 事件入口
    # ------------------------------------------------------------------ #

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_event(self, event: AstrMessageEvent):
        """监听所有事件：文件消息 / 文件上传通知 / @机器人的后续消息。

        采用「直接 event.send() 后 stop_event()」的写法：本插件的回复不经过
        ResultDecorate/Respond 阶段，因此对 notice 派生事件（没有真实 message_id）
        同样可靠，也不会触发 LLM 二次回复。
        """
        if not self._cfg_bool("enabled", True):
            return
        try:
            chain = await self._dispatch(event)
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 处理事件失败")
            return
        if not chain:
            return
        if await self._send_chain(event, chain):
            event.should_call_llm(False)
            event.stop_event()

    async def _send_chain(self, event: AstrMessageEvent, chain: list) -> bool:
        # 文件消息段和普通消息不能混在一条里发送；先发文本/图片，再逐个发文件
        files = [comp for comp in chain if isinstance(comp, Comp.File)]
        others = [comp for comp in chain if not isinstance(comp, Comp.File)]
        try:
            if others:
                await event.send(MessageChain(others))
            for comp in files:
                await event.send(MessageChain([comp]))
            return True
        except Exception:
            logger.exception(f"[{PLUGIN_NAME}] 发送回复失败")
            # 退化为纯文本，避免用户什么都收不到
            texts = [c.text for c in others if isinstance(c, Comp.Plain) and c.text]
            if texts:
                try:
                    await event.send(MessageChain([Comp.Plain("\n".join(texts))]))
                    return True
                except Exception:
                    logger.exception(f"[{PLUGIN_NAME}] 发送文本兜底也失败")
            return False
        finally:
            for comp in files:
                self._cleanup_material_file(comp)

    async def _dispatch(self, event: AstrMessageEvent) -> list | None:
        """返回要发送的消息链；None 表示不处理该事件。"""
        raw = getattr(event.message_obj, "raw_message", None)
        if self._is_notice(raw):
            return await self._handle_notice(event, raw)

        messages = event.get_messages() or []
        triggered = self._is_triggered(event, messages)
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id() or "")

        pendings = self._collect_files(event, messages)

        # 没有 @ 机器人时：若开启了自动渲染，直接渲染；否则缓存等 @
        if not triggered:
            supported_now = [p for p in pendings if _matches_ext(p.name)]
            unsupported_now = [p for p in pendings if not _matches_ext(p.name)]
            auto_render = (
                self._cfg_bool("auto_render_private", True)
                if event.is_private_chat()
                else self._cfg_bool("auto_render_group_upload", False)
            )
            if supported_now and auto_render:
                self._maybe_gc_pending()
                logger.info(
                    f"[{PLUGIN_NAME}] 检测到文件消息，自动渲染 "
                    f"{', '.join(p.name for p in supported_now)}"
                )
                return await self._render_pendings(event, supported_now, unsupported_now)
            for pending in pendings:
                if _matches_ext(pending.name):
                    pending.group_id = group_id
                    pending.user_id = user_id
                    pending.timestamp = time.time()
                    self._remember_pending(group_id, user_id, pending)
                    logger.info(f"[{PLUGIN_NAME}] 缓存文件 {pending.name}（等待 @ 触发）")
            return None

        self._maybe_gc_pending()

        # 1) 本条消息（或其引用消息）直接带文件
        supported = [p for p in pendings if _matches_ext(p.name)]
        unsupported = [p for p in pendings if not _matches_ext(p.name)]
        if supported or unsupported:
            return await self._render_pendings(event, supported, unsupported)

        # 2) 本条消息没带文件：看看有没有刚发过、等待 @ 的文件
        pending = self._pop_pending(group_id, user_id)
        if pending is not None:
            return await self._render_pendings(event, [pending], [])

        # 3) 只是 @ 了机器人并提到投影/渲染：给个用法提示
        if self._looks_like_request(event):
            return [Comp.Plain(_HELP_HINT)]
        return None

    # ------------------------------------------------------------------ #
    # 文件收集
    # ------------------------------------------------------------------ #

    def _collect_files(self, event: AstrMessageEvent, messages: list) -> list[_PendingFile]:
        """把消息里的 File 组件（含引用消息里的）和原始 OneBot file 段统一成 _PendingFile。"""
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id() or "")
        now = time.time()

        components: list[Comp.File] = [m for m in messages if isinstance(m, Comp.File)]
        # 引用（回复）一条文件消息再 @机器人：AstrBot 会把被引用消息的 File 放进 Reply.chain
        for comp in messages:
            if isinstance(comp, Comp.Reply):
                for sub in getattr(comp, "chain", None) or []:
                    if isinstance(sub, Comp.File):
                        components.append(sub)

        raw_files = self._raw_file_segments(getattr(event.message_obj, "raw_message", None))
        out: list[_PendingFile] = []
        for index, comp in enumerate(components):
            name = str(getattr(comp, "name", "") or "")
            info = raw_files[index] if index < len(raw_files) else {}
            out.append(
                _PendingFile(
                    name=name,
                    group_id=group_id,
                    user_id=user_id,
                    source="component",
                    timestamp=now,
                    size=int(info.get("size") or info.get("file_size") or 0),
                    file_id=str(info.get("file_id") or info.get("id") or ""),
                    file_hash=str(info.get("file_hash") or ""),
                    busid=info.get("busid") or 0,
                    url=str(info.get("url") or getattr(comp, "url", "") or ""),
                    component=comp,
                )
            )
        # 适配器没有生成 File 组件时，用原始 file 段兜底
        if not components:
            for info in raw_files:
                name = str(info.get("name") or info.get("file") or "")
                if not name:
                    continue
                out.append(
                    _PendingFile(
                        name=name,
                        group_id=group_id,
                        user_id=user_id,
                        source="raw",
                        timestamp=now,
                        size=int(info.get("size") or info.get("file_size") or 0),
                        file_id=str(info.get("file_id") or info.get("id") or ""),
                        file_hash=str(info.get("file_hash") or ""),
                        busid=info.get("busid") or 0,
                        url=str(info.get("url") or ""),
                    )
                )
        return out

    @staticmethod
    def _raw_file_segments(raw: Any) -> list[dict]:
        """从原始 OneBot message 事件里提取 file 段 data。"""
        try:
            if not raw or raw.get("post_type") != "message":
                return []
            result: list[dict] = []
            for seg in raw.get("message") or []:
                if isinstance(seg, dict) and seg.get("type") == "file":
                    data = seg.get("data") or {}
                    if isinstance(data, dict):
                        result.append(data)
            return result
        except Exception:
            return []

    # ------------------------------------------------------------------ #
    # 触发判断
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_notice(raw: Any) -> bool:
        """aiocqhttp 的 Event 是 dict 子类；这里兼容 mapping 风格的原始事件。"""
        try:
            return raw is not None and raw.get("post_type") == "notice"
        except Exception:
            return False

    def _is_triggered(self, event: AstrMessageEvent, messages: list) -> bool:
        """私聊、@机器人、@全体（可配置）或命中唤醒前缀/引用机器人时触发。"""
        if event.is_private_chat():
            return True
        if event.is_at_or_wake_command:
            return True
        self_ids = {str(event.get_self_id() or "")}
        for comp in messages:
            if isinstance(comp, Comp.At):
                qq = str(getattr(comp, "qq", "") or "")
                if qq and qq in self_ids:
                    return True
                if qq == "all" and self._cfg_bool("trigger_at_all", False):
                    return True
        return False

    def _looks_like_request(self, event: AstrMessageEvent) -> bool:
        text = (event.message_str or "").lower()
        if _matches_ext(text):
            return True
        return any(keyword in text for keyword in _KEYWORDS)

    # ------------------------------------------------------------------ #
    # 文件上传通知（防御性兼容 group_upload / offline_file）
    # ------------------------------------------------------------------ #

    async def _handle_notice(self, event: AstrMessageEvent, raw: dict) -> list | None:
        notice_type = str(raw.get("notice_type") or "")
        group_id = str(raw.get("group_id") or "")
        user_id = str(raw.get("user_id") or "")
        file_info = raw.get("file") or {}
        if not isinstance(file_info, dict):
            return None

        if notice_type == "group_upload":
            name = str(file_info.get("name") or file_info.get("file") or "")
            if not _matches_ext(name):
                return None
            pending = _PendingFile(
                name=name,
                group_id=group_id,
                user_id=user_id,
                source="group_upload",
                timestamp=time.time(),
                size=int(file_info.get("size") or 0),
                file_id=str(file_info.get("id") or file_info.get("file_id") or ""),
                busid=file_info.get("busid") or 0,
            )
            auto = self._cfg_bool("auto_render_group_upload", False)
            if auto:
                logger.info(f"[{PLUGIN_NAME}] 群文件通知自动渲染 {name}")
                return await self._render_pendings(event, [pending], [])
            self._remember_pending(group_id, user_id, pending)
            logger.info(f"[{PLUGIN_NAME}] 缓存群文件通知 {name}（等待 @ 触发）")
            return None

        if notice_type == "offline_file":
            name = str(file_info.get("name") or file_info.get("file") or "")
            if not _matches_ext(name):
                return None
            pending = _PendingFile(
                name=name,
                group_id="",
                user_id=user_id,
                source="offline_file",
                timestamp=time.time(),
                size=int(file_info.get("size") or 0),
                url=str(file_info.get("url") or ""),
            )
            if self._cfg_bool("auto_render_private", True):
                return await self._render_pendings(event, [pending], [])
            self._remember_pending("", user_id, pending)
            return None

        return None

    # ------------------------------------------------------------------ #
    # 待渲染缓存
    # ------------------------------------------------------------------ #

    def _remember_pending(self, group_id: str, user_id: str, pending: _PendingFile) -> None:
        key = (group_id, user_id)
        self._pending.pop(key, None)
        self._pending[key] = pending
        self._maybe_gc_pending()

    def _pop_pending(self, group_id: str, user_id: str) -> _PendingFile | None:
        key = (group_id, user_id)
        pending = self._pending.pop(key, None)
        if pending is not None:
            if time.time() - pending.timestamp > self._pending_ttl():
                return None
            return pending
        # 可选：允许同群任何人刚发过的文件被当前 @ 触发
        if self._cfg_bool("pending_any_user_in_group", False) and group_id:
            candidates = [
                (k, v)
                for k, v in self._pending.items()
                if k[0] == group_id and time.time() - v.timestamp <= self._pending_ttl()
            ]
            if candidates:
                candidates.sort(key=lambda item: item[1].timestamp, reverse=True)
                key, pending = candidates[0]
                self._pending.pop(key, None)
                return pending
        return None

    def _pending_ttl(self) -> int:
        return self._cfg_int("pending_timeout_seconds", 600, minimum=10, maximum=3600)

    def _maybe_gc_pending(self) -> None:
        now = time.time()
        if now - self._last_pending_gc < 20:
            return
        self._last_pending_gc = now
        ttl = self._pending_ttl()
        for key, pending in list(self._pending.items()):
            if now - pending.timestamp > ttl:
                self._pending.pop(key, None)
        if len(self._pending) > 500:
            ordered = sorted(self._pending.items(), key=lambda item: item[1].timestamp)
            for key, _ in ordered[: len(self._pending) - 500]:
                self._pending.pop(key, None)

    # ------------------------------------------------------------------ #
    # 渲染入口
    # ------------------------------------------------------------------ #

    async def _render_pendings(self, event: AstrMessageEvent, supported: list,
                               unsupported: list) -> list | None:
        max_files = self._cfg_int("max_files_per_message", 3, minimum=1, maximum=9)
        chain: list = []
        lines: list[str] = []

        # 冷却按「一次请求」计算，而不是按文件；否则一条消息带多个文件时第二个必然被拦
        user_id = str(event.get_sender_id() or "")
        cooldown = self._cfg_int("cooldown_seconds", 3, minimum=0, maximum=120)
        now = time.time()
        if cooldown and now - self._last_render.get(user_id, 0.0) < cooldown:
            return [Comp.Plain("⏳ 渲染太频繁了，请稍等几秒再试。")]
        self._last_render[user_id] = now

        for pending in supported[:max_files]:
            line, images = await self._render_one(event, pending)
            if line:
                lines.append(line)
            chain.extend(images)

        if len(supported) > max_files:
            lines.append(f"⚠️ 一次最多渲染 {max_files} 个文件，其余已忽略。")
        if unsupported and self._cfg_bool("reply_on_unsupported", True):
            names = "、".join(p.name for p in unsupported[:5])
            lines.append(
                f"❌ 不支持的文件格式：{names}\n"
                f"目前支持：{' / '.join(SUPPORTED_EXTENSIONS)}"
            )
        if not chain and not lines:
            return None
        result_chain: list = []
        if lines:
            result_chain.append(Comp.Plain("\n".join(lines)))
        result_chain.extend(chain)
        return result_chain

    async def _render_one(self, event: AstrMessageEvent, pending: _PendingFile):
        """下载 + 渲染单个文件。返回 (文本, 图片组件列表)。"""
        name = pending.name or "file"

        if not _matches_ext(name):
            return (
                f"❌ 不支持的文件格式：{name}\n"
                f"目前支持：{' / '.join(SUPPORTED_EXTENSIONS)}",
                [],
            )

        # 下载文件内容
        try:
            data = await self._read_file(event, pending)
        except asyncio.TimeoutError:
            return f"⌛ 下载 {name} 超时，请稍后重试。", []
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] 下载文件失败: {name}")
            return f"❌ 下载文件失败：{exc}", []

        max_bytes = self._cfg_int("max_file_size_mb", 50, minimum=1, maximum=512) * 1024 * 1024
        if len(data) > max_bytes:
            return f"❌ 文件太大（{_pretty_size(len(data))}），上限 {max_bytes // 1024 // 1024} MB。", []
        if not data:
            return "❌ 文件内容为空，无法解析。", []

        engine = await self._choose_engine(name)
        if engine == "java":
            try:
                return await self._render_with_java(data, name)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] Java 渲染失败（{name}）：{exc}")
                if not self._cfg_bool("java_fallback_to_python", True):
                    return f"❌ Java 渲染失败：{exc}", []
                python_outcome = await self._render_with_python(data, name, fallback_reason=str(exc))
                return python_outcome
        return await self._render_with_python(data, name)

    # ------------------------------------------------------------------ #
    # Java 真实材质渲染
    # ------------------------------------------------------------------ #

    async def _render_with_java(self, data: bytes, name: str):
        """用独立 Java 渲染器渲染（.litematic 直接渲染；其它格式先转 litematic）。"""
        opts = self._build_java_options()
        ext = os.path.splitext(name)[1].lower()
        parsed = None
        render_data = data

        if ext != ".litematic":
            if write_litematic is None:
                raise java_renderer.JavaRendererError("当前版本的转换器不可用，无法用 Java 渲染该格式")
            parsed = await self._safe_parse(data, name)
            if parsed is None:
                raise java_renderer.JavaRendererError("投影解析失败，无法转换为 litematic 渲染")
            render_data = await asyncio.to_thread(
                write_litematic,
                parsed,
                name=(getattr(parsed, "name", "") or name),
                author=(getattr(parsed, "author", "") or ""),
                max_volume=self._max_schematic_volume(),
            )
            if not render_data:
                raise java_renderer.JavaRendererError("转换为 litematic 失败")

        view_mode = str(self._cfg("java_views", "quad") or "quad").strip().lower()
        parse_attempted = False

        # 单视角：按投影尺寸自动提高分辨率（让大建筑每个方块有更多像素）
        if view_mode != "quad" and self._cfg_bool("java_auto_resolution", True):
            if parsed is None:
                parsed = await self._safe_parse(data, name)
                parse_attempted = True
            if parsed is not None:
                auto_size = self._auto_output_size(parsed, opts)
                if auto_size != int(opts.output_size):
                    self.logger.info(
                        f"[{PLUGIN_NAME}] 自动清晰度：{opts.output_size}px -> {auto_size}px "
                        f"（尺寸 {parsed.width}×{parsed.height}×{parsed.length}）"
                    )
                    opts = replace(opts, output_size=auto_size)

        # 解析用于文案/统计，与渲染并行（失败不影响出图）
        parse_task = (
            None
            if parsed is not None or parse_attempted
            else self._safe_parse(data, name)
        )

        if view_mode == "quad":
            return await self._render_java_quad(render_data, name, opts, parse_task)

        render_task = asyncio.to_thread(
            java_renderer.render_litematic_bytes, render_data, name, opts
        )
        # 外层等待要覆盖“按分辨率自动放宽后的超时”，否则会把正常的大图渲染提前取消
        render_timeout = java_renderer._effective_timeout(opts) + 60
        async with self._get_semaphore():
            async with self._get_java_semaphore():
                if parse_task is not None:
                    render_result, parse_result = await asyncio.gather(
                        asyncio.wait_for(render_task, timeout=render_timeout),
                        parse_task,
                    )
                    if isinstance(parse_result, Exception) or parse_result is None:
                        parsed = None
                    else:
                        parsed = parse_result
                else:
                    render_result = await asyncio.wait_for(render_task, timeout=render_timeout)

        views = render_result.image_names(opts.send_both_views)
        images = [Comp.Image.fromBytes(render_result.images[v]) for v in views if render_result.images.get(v)]
        if not images:
            raise java_renderer.JavaRendererError("Java 渲染器没有返回图片")

        self.logger.info(
            f"[{PLUGIN_NAME}] Java 渲染完成 {name}: {java_renderer.describe(render_result, opts.send_both_views)}"
        )
        material_components = await self._material_components(parsed, name)
        if material_components:
            images = [*images, *material_components]
        if not self._cfg_bool("reply_with_text", True):
            return "", images

        lines = ["✅ 投影预览渲染完成（真实材质）", f"📄 {name}"]
        if parsed is not None:
            lines.append(
                f"📐 尺寸 {parsed.width}×{parsed.height}×{parsed.length} · "
                f"非空气方块 {getattr(parsed, 'total_blocks', 0):,} 个"
            )
        if render_result.cached:
            lines.append(f"🎨 {len(images)} 视图 · 缓存命中 · {opts.output_size}px")
        else:
            lines.append(
                f"🎨 {len(images)} 视图 · Java {render_result.java_major or ''} 渲染 · "
                f"{opts.output_size}px · 耗时 {render_result.elapsed:.2f}s"
            )
        unsupported_line = self._unsupported_warning([render_result])
        if unsupported_line:
            lines.append(unsupported_line)
        for note in getattr(render_result, "notes", []) or []:
            lines.append(f"⚠️ {note}")
        lines.extend(await self._material_lines(parsed))
        total_bytes = sum(len(render_result.images.get(v, b"")) for v in views)
        if total_bytes > 8 * 1024 * 1024:
            lines.append(
                f"⚠️ 图片较大（合计 {total_bytes / 1024 / 1024:.1f}MB），QQ 可能压缩或发送失败；"
                f"建议降低 render_unified_size 或稍后重试"
            )
        return "\n".join(lines), images

    async def _render_java_quad(self, render_data: bytes, name: str,
                                opts: java_renderer.JavaRenderOptions, parse_task) -> tuple:
        """四对角视角：用 base/base+90 两次渲染拿到 4 个 90° 间隔视角，拼图或分开发送。"""
        base = float(opts.rotation) % 360.0
        angles = [base, (base + 90.0) % 360.0, (base + 180.0) % 360.0, (base + 270.0) % 360.0]
        view_size = int(self._render_sizes()["quad_view"])
        layout = str(self._cfg("java_quad_layout", "grid") or "grid").strip().lower()
        if layout not in ("grid", "separate"):
            layout = "grid"
        max_size = int(self._render_sizes()["quad_grid"])

        run_opts = [
            replace(opts, output_size=view_size, rotation=base, send_both_views=True),
            replace(opts, output_size=view_size, rotation=(base + 90.0) % 360.0, send_both_views=True),
        ]
        results: list[java_renderer.JavaRenderResult] = []
        async with self._get_semaphore():
            async with self._get_java_semaphore():
                for single_opts in run_opts:
                    task = asyncio.to_thread(
                        java_renderer.render_litematic_bytes, render_data, name, single_opts
                    )
                    run_timeout = java_renderer._effective_timeout(single_opts) + 60
                    results.append(await asyncio.wait_for(task, timeout=run_timeout))

        # 两次渲染各出「正向 + 旋转180°」，按 R / R+90 / R+180 / R+270 排序
        ordered = [
            results[0].images["isometric"],
            results[1].images["isometric"],
            results[0].images["isometric-reverse"],
            results[1].images["isometric-reverse"],
        ]
        labels = [f"视角 {i + 1} · {angles[i]:g}°" for i in range(4)]
        total_elapsed = sum(result.elapsed for result in results)
        cached = all(result.cached for result in results)
        unsupported = sum(result.unsupported_blocks for result in results)

        if layout == "grid":
            png = await asyncio.to_thread(
                compose_views,
                ordered,
                labels,
                columns=2,
                background=opts.background,
                transparent=opts.transparent_background,
                max_size=max_size,
                trim=opts.transparent_background,
            )
            sent_pngs = [png]
            images = [Comp.Image.fromBytes(png)]
        else:
            if opts.transparent_background:
                trimmed = await asyncio.to_thread(
                    lambda: [trim_png(png) for png in ordered]
                )
            else:
                trimmed = ordered
            sent_pngs = trimmed
            images = [Comp.Image.fromBytes(png) for png in trimmed]
        if not images:
            raise java_renderer.JavaRendererError("Java 渲染器没有返回图片")

        self.logger.info(
            f"[{PLUGIN_NAME}] Java 四视角渲染完成 {name}: angles={angles}, "
            f"layout={layout}, size={view_size}px, cached={cached}, "
            f"unsupported={unsupported}, elapsed={total_elapsed:.2f}s"
        )
        parsed = None
        if parse_task is not None:
            parse_result = await parse_task
            if parse_result is not None and not isinstance(parse_result, Exception):
                parsed = parse_result
        material_components = await self._material_components(parsed, name)
        if material_components:
            images = [*images, *material_components]
        if not self._cfg_bool("reply_with_text", True):
            return "", images
        lines = ["✅ 投影预览渲染完成（真实材质 · 四对角视角）", f"📄 {name}"]
        if parsed is not None:
            lines.append(
                f"📐 尺寸 {parsed.width}×{parsed.height}×{parsed.length} · "
                f"非空气方块 {getattr(parsed, 'total_blocks', 0):,} 个"
            )
        mode_desc = "2×2 总览图" if layout == "grid" else "4 张图片"
        if cached:
            lines.append(f"🎨 4 视角（{mode_desc}）· 缓存命中 · 单视角 {view_size}px")
        else:
            lines.append(
                f"🎨 4 视角（{mode_desc}）· Java {results[0].java_major or ''} 渲染 · "
                f"单视角 {view_size}px · 总耗时 {total_elapsed:.2f}s"
            )
        unsupported_line = self._unsupported_warning(results)
        if unsupported_line:
            lines.append(unsupported_line)
        seen_notes: list[str] = []
        for result in results:
            for note in getattr(result, "notes", []) or []:
                if note not in seen_notes:
                    seen_notes.append(note)
        for note in seen_notes:
            lines.append(f"⚠️ {note}")
        lines.extend(await self._material_lines(parsed))
        total_bytes = sum(len(png) for png in sent_pngs)
        if total_bytes > 8 * 1024 * 1024:
            lines.append(
                f"⚠️ 图片较大（合计 {total_bytes / 1024 / 1024:.1f}MB），QQ 可能压缩或发送失败；"
                f"建议降低 render_unified_size 或改用 2×2 总览图"
            )
        return "\n".join(lines), images

    def _unsupported_warning(self, results: list) -> str:
        """生成“缺失模型/材质”的提示，带上具体方块名与数量（最多 5 个示例）。"""
        total = 0
        parts: list[str] = []
        for result in results:
            count, text = java_renderer.unsupported_summary(
                getattr(result, "diagnostics", None)
            )
            total += count
            if text:
                parts.append(text)
        if total <= 0:
            return ""
        line = f"⚠️ {total:,} 个方块缺少模型/材质，已按占位显示"
        if parts:
            line += "（" + "；".join(parts) + "）"
        return line

    def _auto_output_size(self, parsed, opts) -> int:
        """按投影尺寸估算“看得清”的单视角分辨率（只升不降，可配置上限/关闭）。"""
        if parsed is None or not self._cfg_bool("java_auto_resolution", True):
            return int(opts.output_size)
        pixels_per_block = self._cfg_int(
            "java_auto_pixels_per_block", 8, minimum=2, maximum=20
        )
        auto_cap = int(self._render_sizes()["auto_max"])
        auto_cap = max(int(opts.output_size), auto_cap)
        width = max(1, int(getattr(parsed, "width", 0) or 0))
        height = max(1, int(getattr(parsed, "height", 0) or 0))
        length = max(1, int(getattr(parsed, "length", 0) or 0))
        fill = max(0.40, min(1.0, float(getattr(opts, "fill", 0.85) or 0.85)))
        # 等距投影下屏幕横向跨度近似 (X+Z)*0.707，纵向再加高度影响；
        # 8 像素/方块即可让 16×16 原版贴图有足够采样。
        span = (width + length) * 0.7071 + height * 0.5
        needed = int(span * pixels_per_block / fill)
        target = min(auto_cap, max(int(opts.output_size), needed))
        target = max(256, int(round(target / 128.0)) * 128)
        return min(8192, target)

    def _material_mode(self) -> str:
        mode = str(self._cfg("material_list_mode", "image") or "image").strip().lower()
        return mode if mode in ("image", "xlsx", "file", "text", "off") else "image"

    def _material_report_sync(self, parsed):
        """同步统计材料（调用方放到线程池里执行）。"""
        if parsed is None or not self._cfg_bool("material_list_enabled", True):
            return None
        locale = str(self._cfg("material_list_language", "zh_cn") or "zh_cn").strip().lower()
        return materials.count_materials(parsed, locale)

    def _material_filename(self, name: str, ext: str = ".txt") -> str:
        stem = os.path.splitext(os.path.basename(name or "投影"))[0]
        stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", stem).strip() or "投影"
        stem = stem[:40]
        stamp = time.strftime("%Y%m%d_%H%M%S")
        return f"{stem}_材料清单_{stamp}{ext}"

    async def _write_material_attachment(self, content, filename: str):
        """把文本/字节内容写入插件 materials 目录，返回 Comp.File。"""
        try:
            directory = os.path.join(self._plugin_data_dir(), "materials")
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, filename)
            if isinstance(content, bytes):
                with open(path, "wb") as f:
                    f.write(content)
            else:
                with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
                    f.write(str(content))
            await asyncio.to_thread(self._prune_material_files, directory)
            return Comp.File(name=filename, file=path)
        except OSError as exc:
            logger.warning(f"[{PLUGIN_NAME}] 写入材料清单文件失败：{exc}")
            return None

    def _material_txt_content(self, report, parsed, name: str) -> str:
        width = int(getattr(parsed, "width", 0) or 0)
        height = int(getattr(parsed, "height", 0) or 0)
        length = int(getattr(parsed, "length", 0) or 0)
        blocks = int(getattr(parsed, "total_blocks", 0) or 0)
        lines = [
            "Minecraft 投影材料清单",
            "=" * 48,
            f"投影文件：{os.path.basename(name or '')}",
            f"投影名称：{(getattr(parsed, 'name', '') or '').strip() or '(未命名)'}",
            f"尺寸：{width} × {height} × {length}",
            f"非空气方块：{blocks:,}",
            f"材料种类：{report.total_types}",
            f"材料合计：{report.total_items:,}",
            f"数据版本：{report.dataset_version or '未知'}",
            f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "物品名称\t物品ID\t数量\t组数\t潜影盒数",
            "-" * 72,
        ]
        for entry in report.items:
            lines.append(
                f"{entry.display_name}\t{entry.full_id}\t{entry.count:,}\t"
                f"{entry.stacks:,}\t{entry.shulker_boxes:,}"
            )
        if report.unresolved_blocks:
            examples = "、".join(
                f"{block}×{count:,}"
                for block, count in sorted(report.unresolved_detail.items(), key=lambda item: -item[1])[:8]
            )
            lines += [
                "",
                f"未计入：{report.unresolved_blocks:,} 个方块无法对应物品（{examples}）",
            ]
        if report.estimated_blocks:
            lines += [
                "",
                f"估算：{report.estimated_blocks:,} 个新版本方块按同名物品估算",
            ]
        ignored = report.ignored_blocks - sum(
            report.ignored_detail.get(block, 0) for block in ("air", "cave_air", "void_air")
        )
        if ignored > 0:
            lines += ["", f"已忽略：{ignored:,} 个空气/技术方块（活塞头、传送门、火、气泡柱等）"]
        return "\n".join(lines) + "\n"

    async def _material_file_component(self, report, parsed, name: str):
        """把完整材料清单写成 .txt 文件并返回 Comp.File。"""
        content = await asyncio.to_thread(self._material_txt_content, report, parsed, name)
        return await self._write_material_attachment(content, self._material_filename(name, ".txt"))

    async def _material_xlsx_component(self, report, parsed, name: str):
        """生成材料清单 Excel 文件并返回 Comp.File。"""
        try:
            data = await asyncio.to_thread(
                material_xlsx.build_material_workbook, report, parsed, name
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 生成 Excel 材料清单失败：{exc}")
            return None
        return await self._write_material_attachment(
            data, self._material_filename(name, ".xlsx")
        )

    async def _material_components(self, parsed, name: str) -> list:
        """按配置生成材料清单组件：表格图片 / txt 文件 / 文本行，并可选附带 Excel 文件。"""
        if parsed is None or not self._cfg_bool("material_list_enabled", True):
            return []
        mode = self._material_mode()
        if mode == "off":
            return []
        try:
            report = await asyncio.to_thread(self._material_report_sync, parsed)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 材料统计失败：{exc}")
            return []
        if report is None or report.total_types <= 0:
            return []
        components: list = []
        if mode == "file":
            comp = await self._material_file_component(report, parsed, name)
            if comp is not None:
                components.append(comp)
        elif mode == "image":
            try:
                pages = await asyncio.to_thread(
                    material_table.render_material_table_pages,
                    report,
                    parsed,
                    title=f"投影材料清单 · {os.path.splitext(os.path.basename(name or '投影'))[0]}",
                )
                components.extend(Comp.Image.fromBytes(png) for png in pages)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 材料表格图片渲染失败：{exc}")
        # text 模式由 _material_lines 输出文本；xlsx 模式才发 Excel
        if mode == "xlsx":
            xlsx_comp = await self._material_xlsx_component(report, parsed, name)
            if xlsx_comp is not None:
                components.append(xlsx_comp)
        return components

    def _prune_material_files(self, directory: str, keep: int = 100, max_age: int = 2 * 24 * 3600) -> None:
        try:
            entries = []
            now = time.time()
            for entry in os.scandir(directory):
                if not entry.is_file() or not entry.name.endswith((".txt", ".xlsx")):
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if now - mtime > max_age:
                    try:
                        os.remove(entry.path)
                    except OSError:
                        pass
                    continue
                entries.append((mtime, entry.path))
            entries.sort(reverse=True)
            for _, path in entries[keep:]:
                try:
                    os.remove(path)
                except OSError:
                    pass
        except OSError:
            pass

    def _cleanup_material_file(self, comp) -> None:
        path = getattr(comp, "file_", "") or ""
        if not path:
            return
        try:
            directory = os.path.join(self._plugin_data_dir(), "materials")
            if os.path.abspath(os.path.dirname(path)) == os.path.abspath(directory):
                os.remove(path)
        except OSError:
            pass

    async def _material_lines(self, parsed) -> list[str]:
        """文本模式：生成“所需物品”文字行（文件模式/关闭时返回空列表）。"""
        if parsed is None or not self._cfg_bool("material_list_enabled", True):
            return []
        if self._material_mode() != "text":
            return []
        locale = str(self._cfg("material_list_language", "zh_cn") or "zh_cn").strip().lower()
        limit = self._cfg_int("material_list_max_items", 15, minimum=1, maximum=50)
        try:
            report = await asyncio.to_thread(materials.count_materials, parsed, locale)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 材料统计失败：{exc}")
            return []
        if report.total_types <= 0:
            return []
        lines = [
            f"📦 所需物品（共 {report.total_types} 种 / {materials.format_count(report.total_items)} 个）："
        ]
        for entry in report.top(limit):
            lines.append(f"  • {entry.display_name} × {materials.format_count(entry.count)}")
        if report.total_types > limit:
            lines.append(f"  … 另有 {report.total_types - limit} 种，完整清单可把 material_list_mode 改为 file 或 xlsx 发送")
        if report.unresolved_blocks:
            lines.append(f"⚠️ {materials.format_count(report.unresolved_blocks)} 个方块无法对应物品，未计入")
        return lines

    def _effective_resource_packs(self) -> tuple[str, ...]:
        """按材质包列表（可启用/关闭）解析出实际使用的 zip 路径。

        每个列表项支持：
        - file: AstrBot 配置上传的 zip（相对插件数据目录 files/...）
        - path: 本地/内置 zip 路径（相对插件目录或插件数据目录）
        """
        if isinstance(self.config, dict):
            entries = self.config.get("java_material_packs")
        else:
            entries = getattr(self.config, "get", lambda *_: None)("java_material_packs")
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            return ()

        packs: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict) or not bool(entry.get("enabled", True)):
                continue
            for raw in entry.get("file") or []:
                value = str(raw or "").strip().strip('"').strip("'")
                if not value:
                    continue
                candidates = [value] if os.path.isabs(value) else [
                    os.path.join(self._plugin_data_dir(), value)
                ]
                for candidate in candidates:
                    if os.path.isfile(candidate):
                        packs.append(candidate)
                        break
            raw_path = str(entry.get("path") or "").strip().strip('"').strip("'")
            if raw_path:
                candidates = [raw_path] if os.path.isabs(raw_path) else [
                    os.path.join(java_renderer.PLUGIN_DIR, raw_path),
                    os.path.join(self._plugin_data_dir(), raw_path),
                ]
                for candidate in candidates:
                    if os.path.isfile(candidate):
                        packs.append(candidate)
                        break

        merged: list[str] = []
        seen: set[str] = set()
        for path in packs:
            key = os.path.normcase(os.path.abspath(path))
            if key in seen:
                continue
            seen.add(key)
            merged.append(path)
        return tuple(merged)

    def _render_sizes(self) -> dict:
        """统一渲染尺寸 / 高级单独尺寸。"""
        unified = self._cfg_int("render_unified_size", 2048, minimum=256, maximum=8192)

        def clamp(value, low, high):
            try:
                number = int(value)
            except (TypeError, ValueError):
                number = unified
            return max(low, min(high, number))

        advanced = self._cfg("render_size_advanced", {}) or {}
        if isinstance(advanced, dict) and bool(advanced.get("enabled", False)):
            return {
                "single": clamp(advanced.get("single", unified), 256, 8192),
                "quad_view": clamp(advanced.get("quad_view", unified), 256, 8192),
                "quad_grid": clamp(advanced.get("quad_grid", unified), 512, 8192),
                "python_width": clamp(advanced.get("python_width", unified), 320, 8192),
                "python_height": clamp(advanced.get("python_height", unified), 320, 8192),
                "auto_max": clamp(advanced.get("auto_max", 8192), 256, 8192),
            }
        return {
            "single": unified,
            "quad_view": unified,
            "quad_grid": unified,
            "python_width": unified,
            "python_height": unified,
            "auto_max": 8192,
        }

    def _resolve_resources_path(self) -> str:
        """解析原版资源包：配置路径 > 插件数据目录 resources/ > 内置资源。"""
        configured = str(self._cfg("java_resources_path", "") or "").strip().strip('"').strip("'")
        if configured:
            candidates = [configured] if os.path.isabs(configured) else [
                os.path.join(self._plugin_data_dir(), configured),
                os.path.join(java_renderer.PLUGIN_DIR, configured),
                configured,
            ]
            for path in candidates:
                if os.path.isfile(path):
                    return path
            # 路径配置了但不存在：返回原值，让渲染器给出明确错误
            return configured

        candidates: list[str] = []

        def add_zip_candidates(directory: str) -> None:
            try:
                names = [
                    name for name in os.listdir(directory)
                    if name.lower().endswith(".zip")
                ]
            except OSError:
                return
            # 优先带 vanilla/原版/resources 字样的包，否则按文件名
            names.sort(key=lambda name: (
                0 if any(key in name.lower() for key in ("vanilla", "原版", "resources")) else 1,
                name,
            ))
            candidates.extend(os.path.join(directory, name) for name in names)

        # 1) 插件数据目录 resources/（推荐用户放这里）
        add_zip_candidates(os.path.join(self._plugin_data_dir(), "resources"))
        # 2) 插件 assets/vanilla-resources/（可直接把用户资源包拖进插件目录）
        add_zip_candidates(os.path.join(java_renderer.PLUGIN_DIR, "assets", "vanilla-resources"))
        # 3) 兼容旧的固定文件名
        candidates.append(java_renderer.DEFAULT_RESOURCES)

        seen: set[str] = set()
        for path in candidates:
            key = os.path.normcase(os.path.abspath(path))
            if key in seen:
                continue
            seen.add(key)
            if os.path.isfile(path):
                return path
        return ""

    def _build_java_options(self) -> java_renderer.JavaRenderOptions:
        return java_renderer.JavaRenderOptions(
            output_size=int(self._render_sizes()["single"]),
            supersampling=self._cfg_int("java_supersampling", 1, minimum=1, maximum=4),
            rotation=self._cfg_float("java_rotation", 135.0),
            slant=self._cfg_float("java_slant", 36.0),
            fill=self._cfg_float("java_fill", 0.85),
            background=str(self._cfg("java_background", "#000000") or "#000000"),
            transparent_background=self._cfg_bool("java_transparent_background", True),
            heap_mb=self._cfg_int("java_heap_mb", 512, minimum=256, maximum=16384),
            timeout=self._cfg_int("java_render_timeout_seconds", 60, minimum=30, maximum=1800),
            resource_packs=self._effective_resource_packs(),
            send_both_views=self._cfg_bool("java_send_both_views", False),
            java_path=str(self._cfg("java_path", "") or ""),
            resources_path=self._resolve_resources_path(),
            cache_dir=self._java_cache_dir(),
            cache_max_mb=self._cfg_int("java_cache_max_mb", 300, minimum=0, maximum=5000),
        )

    async def _java_probe_async(self, refresh: bool = False) -> dict:
        opts = self._build_java_options()
        key = (opts.java_path, opts.jar_path, opts.resources_path)
        if not refresh and self._java_probe is not None and self._java_probe_key == key:
            return self._java_probe
        info = await asyncio.to_thread(java_renderer.probe, opts)
        self._java_probe = info
        self._java_probe_key = key
        return info

    async def _choose_engine(self, name: str) -> str:
        """根据配置和 Java 可用性选择渲染引擎。"""
        mode = str(self._cfg("render_engine", "auto") or "auto").strip().lower()
        if mode == "python":
            return "python"
        ext = os.path.splitext(name)[1].lower()
        if ext != ".litematic" and write_litematic is None:
            return "python"
        info = await self._java_probe_async()
        if mode == "java":
            return "java"
        return "java" if info.get("available") else "python"

    def _max_schematic_volume(self) -> int:
        return self._cfg_int(
            "max_schematic_volume", 32_000_000, minimum=1_000_000, maximum=200_000_000
        )

    async def _safe_parse(self, data: bytes, name: str):
        """解析文件（失败返回 None，不抛异常），用于渲染文案与格式转换。"""
        timeout = self._cfg_int("render_timeout_seconds", 180, minimum=30, maximum=600)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(parse_schematic, data, name, self._max_schematic_volume()), timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.info(f"[{PLUGIN_NAME}] 解析 {name} 失败（不影响 Java 渲染）：{exc}")
            return None

    # ------------------------------------------------------------------ #
    # 简化 Python 渲染（回退）
    # ------------------------------------------------------------------ #

    async def _render_with_python(self, data: bytes, name: str, fallback_reason: str = ""):
        render_timeout = self._cfg_int("render_timeout_seconds", 180, minimum=30, maximum=600)
        options = self._build_render_options()
        try:
            parsed = await asyncio.wait_for(
                asyncio.to_thread(parse_schematic, data, name, self._max_schematic_volume()), timeout=render_timeout
            )
        except asyncio.TimeoutError:
            return f"⌛ 解析 {name} 超时（>{render_timeout}s）。", []
        except SchematicParseError as exc:
            return f"❌ 解析 {name} 失败：{exc}", []
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] 解析文件异常: {name}")
            return f"❌ 解析 {name} 时出现异常：{exc}", []

        raw_title = (getattr(parsed, "name", "") or "").strip()
        if not raw_title or raw_title.lower() in ("unnamed", "未命名", "无标题"):
            raw_title = name
        title = raw_title
        subtitle = self._build_subtitle(parsed)
        try:
            async with self._get_semaphore():
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        render_from_parsed,
                        parsed,
                        title=title,
                        subtitle=subtitle,
                        options=options,
                    ),
                    timeout=render_timeout,
                )
        except asyncio.TimeoutError:
            return f"⌛ 渲染 {name} 超时（>{render_timeout}s），投影可能太大。", []
        except RenderError as exc:
            return f"❌ 渲染 {name} 失败：{exc}", []
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] 渲染异常: {name}")
            return f"❌ 渲染 {name} 时出现异常：{exc}", []

        self.logger.info(
            f"[{PLUGIN_NAME}] Python 简化渲染完成 {name}: {parsed.width}×{parsed.height}×{parsed.length}, "
            f"可见 {result.visible_blocks} 块, {result.width}×{result.height}px, {result.elapsed:.2f}s"
        )
        image_components = [Comp.Image.fromBytes(result.png)]
        image_components = [
            *image_components,
            *(await self._material_components(parsed, name)),
        ]
        if not self._cfg_bool("reply_with_text", True):
            return "", image_components

        lines = ["✅ 投影预览渲染完成"]
        if fallback_reason:
            lines[0] = "✅ 投影预览渲染完成（简化渲染）"
        lines.append(f"📄 {name}")
        lines.append(
            f"📐 尺寸 {parsed.width}×{parsed.height}×{parsed.length} · "
            f"非空气方块 {getattr(parsed, 'total_blocks', 0):,} 个"
        )
        lines.append(
            f"🎨 可见方块 {result.visible_blocks:,} 个 · 输出 {result.width}×{result.height}px · "
            f"耗时 {result.elapsed:.2f}s"
        )
        if fallback_reason:
            lines.append(f"⚠️ Java 真实材质渲染不可用：{_short_error(fallback_reason)}")
        lines.extend(await self._material_lines(parsed))
        return "\n".join(lines), image_components

    # ------------------------------------------------------------------ #
    # 文件读取
    # ------------------------------------------------------------------ #

    async def _read_file(self, event: AstrMessageEvent, pending: _PendingFile) -> bytes:
        if pending.component is not None:
            try:
                return await self._read_component_file(pending.component)
            except Exception:
                # 组件下载失败时退回用原始 file_id / url 自取
                if not pending.file_id and not pending.url:
                    raise
                logger.warning(f"[{PLUGIN_NAME}] File 组件下载失败，尝试直接从 OneBot 取文件")
        # 优先用 OneBot action 换一个新鲜链接（消息里的 url 可能会过期），失败再用缓存 url
        url = ""
        if pending.file_id:
            url = await self._resolve_file_url(event, pending)
        url = url or pending.url
        if not url:
            raise RuntimeError("无法获取文件下载链接（可能文件已过期，请重新发送）")
        return await asyncio.to_thread(_http_download, url, 512 * 1024 * 1024, 120)

    async def _read_component_file(self, comp: Comp.File) -> bytes:
        path = await comp.get_file()
        if not path or not os.path.exists(path):
            raise FileNotFoundError("适配器未能提供文件下载地址")
        try:
            return await asyncio.to_thread(_read_file_bytes, path, 512 * 1024 * 1024)
        finally:
            # File.get_file() 下载的临时文件（fileseg_ 前缀）AstrBot 不会自动清理，这里主动删掉
            try:
                if os.path.basename(path).startswith("fileseg_"):
                    os.remove(path)
            except OSError:
                pass

    async def _resolve_file_url(self, event: AstrMessageEvent, pending: _PendingFile) -> str:
        """通过 OneBot action 获取文件下载链接（群：get_group_file_url；私聊：get_private_file_url）。"""
        bot = getattr(event, "bot", None)
        if bot is None or not pending.file_id:
            return ""
        if pending.group_id:
            params: dict[str, Any] = {"file_id": pending.file_id}
            params["group_id"] = (
                int(pending.group_id) if pending.group_id.isdigit() else pending.group_id
            )
            if pending.busid:
                params["busid"] = pending.busid
            action = "get_group_file_url"
        else:
            params = {"file_id": pending.file_id}
            if pending.file_hash:
                params["file_hash"] = pending.file_hash
            action = "get_private_file_url"
        try:
            ret = await bot.call_action(action, **params)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] {action} 调用失败: {exc}")
            return ""
        if isinstance(ret, dict):
            return str(ret.get("url") or "")
        return ""

    # ------------------------------------------------------------------ #
    # 配置 / 数据目录 / 工具
    # ------------------------------------------------------------------ #

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _cfg_int(self, key: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
        try:
            value = int(self._cfg(key, default))
        except (TypeError, ValueError):
            value = default
        value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(value)

    def _build_render_options(self) -> RenderOptions:
        sizes = self._render_sizes()
        return RenderOptions(
            max_width=int(sizes["python_width"]),
            max_height=int(sizes["python_height"]),
            prefer_tile_size=self._cfg_int("render_tile_size", 28, minimum=4, maximum=32),
            background=str(self._cfg("render_background", "dark") or "dark"),
            show_caption=self._cfg_bool("render_show_caption", True),
            skip_decorations=self._cfg_bool("render_skip_decorations", True),
            max_visible_blocks=self._cfg_int(
                "render_max_visible_blocks", 1_000_000, minimum=10_000, maximum=2_000_000
            ),
        )

    @staticmethod
    def _build_subtitle(parsed) -> str:
        fmt = getattr(parsed, "format", "") or ""
        subtitle = f"尺寸 {parsed.width}×{parsed.height}×{parsed.length}"
        data_version = getattr(parsed, "data_version", 0) or 0
        if data_version:
            subtitle += f" · DataVersion {data_version}"
        if fmt:
            subtitle += f" · {fmt}"
        return subtitle

    def _plugin_data_dir(self) -> str:
        if self._data_dir:
            return self._data_dir
        try:
            self._data_dir = str(StarTools.get_data_dir(self.name or PLUGIN_NAME))
        except Exception:
            self._data_dir = os.path.join(
                tempfile.gettempdir(), "astrbot_plugin_litematic_preview"
            )
        try:
            os.makedirs(self._data_dir, exist_ok=True)
        except OSError:
            pass
        return self._data_dir

    def _java_cache_dir(self) -> str:
        if self._cfg_int("java_cache_max_mb", 300, minimum=0, maximum=5000) <= 0:
            return ""
        path = os.path.join(self._plugin_data_dir(), "java_cache")
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            return ""
        return path

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._render_sem is None:
            concurrency = self._cfg_int("render_concurrency", 1, minimum=1, maximum=8)
            self._render_sem = asyncio.Semaphore(concurrency)
        return self._render_sem

    def _get_java_semaphore(self) -> asyncio.Semaphore:
        """Java 渲染单独限流：每个 JVM 可能占用数 GB 堆，默认串行执行。"""
        if self._java_sem is None:
            concurrency = self._cfg_int("java_concurrency", 1, minimum=1, maximum=4)
            self._java_sem = asyncio.Semaphore(concurrency)
        return self._java_sem

    async def terminate(self) -> None:
        self._pending.clear()
        self._last_render.clear()
        self.logger.info(f"[{PLUGIN_NAME}] 插件已卸载")


# ---------------------------------------------------------------------- #
# 同步工具函数（在线程池里跑）
# ---------------------------------------------------------------------- #


def _read_file_bytes(path: str, max_bytes: int) -> bytes:
    size = os.path.getsize(path)
    if size > max_bytes:
        raise RuntimeError(f"文件过大（{_pretty_size(size)}）")
    with open(path, "rb") as f:
        return f.read()


def _http_download(url: str, max_bytes: int, timeout: int = 120) -> bytes:
    if not url.startswith(("http://", "https://")):
        raise RuntimeError(f"无效的下载链接: {url[:64]}")
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 astrbot-litematic-preview/2.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise RuntimeError("文件超过大小上限")
            chunks.append(chunk)
    return b"".join(chunks)
