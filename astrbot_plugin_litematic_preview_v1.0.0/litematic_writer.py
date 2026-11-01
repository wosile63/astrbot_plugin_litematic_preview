"""把 :class:`schematic_parser.ParsedSchematic` 重新打包成 Litematica v6 `.litematic`。

背景：Java 独立渲染器（koishi-plugin-litematic-renderer 的 standalone jar）只接受
`.litematic`。为了让 `.schem` / `.schematic` / `.nbt` 也能走真实材质/方块模型的高质量
渲染，先用纯标准库把解析结果写成 Litematica v6 文件，再交给 Java 渲染器。

设计要点
--------
- **纯标准库**：只用 ``gzip/io/struct/sys/time/array``，不依赖 nbtlib 等第三方库。
- **与解析器严格互逆**：本模块的 ``BlockStates`` 打包（连续位流，值可跨 64 位 long
  边界）是 `schematic_parser._unpack_litematic_block_states` 的逆运算，
  ``bits = max(2, ceil(log2(palette_size)))``、long 数 = ``ceil(volume * bits / 64)``。
  因此 ``parse_schematic(write_litematic(p), ".litematic")`` 的尺寸 / palette /
  方块索引与 ``p`` 完全一致（有测试覆盖）。
- **palette 0 号固定 ``minecraft:air``**：Litematica/渲染器都假定 0 号是空气；
  若传入的 ``ParsedSchematic`` 0 号不是空气，会重排调色板并重映射方块索引。
  调色板条目按 ``(Name, Properties)`` 去重。
- **保留方块状态**：把 ``parsed.palette_states[i]`` 写进 ``BlockStatePalette[i]`` 的
  ``Properties`` Compound（无属性时省略该字段），这样楼梯朝向、半砖上下等状态在
  Java 渲染器里能正确显示。
- ``palette_states`` 是解析器的新增可选字段：老代码构造的 ``ParsedSchematic`` 没有它
  （为空列表）时按“无属性”处理，保持向后兼容。

公开 API
--------
- :func:`write_litematic`
- :data:`DEFAULT_DATA_VERSION`
"""

from __future__ import annotations

import gzip
import io
import struct
import sys
import time
from array import array
from typing import Any

try:  # 作为包导入（AstrBot 正常加载插件时）
    from .schematic_parser import (
        AIR_BLOCK_NAMES,
        MAX_VOLUME,
        ParsedSchematic,
        SchematicParseError,
    )
except ImportError:  # 直接以顶层模块导入（单元测试）
    from schematic_parser import (  # type: ignore[no-redef]
        AIR_BLOCK_NAMES,
        MAX_VOLUME,
        ParsedSchematic,
        SchematicParseError,
    )

__all__ = ["write_litematic", "DEFAULT_DATA_VERSION"]

#: Minecraft DataVersion 默认值（1.20.1）。parsed.data_version 为 0 时使用。
DEFAULT_DATA_VERSION = 3465

#: 生成的 Litematica 格式版本
LITEMATIC_VERSION = 6
LITEMATIC_SUBVERSION = 1

#: 统一后的空气方块名
AIR_NAME = "minecraft:air"

_MASK64 = (1 << 64) - 1

# NBT 标签类型
_TAG_BYTE = 1
_TAG_SHORT = 2
_TAG_INT = 3
_TAG_LONG = 4
_TAG_FLOAT = 5
_TAG_DOUBLE = 6
_TAG_BYTE_ARRAY = 7
_TAG_STRING = 8
_TAG_LIST = 9
_TAG_COMPOUND = 10
_TAG_INT_ARRAY = 11
_TAG_LONG_ARRAY = 12


# --------------------------------------------------------------------------- #
# 极简 NBT 写入器（大端序）
# --------------------------------------------------------------------------- #


def _int(value: int) -> tuple[int, int]:
    return (_TAG_INT, int(value))


def _long(value: int) -> tuple[int, int]:
    return (_TAG_LONG, int(value))


def _string(value: str) -> tuple[int, str]:
    return (_TAG_STRING, str(value))


def _compound(children: dict) -> tuple[int, dict]:
    return (_TAG_COMPOUND, children)


def _list(elem_type: int, items: list) -> tuple[int, tuple[int, list]]:
    return (_TAG_LIST, (elem_type, items))


def _write_string(out: io.BytesIO, value: str) -> None:
    raw = value.encode("utf-8")
    if len(raw) > 0xFFFF:
        raise SchematicParseError("字符串过长，无法写入 NBT。")
    out.write(struct.pack(">H", len(raw)))
    out.write(raw)


def _write_payload(out: io.BytesIO, tag: tuple[int, Any]) -> None:
    tag_type, value = tag
    if tag_type == _TAG_BYTE:
        out.write(struct.pack(">b", value))
    elif tag_type == _TAG_SHORT:
        out.write(struct.pack(">h", value))
    elif tag_type == _TAG_INT:
        out.write(struct.pack(">i", value))
    elif tag_type == _TAG_LONG:
        out.write(struct.pack(">q", value))
    elif tag_type == _TAG_FLOAT:
        out.write(struct.pack(">f", value))
    elif tag_type == _TAG_DOUBLE:
        out.write(struct.pack(">d", value))
    elif tag_type == _TAG_BYTE_ARRAY:
        out.write(struct.pack(">i", len(value)))
        out.write(value)
    elif tag_type == _TAG_STRING:
        _write_string(out, value)
    elif tag_type == _TAG_LIST:
        elem_type, items = value
        out.write(bytes([elem_type]))
        out.write(struct.pack(">i", len(items)))
        for item in items:
            _write_payload(out, item)
    elif tag_type == _TAG_COMPOUND:
        for name, child in value.items():
            out.write(bytes([child[0]]))
            _write_string(out, name)
            _write_payload(out, child)
        out.write(b"\x00")
    elif tag_type == _TAG_INT_ARRAY:
        out.write(struct.pack(">i", len(value)))
        for item in value:
            out.write(struct.pack(">i", item))
    elif tag_type == _TAG_LONG_ARRAY:
        # value 是 array('Q')，调用方已保证按大端字节序排好
        out.write(struct.pack(">i", len(value)))
        out.write(value.tobytes())
    else:  # pragma: no cover - 内部错误
        raise ValueError(f"不支持的 NBT 标签类型: {tag_type}")


def _nbt_bytes(root: dict) -> bytes:
    """把根 Compound（dict[str, (tag, value)]）序列化成未压缩 NBT 字节。"""
    out = io.BytesIO()
    out.write(bytes([_TAG_COMPOUND]))
    _write_string(out, "")  # 根名称为空
    _write_payload(out, (_TAG_COMPOUND, root))
    return out.getvalue()


# --------------------------------------------------------------------------- #
# 方块数据打包
# --------------------------------------------------------------------------- #


def _normalize_name(raw: Any) -> str:
    """把方块名统一成 ``minecraft:xxx``（去掉可能携带的 [state] 部分）。"""
    if not isinstance(raw, str):
        raise SchematicParseError("解析结果的 palette 中存在非字符串方块名。")
    name = raw.strip()
    if name.startswith("#"):
        name = name[1:]
    bracket = name.find("[")
    if bracket >= 0:
        name = name[:bracket]
    if not name:
        raise SchematicParseError("解析结果的 palette 中存在空方块名。")
    if ":" not in name:
        name = "minecraft:" + name
    return name


def _build_palette(parsed: ParsedSchematic) -> tuple[list[tuple[str, dict[str, str]]], array]:
    """构造写出的 Litematica 调色板与“原 palette 下标 -> 新下标”映射。

    返回 ``(palette, remap)``：``palette`` 是 [(方块名, Properties)] 列表，
    0 号固定空气；``remap`` 是 ``array('i')``。
    """
    palette = list(getattr(parsed, "palette", None) or [])
    if not palette:
        raise SchematicParseError("解析结果的 palette 为空，无法写入 litematic。")
    states = getattr(parsed, "palette_states", None) or []

    output: list[tuple[str, dict[str, str]]] = [(AIR_NAME, {})]
    remap = array("i", [0]) * len(palette)
    # (方块名, 属性) -> 新调色板下标；相同状态去重，保持调色板紧凑
    lookup: dict[tuple[str, frozenset], int] = {}
    for index, raw_name in enumerate(palette):
        name = _normalize_name(raw_name)
        properties = states[index] if index < len(states) and states[index] else {}
        if not isinstance(properties, dict):
            raise SchematicParseError("解析结果的 palette_states 中存在非 dict 项。")
        # 属性值统一成字符串（NBT String），保持确定性顺序
        props = {str(key): str(value) for key, value in properties.items()}
        if name in AIR_BLOCK_NAMES:
            remap[index] = 0
            continue
        key = (name, frozenset(props.items()))
        existing = lookup.get(key)
        if existing is not None:
            remap[index] = existing
            continue
        output_index = len(output)
        output.append((name, props))
        lookup[key] = output_index
        remap[index] = output_index
    return output, remap


def _pack_block_states(
    blocks: array, remap: array, palette_len: int, volume: int, bits: int
) -> tuple[array, int]:
    """把扁平方块索引数组打成 Litematica 连续位流（与解析器解包严格互逆）。

    返回 ``(LongArray(array('Q', 大端字节序)), 非空气方块数)``。
    """
    long_count = (volume * bits + 63) // 64
    # 用 array('Q') 而不是 Python list，避免 11M 方块级别时几十 MB 的 int 对象开销
    longs = array("Q", bytes(8 * long_count))
    bitpos = 0
    non_air = 0
    for index in range(volume):
        value = blocks[index]
        if value < 0 or value >= palette_len:
            raise SchematicParseError(
                f"解析结果损坏：blocks[{index}]={value} 超出 palette 范围（{palette_len}）。"
            )
        mapped = remap[value]
        if mapped:
            non_air += 1
        long_index = bitpos >> 6
        shift = bitpos & 63
        longs[long_index] |= (mapped << shift) & _MASK64
        if shift + bits > 64:
            longs[long_index + 1] |= mapped >> (64 - shift)
        bitpos += bits
    if sys.byteorder == "little":
        longs.byteswap()
    return longs, non_air


def _palette_tag(name: str, properties: dict[str, str]) -> tuple[int, dict]:
    """构造 BlockStatePalette 的一个条目 Compound（无属性时不写 Properties）。"""
    children: dict[str, Any] = {"Name": _string(name)}
    if properties:
        children["Properties"] = _compound(
            {str(key): _string(str(value)) for key, value in properties.items()}
        )
    return _compound(children)


# --------------------------------------------------------------------------- #
# 公开入口
# --------------------------------------------------------------------------- #


def write_litematic(
    parsed: ParsedSchematic,
    *,
    name: str = "",
    author: str = "",
    max_volume: int | None = None,
) -> bytes:
    """把解析结果写成 Litematica v6 格式的 gzip NBT 字节。

    :param parsed: :class:`schematic_parser.ParsedSchematic`（或鸭子类型对象：
        需要 width/height/length/palette/blocks，可选 palette_states 等字段）。
    :param name: 投影名称；传空串时沿用 ``parsed.name``。
    :param author: 作者；传空串时沿用 ``parsed.author``。
    :param max_volume: 可选的体积上限（格）；None 用默认 MAX_VOLUME。
    :return: 完整的 ``.litematic`` 文件字节（gzip 压缩）。
    :raises SchematicParseError: 尺寸为 0/体积超限/数组长度不一致/索引越界等。
    """
    if parsed is None:
        raise SchematicParseError("解析结果为空，无法写入 litematic。")

    try:
        width = int(getattr(parsed, "width"))
        height = int(getattr(parsed, "height"))
        length = int(getattr(parsed, "length"))
    except (TypeError, ValueError, AttributeError):
        raise SchematicParseError("解析结果缺少合法的 width/height/length 字段。") from None
    for axis_name, axis_value in (("X", width), ("Y", height), ("Z", length)):
        if axis_value <= 0:
            raise SchematicParseError(
                f"投影为空（{axis_name} 轴尺寸 {axis_value}），无法转换为 litematic。"
            )
    volume = width * height * length
    try:
        limit = int(max_volume) if max_volume is not None else MAX_VOLUME
    except (TypeError, ValueError):
        limit = MAX_VOLUME
    if volume > limit:
        raise SchematicParseError(
            f"投影体积过大：{width}×{height}×{length} = {volume:,} 格，"
            f"超过安全上限 {limit:,} 格，已拒绝写入。"
        )

    blocks = getattr(parsed, "blocks", None)
    if blocks is None:
        raise SchematicParseError("解析结果缺少 blocks 字段。")
    if len(blocks) != volume:
        raise SchematicParseError(
            f"解析结果的 blocks 长度 {len(blocks)} 与尺寸 {width}×{height}×{length}"
            f"（{volume}）不一致。"
        )

    palette, remap = _build_palette(parsed)
    bits = max(2, (len(palette) - 1).bit_length())
    longs, non_air = _pack_block_states(blocks, remap, len(palette), volume, bits)

    try:
        data_version = int(getattr(parsed, "data_version", 0) or 0)
    except (TypeError, ValueError):
        data_version = 0
    if data_version <= 0:
        data_version = DEFAULT_DATA_VERSION

    effective_name = str(name or getattr(parsed, "name", "") or "")
    effective_author = str(author or getattr(parsed, "author", "") or "")
    description = str(getattr(parsed, "description", "") or "")
    now_ms = int(time.time() * 1000)

    root = {
        "MinecraftDataVersion": _int(data_version),
        "Version": _int(LITEMATIC_VERSION),
        "SubVersion": _int(LITEMATIC_SUBVERSION),
        "Metadata": _compound(
            {
                "Name": _string(effective_name),
                "Author": _string(effective_author),
                "Description": _string(description),
                "EnclosingSize": _compound(
                    {"x": _int(width), "y": _int(height), "z": _int(length)}
                ),
                "RegionCount": _int(1),
                "TotalBlocks": _int(non_air),
                "TotalVolume": _int(volume),
                "TimeCreated": _long(now_ms),
                "TimeModified": _long(now_ms),
                "Software": _string("astrbot_plugin_litematic_preview/litematic_writer"),
            }
        ),
        "Regions": _compound(
            {
                "Main": _compound(
                    {
                        "Position": _compound({"x": _int(0), "y": _int(0), "z": _int(0)}),
                        "Size": _compound({"x": _int(width), "y": _int(height), "z": _int(length)}),
                        "BlockStatePalette": _list(
                            _TAG_COMPOUND,
                            [_palette_tag(palette_name, props) for palette_name, props in palette],
                        ),
                        "BlockStates": (_TAG_LONG_ARRAY, longs),
                        "TileEntities": _list(_TAG_COMPOUND, []),
                        "Entities": _list(_TAG_COMPOUND, []),
                        "PendingBlockTicks": _list(_TAG_COMPOUND, []),
                        "PendingFluidTicks": _list(_TAG_COMPOUND, []),
                    }
                )
            }
        ),
    }
    nbt = _nbt_bytes(root)
    # mtime=0 让输出可复现（便于缓存/测试）；compresslevel=6 在速度与体积间折中
    return gzip.compress(nbt, compresslevel=6, mtime=0)
