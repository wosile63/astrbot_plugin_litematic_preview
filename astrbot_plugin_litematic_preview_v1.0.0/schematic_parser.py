"""Minecraft 投影 / 结构文件解析器（纯 Python 标准库实现）。

本模块把 `.litematic` / `.schem` / `.schematic` / `.nbt` / `.bp` 五种文件解析为统一的
:class:`ParsedSchematic`：一个调色板 + 一个扁平方块索引数组，供渲染层生成等距预览图。
**不依赖任何第三方库**（不能 import numpy/PIL/nbtlib），也不读取磁盘——调用方传入文件字节。

支持的格式与参考依据
--------------------
1. ``.litematic``（Litematica 投影）
   - 文件结构：gzip 压缩的 NBT，根 Compound 含 ``MinecraftDataVersion``、
     ``Version``（投影格式版本）、``Metadata``、``Regions``。
   - ``Regions`` 下每个区域含 ``Position{x,y,z}``、``Size{x,y,z}``、
     ``BlockStatePalette``（List<Compound>，条目含 ``Name``/可选 ``Properties``）、
     ``BlockStates``（LongArray）。
   - 坐标系/负尺寸依据 Litemapy 文档《The Litematic file format》Coordinate Systems 一节：
     “regions can have negative dimensions, meaning they will contain blocks in the
     ``[size + 1; 0]`` range instead of the ``[0; size - 1]`` range, in their own
     coordinate system.”
     即：``Size > 0`` 时区域本地坐标范围是 ``[0, Size-1]``；``Size < 0`` 时是
     ``[Size+1, 0]``（Position 是负方向一侧的端点，相当于该轴镜像）。世界坐标 =
     Position + 本地坐标。
     （参考实现：litemapy ``Region.__region_coordinates_to_store_coordinates`` 与
     ``min_schem_x`` 等；本模块也用真实样本做了交叉验证，见
     ``research/parser_notes.md``。）
   - ``BlockStates`` 位打包语义：**Litematica 使用的是 malilib 的
     ``LitematicaBitArray``，即把整个区域视为一条连续位流**：
     第 i 个方块状态值位于第 ``i * bits`` 个 bit 处，可以跨 64 位 long 边界；
     ``bits = max(2, ceil(log2(palette_size)))``，long 数为
     ``ceil(volume * bits / 64)``。
     （注意：这与原版 Minecraft 1.16+ 的 ``BitStorage``——“每个 long 容纳
     ``64 // bits`` 个值、值不跨 long 边界”——**不同**。以真实文件行为为准：
     真实样本的 long 数量恰好等于连续位流公式，若按“不跨边界”解读会出现
     越界调色板索引；litemapy 同样按连续位流实现。详见 parser_notes.md。）
   - 多区域合并：每个区域按 Position 平移到统一世界坐标系，取所有非空气方块的
     包围盒作为输出尺寸；重叠位置的方块以“后解析到的区域优先”，空气不覆盖已有方块。
2. ``.schem``（Sponge Schematic / WorldEdit），参考 WorldEdit 源码
   ``SpongeSchematicV1Reader/V2Reader/V3Reader/Writer``：
   - v1：根 Compound 含 ``Version=1``、``Palette``（名称->索引 Compound）、
     ``BlockData``（varint 字节流）、``Width/Height/Length``（Short）、``Offset``、
     ``Metadata``。
   - v2：``Version=2``，调色板可能叫 ``Palettes``（List<Compound>，取第一套），
     WorldEdit 实际也写出单数 ``Palette``，两种都支持；``BlockData`` 同上。
   - v3：根 Compound 下再嵌一层 ``Schematic`` Compound，其中含
     ``Version=3``、``DataVersion``、``Width/Height/Length``、
     ``Blocks{Palette, Data, BlockEntities}``、``Offset``、``Metadata``。
   - 方块数据编码：官方规范为 varint；当调色板 ≤ 256 时部分实现（含 WorldEdit v3
     的读端启发式）会直接写“每格一字节”。因此按“``len(Data) == volume`` 则按原始
     字节、否则按 varint”解码——该启发式对两种编码都无歧义。
   - 方块顺序 ``index = (y * length + z) * width + x``。
3. ``.schematic``（MCEdit / 旧版 WorldEdit）
   - 根 Compound 含 ``Width/Height/Length``（Short）、``Blocks``（ByteArray，
     0~255 数字 ID）、``Data``（nibble 数组，低 4 位先）、可选 ``AddBlocks``
     （nibble 数组，提供 ID 高 8 位：``id = Blocks[i] + (AddBlocks nibble << 8)``）。
   - 数字 ID -> 方块名映射见同目录 ``legacy_ids.py``（由 minecraft-data 1.12 数据生成，
     已扁平化到现代名称；未知 ID 返回 ``legacy:<id>``）。
   - 方块顺序同样是 ``index = (y * length + z) * width + x``。
4. ``.nbt``（原版结构方块结构文件）
   - 根 Compound 含 ``DataVersion``、``size``（3 个 int）、``palette``
     （List<Compound>，``Name``/``Properties``）、``blocks``
     （List<Compound>，``pos`` 3 个 int + ``state`` int，可选 ``nbt``）。
   - 兼容多调色板变体（``palettes`` List<List<Compound>>，取第一套）。
5. ``.bp``（Axiom 蓝图 / Blueprint）
   - 容器结构：4 字节 magic ``0A E5 BB 36``，随后三个“大端 u32 长度前缀 + 数据”分段：
     metadata（未压缩的 NBT Compound）、preview（PNG 缩略图）、structure
     （gzip 压缩的 NBT Compound）。
   - metadata 含 ``Name`` / ``Author`` / ``Tags`` / ``BlockCount`` / ``Version`` 等；
     ``BlockCount`` 是文件中实际方块数，可用于校验解析结果。
   - structure 根 Compound 含 ``DataVersion`` 与 ``BlockRegion``（List<Compound>）。
     每个 BlockRegion 是一个 16×16×16 分块：``X``/``Y``/``Z`` 是分块坐标（乘 16 得到
     方块坐标），``BlockStates`` 为 ``{palette: List<Compound>, data: LongArray}``。
     ``data`` 是 4096 个调色板索引的位打包数据：调色板 ≤16 时固定 256 个 long
     （4-bit，按 Minecraft 1.16+ 连续位流语义：低比特优先、可跨 long 边界），
     方块顺序 ``i = x + 16*z + 256*y``；调色板更多时按
     ``bits = max(4, ceil(log2(palette_size)))`` 的连续位流解包（同时兼容
     “值不跨 long 边界”的逐 long 打包作为回退）。
   - 分块内用 ``minecraft:structure_void`` 表示“未保存的空气/空位”，解析时按空气处理。
   - 依据：nahkd123/file-structures-index 的 ``mc-schematic/axiom-blueprint-v1``
     格式索引；并用真实样本 ``samples/axiom_bp/kuqing_tree.bp``（元数据
     ``BlockCount=421,706``）逐块验证解析结果方块数完全一致（见 parser_notes.md）。

安全限制
--------
- 单轴尺寸 <= :data:`MAX_AXIS`（4096），输出包围盒体积 <= :data:`MAX_VOLUME`
  （16,000,000 格，约 61 MB int32；契约示例值 8,000,000 会让两个真实样本
  11,468,871 / 8,470,984 格被误杀，故按“真实样本必须可解析”优先放宽，见
  parser_notes.md）。
- NBT 解析限深 :data:`MAX_NBT_DEPTH`（64 层）、限制各种 List/数组长度，
  压缩数据解压后限制 :data:`MAX_DECOMPRESSED_BYTES`（128 MiB），防止 gzip 炸弹。
- 空文件、非 NBT、缺字段、长度不符、尺寸超限等都会抛出
  :class:`SchematicParseError`，其消息为可直接展示给用户的中文。

公开 API
--------
- :data:`SUPPORTED_EXTENSIONS`
- :class:`SchematicParseError`
- :class:`ParsedSchematic`（含 ``palette_states``：与 ``palette`` 等长的方块状态属性列表）
- :func:`parse_schematic`

配套模块
--------
- ``litematic_writer.py``：把 :class:`ParsedSchematic` 重新写回 Litematica v6
  ``.litematic``（供只接受 `.litematic` 的 Java 高质量渲染器使用），其
  ``BlockStates`` 连续位流与本模块的 ``_unpack_litematic_block_states`` 严格互逆。
"""

from __future__ import annotations

import struct
import zlib
from array import array
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

try:  # 作为包导入（AstrBot 正常加载插件时）
    from . import legacy_ids as _legacy_ids
except ImportError:  # 直接以顶层模块导入（单元测试）
    import legacy_ids as _legacy_ids  # type: ignore[no-redef]

__all__ = [
    "SUPPORTED_EXTENSIONS",
    "SchematicParseError",
    "ParsedSchematic",
    "parse_schematic",
    "MAX_AXIS",
    "MAX_VOLUME",
]

# --------------------------------------------------------------------------- #
# 常量与限制
# --------------------------------------------------------------------------- #

#: 支持的文件扩展名（小写，含点）
SUPPORTED_EXTENSIONS: tuple[str, ...] = (".litematic", ".schem", ".schematic", ".nbt", ".bp")

#: Axiom 蓝图（.bp）文件头 4 字节 magic（大端）
_AXIOM_BP_MAGIC = b"\x0a\xe5\xbb\x36"

#: Axiom 蓝图每个分块（BlockRegion）固定包含的方块数：16×16×16
_AXIOM_BP_CHUNK_BLOCKS = 16 * 16 * 16

#: Axiom 蓝图允许的最大分块数量（防御异常文件；真实大建筑通常几百到几千个分块）
MAX_AXIOM_REGIONS = 20_000

#: Axiom 蓝图所有分块原始体积上限（分块数 × 4096），防止异常文件耗尽内存；
#: 64,000,000 格，足以容纳带大量空分块的真实蓝图。
MAX_AXIOM_CHUNK_VOLUME = 64_000_000

#: 单轴最大尺寸（格）。超过此值直接拒绝，避免异常文件导致超大分配。
MAX_AXIS = 4096

#: 输出包围盒默认最大体积（格）。可在插件配置 max_schematic_volume 里单独修改；
#: 默认 32,000,000（约 122 MB int32）以容纳大型红石/建筑投影，最高允许 200,000,000。
MAX_VOLUME = 32_000_000

#: 允许用户配置的体积上限范围
MIN_VOLUME = 1_000_000
MAX_VOLUME_LIMIT = 200_000_000

#: 单次解析的体积上限覆盖（ContextVar，线程/任务间互不影响）
_VOLUME_OVERRIDE: ContextVar[int | None] = ContextVar("litematic_volume_limit", default=None)


def _volume_limit() -> int:
    value = _VOLUME_OVERRIDE.get()
    return int(value) if value else MAX_VOLUME

#: NBT 嵌套最大深度（Compound/List 层数）
MAX_NBT_DEPTH = 64

#: 单个 NBT List 的最大元素个数
MAX_NBT_LIST_LEN = 8_000_000

#: 单个 NBT ByteArray 的最大字节数（varint 方块数据可能比体积大若干倍）
MAX_NBT_BYTE_ARRAY_LEN = 64_000_000

#: NBT IntArray / LongArray 的最大元素个数
MAX_NBT_NUM_ARRAY_LEN = 8_000_000

#: 压缩数据解压后的最大字节数（gzip 炸弹防护）
MAX_DECOMPRESSED_BYTES = 128 * 1024 * 1024

#: 空气方块名集合（统一成 minecraft: 前缀后）
AIR_BLOCK_NAMES = frozenset(
    {
        "minecraft:air",
        "minecraft:cave_air",
        "minecraft:void_air",
        # Axiom 蓝图用 structure_void 作为“未保存空气/空位”的占位，视觉上也不可见，
        # 统一按空气处理（对 .nbt 等格式里的 structure_void 同样适用）。
        "minecraft:structure_void",
    }
)

#: 64 位无符号掩码
_MASK64 = (1 << 64) - 1

# NBT 标签类型常量
_TAG_END = 0
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


class SchematicParseError(Exception):
    """文件损坏 / 格式不支持 / 超限时抛出，消息可直接展示给用户（中文）。"""


@dataclass
class ParsedSchematic:
    """解析结果：调色板 + 扁平方块数组。

    ``blocks`` 的索引公式为 ``idx = (y * length + z) * width + x``，
    值是 ``palette`` 的下标；0 号调色板恒为 ``minecraft:air``（但调用方不应依赖这一点，
    契约只保证空气名统一为 ``minecraft:air``）。

    ``palette_states`` 与 ``palette`` 等长：第 i 项是第 i 个调色板条目的方块状态
    Properties（如 ``{"facing": "north", "half": "top"}``），无属性时为空 dict。
    v1 之前的调用方不传该字段时默认为空列表（向后兼容；解析器产出的结果总是等长填充）。
    """

    width: int
    height: int
    length: int
    palette: list[str]
    blocks: array
    format: str
    name: str = ""
    author: str = ""
    description: str = ""
    data_version: int = 0
    total_blocks: int = 0
    #: 与 palette 等长的方块状态属性列表（新增可选字段，向后兼容）
    palette_states: list[dict[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 小工具：压缩识别 / NBT 读取
# --------------------------------------------------------------------------- #


def _extension(filename: str) -> str:
    """从文件名取小写扩展名（含点）；没有扩展名返回空串。"""
    if not filename:
        return ""
    base = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
    dot = base.rfind(".")
    if dot <= 0:
        return ""
    return base[dot:].lower()


def _is_zlib_header(data: bytes) -> bool:
    """判断是否 zlib 流：CMF 低 4 位为 8（deflate），且前两字节组成的 16 位整数能被 31 整除。"""
    if len(data) < 2:
        return False
    cmf, flg = data[0], data[1]
    return (cmf & 0x0F) == 8 and ((cmf << 8) | flg) % 31 == 0


def _decompress(data: bytes) -> bytes:
    """自动识别 gzip / zlib / 未压缩，返回 NBT 字节。"""
    if not data:
        raise SchematicParseError("文件为空，无法解析。")
    if data[:2] == b"\x1f\x8b":
        return _bounded_decompress(data, 16 + zlib.MAX_WBITS, "gzip")
    if _is_zlib_header(data):
        return _bounded_decompress(data, zlib.MAX_WBITS, "zlib")
    return data


def _bounded_decompress(data: bytes, wbits: int, kind: str) -> bytes:
    """带输出长度上限的 zlib/gzip 解压。"""
    decompressor = zlib.decompressobj(wbits)
    try:
        out = decompressor.decompress(data, MAX_DECOMPRESSED_BYTES + 1)
    except zlib.error as exc:
        raise SchematicParseError(f"压缩数据损坏（{kind}）：{exc}") from None
    if len(out) > MAX_DECOMPRESSED_BYTES:
        raise SchematicParseError(
            f"解压后的数据超过安全上限（{MAX_DECOMPRESSED_BYTES // (1024 * 1024)} MiB），"
            "已拒绝解析该文件。"
        )
    if decompressor.unconsumed_tail:
        raise SchematicParseError(
            f"解压后的数据超过安全上限（{MAX_DECOMPRESSED_BYTES // (1024 * 1024)} MiB），"
            "已拒绝解析该文件。"
        )
    if not decompressor.eof:
        raise SchematicParseError(f"压缩数据不完整（{kind}），文件可能已损坏或被截断。")
    return out


class _NBTReader:
    """最小但严谨的 NBT 读取器（大端序，Java 版）。

    - 返回原生类型：Compound -> dict；List -> list；ByteArray -> bytes；
      IntArray -> tuple[int, ...]；LongArray -> array('Q')（无符号 64 位）；
      String -> str；其余数值 -> int/float。
    - 限深，限制 List / 数组元素个数；任何越界都转成中文错误。
    """

    __slots__ = ("_data", "_pos", "_size")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self._size = len(data)

    # -- 基础读取 ---------------------------------------------------------- #

    def _need(self, count: int) -> None:
        if self._pos + count > self._size:
            raise SchematicParseError("NBT 数据不完整：文件被截断或已损坏。")

    def _read_u8(self) -> int:
        if self._pos >= self._size:
            raise SchematicParseError("NBT 数据不完整：文件被截断或已损坏。")
        value = self._data[self._pos]
        self._pos += 1
        return value

    def _read_i16(self) -> int:
        self._need(2)
        value = struct.unpack_from(">h", self._data, self._pos)[0]
        self._pos += 2
        return value

    def _read_u16(self) -> int:
        self._need(2)
        value = struct.unpack_from(">H", self._data, self._pos)[0]
        self._pos += 2
        return value

    def _read_i32(self) -> int:
        self._need(4)
        value = struct.unpack_from(">i", self._data, self._pos)[0]
        self._pos += 4
        return value

    def _read_i64(self) -> int:
        self._need(8)
        value = struct.unpack_from(">q", self._data, self._pos)[0]
        self._pos += 8
        return value

    def _read_string(self) -> str:
        length = self._read_u16()
        self._need(length)
        raw = self._data[self._pos : self._pos + length]
        self._pos += length
        # Java 的“修改版 UTF-8”与标准 UTF-8 在 BMP 内兼容，异常字节用 replace 容错
        return raw.decode("utf-8", "replace")

    # -- 公开入口 ---------------------------------------------------------- #

    def read_root(self) -> tuple[str, dict]:
        """读取根标签；根必须是 Compound，否则给出明确错误。"""
        tag_type = self._read_u8()
        if tag_type != _TAG_COMPOUND:
            raise SchematicParseError(
                "不是有效的 NBT 文件：根标签应为 Compound（类型 10），"
                f"实际读到类型 {tag_type}。"
            )
        name = self._read_string()
        value = self._read_compound(1)
        return name, value

    # -- 递归读取 ---------------------------------------------------------- #

    def _read_tag_payload(self, tag_type: int, depth: int) -> Any:
        if tag_type == _TAG_BYTE:
            self._need(1)
            value = struct.unpack_from(">b", self._data, self._pos)[0]
            self._pos += 1
            return value
        if tag_type == _TAG_SHORT:
            return self._read_i16()
        if tag_type == _TAG_INT:
            return self._read_i32()
        if tag_type == _TAG_LONG:
            return self._read_i64()
        if tag_type == _TAG_FLOAT:
            self._need(4)
            value = struct.unpack_from(">f", self._data, self._pos)[0]
            self._pos += 4
            return value
        if tag_type == _TAG_DOUBLE:
            self._need(8)
            value = struct.unpack_from(">d", self._data, self._pos)[0]
            self._pos += 8
            return value
        if tag_type == _TAG_BYTE_ARRAY:
            length = self._read_i32()
            if length < 0 or length > MAX_NBT_BYTE_ARRAY_LEN:
                raise SchematicParseError(f"NBT 数据损坏：ByteArray 长度非法（{length}）。")
            self._need(length)
            raw = self._data[self._pos : self._pos + length]
            self._pos += length
            return raw
        if tag_type == _TAG_STRING:
            return self._read_string()
        if tag_type == _TAG_LIST:
            return self._read_list(depth)
        if tag_type == _TAG_COMPOUND:
            if depth + 1 > MAX_NBT_DEPTH:
                raise SchematicParseError(f"NBT 嵌套层数超过安全上限（{MAX_NBT_DEPTH} 层）。")
            return self._read_compound(depth + 1)
        if tag_type == _TAG_INT_ARRAY:
            length = self._read_i32()
            if length < 0 or length > MAX_NBT_NUM_ARRAY_LEN:
                raise SchematicParseError(f"NBT 数据损坏：IntArray 长度非法（{length}）。")
            self._need(length * 4)
            values = struct.unpack_from(">%di" % length, self._data, self._pos) if length else ()
            self._pos += length * 4
            return values
        if tag_type == _TAG_LONG_ARRAY:
            length = self._read_i32()
            if length < 0 or length > MAX_NBT_NUM_ARRAY_LEN:
                raise SchematicParseError(f"NBT 数据损坏：LongArray 长度非法（{length}）。")
            self._need(length * 8)
            raw = self._data[self._pos : self._pos + length * 8]
            self._pos += length * 8
            values = array("Q")
            if length:
                values.frombytes(raw)
                # NBT 是大端；array('Q') 用本机字节序，需转换
                if struct.pack("=H", 1) != struct.pack(">H", 1):
                    values.byteswap()
            return values
        raise SchematicParseError(f"不是有效的 NBT 文件：未知标签类型 {tag_type}。")

    def _read_compound(self, depth: int) -> dict:
        result: dict[str, Any] = {}
        while True:
            tag_type = self._read_u8()
            if tag_type == _TAG_END:
                return result
            name = self._read_string()
            result[name] = self._read_tag_payload(tag_type, depth)

    def _read_list(self, depth: int) -> list:
        element_type = self._read_u8()
        length = self._read_i32()
        if length < 0 or length > MAX_NBT_LIST_LEN:
            raise SchematicParseError(f"NBT 数据损坏：List 长度非法（{length}）。")
        if length and element_type == _TAG_END:
            raise SchematicParseError("NBT 数据损坏：非空 List 的元素类型不能是 END。")
        result: list[Any] = []
        append = result.append
        for _ in range(length):
            append(self._read_tag_payload(element_type, depth))
        return result


def _parse_nbt(data: bytes) -> tuple[str, dict]:
    """解析（已解压的）NBT 字节，返回根名称与根 Compound。"""
    if len(data) < 3:
        raise SchematicParseError("数据太短，不是有效的 NBT 文件。")
    return _NBTReader(data).read_root()


# --------------------------------------------------------------------------- #
# 通用辅助：方块名、调色板、维度校验、向量读取
# --------------------------------------------------------------------------- #


def _as_int(value: Any, default: int | None = None) -> int:
    """把 NBT 数值转成 int；失败返回 default（default 为 None 时抛错由调用方处理）。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if default is None:
        raise SchematicParseError("NBT 数据损坏：期望整数字段。")
    return default


def _as_str(value: Any, default: str = "") -> str:
    """把 NBT 字符串转成 str（非字符串返回 default）。"""
    if isinstance(value, str):
        return value
    return default


def _normalize_block_name(raw: Any) -> str:
    """把任意写法的方块名统一成 ``minecraft:xxx``（去掉方括号状态、补命名空间）。"""
    return _parse_block_state(raw)[0]


def _coerce_properties(raw: Any) -> dict[str, str]:
    """把 NBT 里的 Properties Compound 转成 ``dict[str, str]``（容错处理非字符串值）。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SchematicParseError("NBT 数据损坏：方块状态的 Properties 不是 Compound。")
    properties: dict[str, str] = {}
    for key, value in raw.items():
        properties[str(key)] = value if isinstance(value, str) else str(value)
    return properties


def _parse_block_state(raw: Any) -> tuple[str, dict[str, str]]:
    """把方块状态字符串解析成 ``(统一命名空间的名字, Properties dict)``。

    支持 Sponge 的 ``minecraft:oak_stairs[facing=east,half=bottom]`` 写法；
    没有方括号状态时返回空 dict。名字统一成 ``minecraft:xxx`` 形式。
    """
    if not isinstance(raw, str):
        raise SchematicParseError("NBT 数据损坏：调色板中的方块名不是字符串。")
    state = raw.strip()
    if state.startswith("#"):
        # Sponge 规范里 # 前缀表示 tag，这里去掉 # 当普通名称处理
        state = state[1:]
    properties: dict[str, str] = {}
    bracket = state.find("[")
    if bracket >= 0:
        closing = state.rfind("]")
        property_text = state[bracket + 1 : closing if closing > bracket else len(state)]
        state = state[:bracket]
        for part in property_text.split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, value = part.split("=", 1)
            properties[key.strip()] = value.strip()
    name = state.strip()
    if not name:
        raise SchematicParseError("NBT 数据损坏：调色板中存在空方块名。")
    if ":" not in name:
        name = "minecraft:" + name
    return name, properties


def _is_air(name: str) -> bool:
    return name in AIR_BLOCK_NAMES


class _PaletteBuilder:
    """收集输出调色板：0 号固定为 minecraft:air，其余按首次出现顺序追加。

    ``states`` 与 ``names`` 等长，保存每个调色板条目的方块状态 Properties（无属性为 {}）。
    """

    __slots__ = ("names", "states", "_lookup")

    def __init__(self) -> None:
        self.names: list[str] = ["minecraft:air"]
        self.states: list[dict[str, str]] = [{}]
        self._lookup: dict[str, int] = {"minecraft:air": 0}

    def index(self, name: str, properties: dict[str, str] | None = None) -> int:
        """返回方块名在输出调色板中的下标；空气统一映射到 0。

        ``properties`` 只在首次加入该方块名时记录（同一名字只保留第一次出现的状态）。
        """
        if name in AIR_BLOCK_NAMES:
            return 0
        index = self._lookup.get(name)
        if index is None:
            index = len(self.names)
            self.names.append(name)
            self.states.append(dict(properties) if properties else {})
            self._lookup[name] = index
        return index


def _validate_volume(width: int, height: int, length: int, fmt_name: str) -> int:
    """校验单个轴尺寸与总体积，返回体积。超限抛中文错误。"""
    for axis, value in (("X", width), ("Y", height), ("Z", length)):
        if value < 0:
            raise SchematicParseError(f"{fmt_name} 文件损坏：{axis} 轴尺寸为负数（{value}）。")
        if value > MAX_AXIS:
            raise SchematicParseError(
                f"{fmt_name} 文件尺寸超过安全上限：{axis} 轴 {value} 格 > {MAX_AXIS} 格，已拒绝解析。"
            )
    volume = width * height * length
    if volume > _volume_limit():
        raise SchematicParseError(
            f"{fmt_name} 文件包围盒体积过大：{width}×{height}×{length} = {volume:,} 格，"
            f"超过安全上限 {_volume_limit():,} 格，已拒绝解析。"
        )
    return volume


def _read_vec3_compound(parent: dict, key: str, fmt_name: str) -> tuple[int, int, int]:
    """读取 ${key} 下 x/y/z 整数（Litematica 的 Position/Size 写法）。"""
    value = parent.get(key)
    if not isinstance(value, dict):
        raise SchematicParseError(f"{fmt_name} 文件损坏：缺少 {key} 字段或其类型不是 Compound。")
    result = []
    for axis in ("x", "y", "z"):
        raw = value.get(axis)
        if not isinstance(raw, int):
            raise SchematicParseError(f"{fmt_name} 文件损坏：{key}.{axis} 不是整数。")
        result.append(raw)
    return result[0], result[1], result[2]


def _read_compound(parent: dict, key: str, fmt_name: str) -> dict:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise SchematicParseError(f"{fmt_name} 文件损坏：缺少 {key} 字段或其类型不是 Compound。")
    return value


def _read_list(parent: dict, key: str, fmt_name: str) -> list:
    value = parent.get(key)
    if not isinstance(value, list):
        raise SchematicParseError(f"{fmt_name} 文件损坏：缺少 {key} 字段或其类型不是 List。")
    return value


def _read_required_int(parent: dict, key: str, fmt_name: str) -> int:
    raw = parent.get(key)
    if not isinstance(raw, int):
        raise SchematicParseError(f"{fmt_name} 文件损坏：缺少 {key} 字段或其类型不是整数。")
    return raw


def _read_int_list_3(raw: Any, field_name: str, fmt_name: str) -> tuple[int, int, int]:
    """读取 3 个整数的数组/列表（.nbt 结构文件的 size / pos 字段）。"""
    if not isinstance(raw, (tuple, list)):
        raise SchematicParseError(f"{fmt_name} 文件损坏：{field_name} 不是 3 个整数的列表。")
    if len(raw) != 3:
        raise SchematicParseError(f"{fmt_name} 文件损坏：{field_name} 长度应为 3，实际为 {len(raw)}。")
    values = []
    for item in raw:
        if not isinstance(item, int):
            raise SchematicParseError(f"{fmt_name} 文件损坏：{field_name} 中包含非整数值。")
        values.append(item)
    return values[0], values[1], values[2]


def _empty_result(fmt: str, name: str = "", author: str = "", description: str = "",
                  data_version: int = 0) -> ParsedSchematic:
    """所有方块都是空气时的结果（0×0×0）。"""
    return ParsedSchematic(
        width=0, height=0, length=0,
        palette=["minecraft:air"], blocks=array("i"), format=fmt,
        name=name, author=author, description=description,
        data_version=data_version, total_blocks=0,
        palette_states=[{}],
    )


# --------------------------------------------------------------------------- #
# .litematic 解析
# --------------------------------------------------------------------------- #


def _unpack_litematic_block_states(longs: array, volume: int, bits: int) -> array:
    """把 Litematica 的连续位流解包成调色板索引数组。

    第 i 个值位于整条位流的第 ``i * bits`` 位，可能跨越 long 边界
    （malilib ``LitematicaBitArray`` 的实际语义）。
    """
    if bits <= 8:
        typecode = "B"
    elif bits <= 16:
        typecode = "H"
    else:
        typecode = "i"
    result = array(typecode, [0]) * volume
    mask = (1 << bits) - 1
    # 局部变量加速（大文件上千万次循环）
    out = result
    arr = longs
    bits_local = bits
    mask_local = mask
    bitpos = 0
    long_index = 0
    shift_limit = 64 - bits_local
    for i in range(volume):
        shift = bitpos & 63
        if shift > shift_limit:
            # 跨越 long 边界：低段来自当前 long，高段来自下一个 long
            out[i] = ((arr[long_index] >> shift) | (arr[long_index + 1] << (64 - shift))) & mask_local
        else:
            out[i] = (arr[long_index] >> shift) & mask_local
        bitpos += bits_local
        long_index = bitpos >> 6
    return result


def _parse_litematic(root: dict) -> ParsedSchematic:
    fmt = "litematic"
    metadata = root.get("Metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    name = _as_str(metadata.get("Name"))
    author = _as_str(metadata.get("Author"))
    description = _as_str(metadata.get("Description"))
    data_version = _as_int(root.get("MinecraftDataVersion"), 0)

    regions = root.get("Regions")
    if not isinstance(regions, dict):
        raise SchematicParseError("litematic 文件损坏：缺少 Regions 字段。")
    if not regions:
        raise SchematicParseError("litematic 文件不包含任何区域（Regions 为空）。")

    builder = _PaletteBuilder()
    decoded_regions: list[tuple] = []  # (pos, size, abs_size, values, remap, palette_len)
    bbox_min = [None, None, None]
    bbox_max = [None, None, None]
    total_blocks = 0
    total_region_volume = 0

    for region_name, region in regions.items():
        if not isinstance(region, dict):
            raise SchematicParseError(f"litematic 文件损坏：区域 {region_name!r} 不是 Compound。")
        position = _read_vec3_compound(region, "Position", fmt)
        size = _read_vec3_compound(region, "Size", fmt)
        if size[0] == 0 or size[1] == 0 or size[2] == 0:
            raise SchematicParseError(
                f"litematic 文件损坏：区域 {region_name!r} 的 Size 存在 0 分量 {size}。"
            )
        abs_size = (abs(size[0]), abs(size[1]), abs(size[2]))
        for axis_name, axis_value in zip(("X", "Y", "Z"), abs_size):
            if axis_value > MAX_AXIS:
                raise SchematicParseError(
                    f"litematic 区域 {region_name!r} 尺寸超过安全上限："
                    f"{axis_name} 轴 {axis_value} 格 > {MAX_AXIS} 格，已拒绝解析。"
                )
        region_volume = abs_size[0] * abs_size[1] * abs_size[2]
        if region_volume > _volume_limit():
            raise SchematicParseError(
                f"litematic 区域 {region_name!r} 体积过大："
                f"{abs_size[0]}×{abs_size[1]}×{abs_size[2]} = {region_volume:,} 格，"
                f"超过安全上限 {_volume_limit():,} 格，已拒绝解析。"
            )
        total_region_volume += region_volume
        if total_region_volume > _volume_limit():
            raise SchematicParseError(
                f"litematic 所有区域体积之和过大（超过 {_volume_limit():,} 格），已拒绝解析。"
            )

        palette_entries = _read_list(region, "BlockStatePalette", fmt)
        if not palette_entries:
            raise SchematicParseError(f"litematic 文件损坏：区域 {region_name!r} 的调色板为空。")
        palette_names: list[str] = []
        palette_states: list[dict[str, str]] = []
        for entry in palette_entries:
            if isinstance(entry, dict):
                raw_name = entry.get("Name")
                raw_properties = entry.get("Properties")
            elif isinstance(entry, str):
                raw_name = entry
                raw_properties = None
            else:
                raise SchematicParseError(
                    f"litematic 文件损坏：区域 {region_name!r} 的调色板条目类型非法。"
                )
            block_name, inline_properties = _parse_block_state(raw_name)
            # Litematic 的状态属性写在 Properties Compound 里；极少数文件可能写在 Name 里
            properties = _coerce_properties(raw_properties)
            if inline_properties:
                properties = {**inline_properties, **properties}
            palette_names.append(block_name)
            palette_states.append(properties)

        # 每个调色板索引 -> 输出调色板索引（空气为 0）
        remap = array("i", [0]) * len(palette_names)
        for index, block_name in enumerate(palette_names):
            remap[index] = builder.index(block_name, palette_states[index])
        air_flags = bytearray(1 if _is_air(n) else 0 for n in palette_names)

        bits = max(2, (len(palette_names) - 1).bit_length())
        block_states = region.get("BlockStates")
        if not isinstance(block_states, array) or block_states.typecode != "Q":
            raise SchematicParseError(
                f"litematic 文件损坏：区域 {region_name!r} 的 BlockStates 不是 LongArray。"
            )
        expected_longs = (region_volume * bits + 63) // 64
        if len(block_states) < expected_longs:
            raise SchematicParseError(
                f"litematic 文件损坏：区域 {region_name!r} 的 BlockStates 长度不足"
                f"（需要 {expected_longs} 个 long，实际 {len(block_states)}）。"
            )
        values = _unpack_litematic_block_states(block_states, region_volume, bits)

        # 第一遍：解码后计算“非空气方块”的世界坐标包围盒
        pos_x, pos_y, pos_z = position
        size_x, size_y, size_z = size
        ax, ay, az = abs_size
        palette_len = len(palette_names)
        if air_flags[0]:
            for index in range(region_volume):
                value = values[index]
                if value >= palette_len:
                    raise SchematicParseError(
                        f"litematic 文件损坏：区域 {region_name!r} 出现越界调色板索引 {value}"
                        f"（调色板大小 {palette_len}）。"
                    )
                if value == 0 or air_flags[value]:
                    continue
                local_x = index % ax
                rest = index // ax
                local_z = rest % az
                local_y = rest // az
                world_x = pos_x + (local_x if size_x > 0 else local_x + size_x + 1)
                world_y = pos_y + (local_y if size_y > 0 else local_y + size_y + 1)
                world_z = pos_z + (local_z if size_z > 0 else local_z + size_z + 1)
                total_blocks += 1
                if bbox_min[0] is None or world_x < bbox_min[0]:
                    bbox_min[0] = world_x
                if bbox_min[1] is None or world_y < bbox_min[1]:
                    bbox_min[1] = world_y
                if bbox_min[2] is None or world_z < bbox_min[2]:
                    bbox_min[2] = world_z
                if bbox_max[0] is None or world_x > bbox_max[0]:
                    bbox_max[0] = world_x
                if bbox_max[1] is None or world_y > bbox_max[1]:
                    bbox_max[1] = world_y
                if bbox_max[2] is None or world_z > bbox_max[2]:
                    bbox_max[2] = world_z
        else:
            for index in range(region_volume):
                value = values[index]
                if value >= palette_len:
                    raise SchematicParseError(
                        f"litematic 文件损坏：区域 {region_name!r} 出现越界调色板索引 {value}"
                        f"（调色板大小 {palette_len}）。"
                    )
                if air_flags[value]:
                    continue
                local_x = index % ax
                rest = index // ax
                local_z = rest % az
                local_y = rest // az
                world_x = pos_x + (local_x if size_x > 0 else local_x + size_x + 1)
                world_y = pos_y + (local_y if size_y > 0 else local_y + size_y + 1)
                world_z = pos_z + (local_z if size_z > 0 else local_z + size_z + 1)
                total_blocks += 1
                if bbox_min[0] is None or world_x < bbox_min[0]:
                    bbox_min[0] = world_x
                if bbox_min[1] is None or world_y < bbox_min[1]:
                    bbox_min[1] = world_y
                if bbox_min[2] is None or world_z < bbox_min[2]:
                    bbox_min[2] = world_z
                if bbox_max[0] is None or world_x > bbox_max[0]:
                    bbox_max[0] = world_x
                if bbox_max[1] is None or world_y > bbox_max[1]:
                    bbox_max[1] = world_y
                if bbox_max[2] is None or world_z > bbox_max[2]:
                    bbox_max[2] = world_z

        decoded_regions.append((position, size, abs_size, values, remap, palette_len))

    if bbox_min[0] is None:
        # 全是空气
        return _empty_result(fmt, name, author, description, data_version)

    width = bbox_max[0] - bbox_min[0] + 1
    height = bbox_max[1] - bbox_min[1] + 1
    length = bbox_max[2] - bbox_min[2] + 1
    volume = _validate_volume(width, height, length, fmt)
    min_x, min_y, min_z = bbox_min
    out_blocks = array("i", [0]) * volume

    # 第二遍：把每个区域的非空气方块写入输出数组（重叠时后写入的区域优先）
    out_width = width
    out_length = length
    for position, size, abs_size, values, remap, palette_len in decoded_regions:
        pos_x, pos_y, pos_z = position
        size_x, size_y, size_z = size
        ax, ay, az = abs_size
        for index, value in enumerate(values):
            out_index = remap[value]
            if out_index == 0:
                continue
            local_x = index % ax
            rest = index // ax
            local_z = rest % az
            local_y = rest // az
            world_x = pos_x + (local_x if size_x > 0 else local_x + size_x + 1)
            world_y = pos_y + (local_y if size_y > 0 else local_y + size_y + 1)
            world_z = pos_z + (local_z if size_z > 0 else local_z + size_z + 1)
            target = ((world_y - min_y) * out_length + (world_z - min_z)) * out_width + (world_x - min_x)
            out_blocks[target] = out_index

    return ParsedSchematic(
        width=width, height=height, length=length,
        palette=builder.names, blocks=out_blocks, format=fmt,
        name=name, author=author, description=description,
        data_version=data_version, total_blocks=total_blocks,
        palette_states=builder.states,
    )


# --------------------------------------------------------------------------- #
# .schem（Sponge Schematic v1/v2/v3）解析
# --------------------------------------------------------------------------- #


def _decode_varint_block_data(data: bytes, count: int) -> array:
    """按 varint 解码 Sponge 的方块数据（BlockData/Data）。"""
    result = array("i", [0]) * count
    position = 0
    data_len = len(data)
    for index in range(count):
        value = 0
        shift = 0
        while True:
            if position >= data_len:
                raise SchematicParseError(
                    "schem 文件损坏：方块数据（varint）长度不足，"
                    f"需要 {count} 个值，在第 {index} 个值处提前结束。"
                )
            byte = data[position]
            position += 1
            value |= (byte & 0x7F) << shift
            if byte < 0x80:
                break
            shift += 7
            if shift > 35:
                raise SchematicParseError("schem 文件损坏：方块数据中的 varint 过长。")
        result[index] = value
    return result


def _decode_block_data(data: Any, count: int, fmt_name: str) -> bytes | array:
    """解码 Sponge 的方块数据数组。

    - ``len(data) == count``：每格一个原始字节（部分实现的优化写法）；
    - 否则按官方规范的 varint 流解码。
    """
    if not isinstance(data, (bytes, bytearray)):
        raise SchematicParseError(f"{fmt_name} 文件损坏：方块数据不是 ByteArray。")
    data = bytes(data)
    if len(data) == count:
        return data
    if len(data) > count:
        return _decode_varint_block_data(data, count)
    raise SchematicParseError(
        f"{fmt_name} 文件损坏：方块数据长度 {len(data)} 小于方块总数 {count}。"
    )


def _palette_indices_from_compound(palette: Any, fmt_name: str) -> tuple[list[str], list[dict[str, str]]]:
    """把 Sponge 的 ``{方块状态: 数字索引}`` 调色板转成“索引 -> (名称, Properties)”列表。"""
    if not isinstance(palette, dict):
        raise SchematicParseError(f"{fmt_name} 文件损坏：调色板不是 Compound。")
    if not palette:
        raise SchematicParseError(f"{fmt_name} 文件损坏：调色板为空。")
    entries: dict[int, tuple[str, dict[str, str]]] = {}
    for raw_name, raw_index in palette.items():
        if not isinstance(raw_index, int):
            raise SchematicParseError(f"{fmt_name} 文件损坏：调色板索引不是整数。")
        if raw_index < 0 or raw_index >= MAX_NBT_LIST_LEN:
            raise SchematicParseError(f"{fmt_name} 文件损坏：调色板索引非法（{raw_index}）。")
        # Sponge 的调色板键带方块状态，例如 minecraft:oak_stairs[facing=east]
        entries[raw_index] = _parse_block_state(raw_name)
    count = len(entries)
    names: list[str | None] = [None] * count
    states: list[dict[str, str] | None] = [None] * count
    for index, (block_name, properties) in entries.items():
        if index >= count:
            raise SchematicParseError(
                f"{fmt_name} 文件损坏：调色板索引不连续（最大索引 {index}，条目数 {count}）。"
            )
        names[index] = block_name
        states[index] = properties
    if any(item is None for item in names):
        raise SchematicParseError(f"{fmt_name} 文件损坏：调色板索引不连续。")
    return names, states  # type: ignore[return-value]


def _parse_schem(root: dict) -> ParsedSchematic:
    fmt = "schem"
    # v3 把内容放在 "Schematic" 子 Compound 里；v1/v2 直接在根上
    base = root
    wrapped = root.get("Schematic")
    if isinstance(wrapped, dict) and ("Blocks" in wrapped or "BlockData" in wrapped):
        base = wrapped

    version = _as_int(base.get("Version"), 0)
    data_version = _as_int(base.get("DataVersion"), 0)
    metadata = base.get("Metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    name = _as_str(metadata.get("Name"))
    author = _as_str(metadata.get("Author"))
    description = _as_str(metadata.get("Description"))

    blocks_field = base.get("Blocks")
    v3_blocks: dict | None = None
    if isinstance(blocks_field, dict) and "Palette" in blocks_field:
        v3_blocks = blocks_field

    if v3_blocks is not None:
        # ---- Sponge v3 ----
        width = _read_required_int(base, "Width", fmt)
        height = _read_required_int(base, "Height", fmt)
        length = _read_required_int(base, "Length", fmt)
        palette_src = v3_blocks.get("Palette")
        data = v3_blocks.get("Data")
        if data is None:
            raise SchematicParseError("schem v3 文件损坏：缺少 Blocks.Data 字段。")
        palette_names, palette_states = _palette_indices_from_compound(palette_src, "schem v3")
    elif "BlockData" in base:
        # ---- Sponge v1 / v2 ----
        width = _read_required_int(base, "Width", fmt)
        height = _read_required_int(base, "Height", fmt)
        length = _read_required_int(base, "Length", fmt)
        palettes = base.get("Palettes")
        if isinstance(palettes, list) and palettes:
            palette_src = palettes[0]
        else:
            palette_src = base.get("Palette")
        if palette_src is None:
            raise SchematicParseError("schem 文件损坏：缺少 Palette 或 Palettes 字段。")
        palette_names, palette_states = _palette_indices_from_compound(
            palette_src, "schem v" + str(version or 2)
        )
        data = base.get("BlockData")
    else:
        raise SchematicParseError(
            "schem 文件损坏：既没有 v3 的 Blocks 字段，也没有 v1/v2 的 BlockData 字段。"
        )

    # 维度按无符号 short 解释（v3 的 WorldEdit 读端即如此；v1/v2 正常文件也是正数）
    width &= 0xFFFF
    height &= 0xFFFF
    length &= 0xFFFF
    volume = _validate_volume(width, height, length, fmt)
    if volume == 0:
        return _empty_result(fmt, name, author, description, data_version)

    # PaletteMax 若存在则必须与调色板条目数一致（WorldEdit 读端的校验）
    palette_max = base.get("PaletteMax")
    if isinstance(palette_max, int) and v3_blocks is None and palette_max != len(palette_names):
        raise SchematicParseError(
            f"schem 文件损坏：PaletteMax={palette_max} 与实际调色板条目数 {len(palette_names)} 不一致。"
        )

    ids = _decode_block_data(data, volume, fmt)
    palette_len = len(palette_names)

    builder = _PaletteBuilder()
    remap = array("i", [0]) * palette_len
    for index, block_name in enumerate(palette_names):
        remap[index] = builder.index(block_name, palette_states[index])
    air_flags = bytearray(1 if _is_air(n) else 0 for n in palette_names)

    # 第一遍：找非空气方块包围盒
    bbox_min = [None, None, None]
    bbox_max = [None, None, None]
    total_blocks = 0
    for index in range(volume):
        value = ids[index]
        if value >= palette_len:
            raise SchematicParseError(
                f"schem 文件损坏：出现越界调色板索引 {value}（调色板大小 {palette_len}）。"
            )
        if air_flags[value]:
            continue
        total_blocks += 1
        x = index % width
        rest = index // width
        z = rest % length
        y = rest // length
        if bbox_min[0] is None or x < bbox_min[0]:
            bbox_min[0] = x
        if bbox_min[1] is None or y < bbox_min[1]:
            bbox_min[1] = y
        if bbox_min[2] is None or z < bbox_min[2]:
            bbox_min[2] = z
        if bbox_max[0] is None or x > bbox_max[0]:
            bbox_max[0] = x
        if bbox_max[1] is None or y > bbox_max[1]:
            bbox_max[1] = y
        if bbox_max[2] is None or z > bbox_max[2]:
            bbox_max[2] = z

    if bbox_min[0] is None:
        return _empty_result(fmt, name, author, description, data_version)

    out_width = bbox_max[0] - bbox_min[0] + 1
    out_height = bbox_max[1] - bbox_min[1] + 1
    out_length = bbox_max[2] - bbox_min[2] + 1
    out_volume = _validate_volume(out_width, out_height, out_length, fmt)
    out_blocks = array("i", [0]) * out_volume
    min_x, min_y, min_z = bbox_min

    # 第二遍：写入输出数组
    for index in range(volume):
        value = ids[index]
        if air_flags[value]:
            continue
        x = index % width
        rest = index // width
        z = rest % length
        y = rest // length
        target = ((y - min_y) * out_length + (z - min_z)) * out_width + (x - min_x)
        out_blocks[target] = remap[value]

    return ParsedSchematic(
        width=out_width, height=out_height, length=out_length,
        palette=builder.names, blocks=out_blocks, format=fmt,
        name=name, author=author, description=description,
        data_version=data_version, total_blocks=total_blocks,
        palette_states=builder.states,
    )


# --------------------------------------------------------------------------- #
# .schematic（MCEdit 旧格式）解析
# --------------------------------------------------------------------------- #


def _nibble(data: bytes, index: int) -> int:
    """取 nibble 数组第 index 个 4 位值（低 4 位在前）。"""
    byte = data[index >> 1]
    if index & 1:
        return (byte >> 4) & 0xF
    return byte & 0xF


def _parse_mcedit_schematic(root: dict) -> ParsedSchematic:
    fmt = "schematic"

    width = _read_required_int(root, "Width", fmt)
    height = _read_required_int(root, "Height", fmt)
    length = _read_required_int(root, "Length", fmt)
    # MCEdit 的宽高长是 signed short，正常文件都是正数
    volume = _validate_volume(width, height, length, fmt)
    if volume == 0:
        return _empty_result(fmt)

    blocks_data = root.get("Blocks")
    if not isinstance(blocks_data, (bytes, bytearray)):
        raise SchematicParseError("schematic 文件损坏：缺少 Blocks 字段或其类型不是 ByteArray。")
    if len(blocks_data) < volume:
        raise SchematicParseError(
            f"schematic 文件损坏：Blocks 长度 {len(blocks_data)} 小于方块总数 {volume}。"
        )
    blocks_data = bytes(blocks_data[:volume])

    data_data = root.get("Data")
    if data_data is None:
        data_data = b""
    elif not isinstance(data_data, (bytes, bytearray)):
        raise SchematicParseError("schematic 文件损坏：Data 字段类型不是 ByteArray。")
    else:
        data_data = bytes(data_data)

    add_data = root.get("AddBlocks")
    if add_data is not None and not isinstance(add_data, (bytes, bytearray)):
        raise SchematicParseError("schematic 文件损坏：AddBlocks 字段类型不是 ByteArray。")
    add_data = bytes(add_data) if add_data is not None else b""

    # 检查 nibble 数组长度（体积为奇数时最后一个字节只用到低 4 位）
    if data_data and len(data_data) * 2 < volume:
        raise SchematicParseError(
            f"schematic 文件损坏：Data 长度 {len(data_data)} 不足以覆盖 {volume} 个方块。"
        )
    if add_data and len(add_data) * 2 < volume:
        raise SchematicParseError(
            f"schematic 文件损坏：AddBlocks 长度 {len(add_data)} 不足以覆盖 {volume} 个方块。"
        )

    builder = _PaletteBuilder()
    bbox_min = [None, None, None]
    bbox_max = [None, None, None]
    total_blocks = 0
    for index in range(volume):
        block_id = blocks_data[index]
        meta = _nibble(data_data, index) if data_data else 0
        if add_data:
            block_id |= _nibble(add_data, index) << 8
        block_name = _legacy_ids.legacy_block_name(block_id, meta)
        if _is_air(block_name):
            continue
        total_blocks += 1
        x = index % width
        rest = index // width
        z = rest % length
        y = rest // length
        if bbox_min[0] is None or x < bbox_min[0]:
            bbox_min[0] = x
        if bbox_min[1] is None or y < bbox_min[1]:
            bbox_min[1] = y
        if bbox_min[2] is None or z < bbox_min[2]:
            bbox_min[2] = z
        if bbox_max[0] is None or x > bbox_max[0]:
            bbox_max[0] = x
        if bbox_max[1] is None or y > bbox_max[1]:
            bbox_max[1] = y
        if bbox_max[2] is None or z > bbox_max[2]:
            bbox_max[2] = z

    if bbox_min[0] is None:
        return _empty_result(fmt)

    out_width = bbox_max[0] - bbox_min[0] + 1
    out_height = bbox_max[1] - bbox_min[1] + 1
    out_length = bbox_max[2] - bbox_min[2] + 1
    out_volume = _validate_volume(out_width, out_height, out_length, fmt)
    out_blocks = array("i", [0]) * out_volume
    min_x, min_y, min_z = bbox_min

    for index in range(volume):
        block_id = blocks_data[index]
        meta = _nibble(data_data, index) if data_data else 0
        if add_data:
            block_id |= _nibble(add_data, index) << 8
        block_name = _legacy_ids.legacy_block_name(block_id, meta)
        if _is_air(block_name):
            continue
        x = index % width
        rest = index // width
        z = rest % length
        y = rest // length
        target = ((y - min_y) * out_length + (z - min_z)) * out_width + (x - min_x)
        out_blocks[target] = builder.index(block_name)

    return ParsedSchematic(
        width=out_width, height=out_height, length=out_length,
        palette=builder.names, blocks=out_blocks, format=fmt,
        total_blocks=total_blocks,
        palette_states=builder.states,
    )


# --------------------------------------------------------------------------- #
# .nbt（原版结构方块结构文件）解析
# --------------------------------------------------------------------------- #


def _structure_block_entry(
    entry: Any, fmt: str, palette_len: int, width: int, height: int, length: int
) -> tuple[tuple[int, int, int], int]:
    """校验并读取结构文件 blocks[] 中的一条记录，返回 (pos, state)。"""
    if not isinstance(entry, dict):
        raise SchematicParseError("结构文件损坏：blocks 中存在非 Compound 条目。")
    position = _read_int_list_3(entry.get("pos"), "blocks[].pos", fmt)
    state = entry.get("state")
    if not isinstance(state, int):
        raise SchematicParseError("结构文件损坏：blocks[].state 不是整数。")
    if state < 0 or state >= palette_len:
        raise SchematicParseError(
            f"结构文件损坏：blocks[].state 超出调色板范围（{state}，调色板大小 {palette_len}）。"
        )
    x, y, z = position
    if not (0 <= x < width and 0 <= y < height and 0 <= z < length):
        raise SchematicParseError(
            f"结构文件损坏：方块坐标 {position} 超出结构尺寸 {width}×{height}×{length}。"
        )
    return position, state


def _parse_structure_nbt(root: dict) -> ParsedSchematic:
    fmt = "structure"
    data_version = _as_int(root.get("DataVersion"), 0)
    size = _read_int_list_3(root.get("size"), "size", fmt)
    width, height, length = size
    volume = _validate_volume(width, height, length, fmt)
    if volume == 0:
        return _empty_result(fmt, data_version=data_version)

    palette_src = root.get("palette")
    if palette_src is None:
        # 1.21 之后的部分结构文件使用 palettes（多调色板），取第一套
        palettes = root.get("palettes")
        if isinstance(palettes, list) and palettes:
            palette_src = palettes[0]
    if not isinstance(palette_src, list) or not palette_src:
        raise SchematicParseError("结构文件损坏：缺少 palette 字段（或为空）。")

    palette_names: list[str] = []
    palette_states: list[dict[str, str]] = []
    for entry in palette_src:
        if not isinstance(entry, dict):
            raise SchematicParseError("结构文件损坏：palette 中存在非 Compound 条目。")
        block_name, inline_properties = _parse_block_state(entry.get("Name"))
        properties = _coerce_properties(entry.get("Properties"))
        if inline_properties:
            properties = {**inline_properties, **properties}
        palette_names.append(block_name)
        palette_states.append(properties)

    builder = _PaletteBuilder()
    remap = array("i", [0]) * len(palette_names)
    for index, block_name in enumerate(palette_names):
        remap[index] = builder.index(block_name, palette_states[index])

    blocks = root.get("blocks")
    if not isinstance(blocks, list):
        raise SchematicParseError("结构文件损坏：缺少 blocks 字段或其类型不是 List。")

    bbox_min = [None, None, None]
    bbox_max = [None, None, None]
    total_blocks = 0
    # 第一遍：校验并计算非空气方块的包围盒（不缓存坐标，避免列表撑爆内存）
    for entry in blocks:
        position, state = _structure_block_entry(entry, fmt, len(palette_names), width, height, length)
        if remap[state] == 0:
            continue
        total_blocks += 1
        x, y, z = position
        if bbox_min[0] is None or x < bbox_min[0]:
            bbox_min[0] = x
        if bbox_min[1] is None or y < bbox_min[1]:
            bbox_min[1] = y
        if bbox_min[2] is None or z < bbox_min[2]:
            bbox_min[2] = z
        if bbox_max[0] is None or x > bbox_max[0]:
            bbox_max[0] = x
        if bbox_max[1] is None or y > bbox_max[1]:
            bbox_max[1] = y
        if bbox_max[2] is None or z > bbox_max[2]:
            bbox_max[2] = z

    if bbox_min[0] is None:
        return _empty_result(fmt, data_version=data_version)

    out_width = bbox_max[0] - bbox_min[0] + 1
    out_height = bbox_max[1] - bbox_min[1] + 1
    out_length = bbox_max[2] - bbox_min[2] + 1
    out_volume = _validate_volume(out_width, out_height, out_length, fmt)
    out_blocks = array("i", [0]) * out_volume
    min_x, min_y, min_z = bbox_min
    # 第二遍：写入非空气方块
    for entry in blocks:
        position, state = _structure_block_entry(entry, fmt, len(palette_names), width, height, length)
        palette_index = remap[state]
        if palette_index == 0:
            continue
        x, y, z = position
        target = ((y - min_y) * out_length + (z - min_z)) * out_width + (x - min_x)
        out_blocks[target] = palette_index

    return ParsedSchematic(
        width=out_width, height=out_height, length=out_length,
        palette=builder.names, blocks=out_blocks, format=fmt,
        data_version=data_version, total_blocks=total_blocks,
        palette_states=builder.states,
    )


# --------------------------------------------------------------------------- #
# Axiom 蓝图（.bp）解析
# --------------------------------------------------------------------------- #


def _read_be_u32(data: bytes, offset: int, field: str) -> int:
    """读取大端 u32（Axiom 蓝图的分段长度前缀）。"""
    if offset < 0 or offset + 4 > len(data):
        raise SchematicParseError(f"Axiom 蓝图文件损坏：读取 {field} 长度时数据不足。")
    return struct.unpack_from(">I", data, offset)[0]


def _axiom_long_array(raw: Any, fmt: str) -> array:
    """把 NBT LongArray 统一成无符号 64 位数组（array('Q')）。"""
    if isinstance(raw, array):
        if raw.typecode == "Q":
            return raw
        if raw.typecode in ("q", "L", "I"):
            return array("Q", (int(value) & _MASK64 for value in raw))
        raise SchematicParseError(f"{fmt} 文件损坏：分块方块数据不是 LongArray。")
    if isinstance(raw, (list, tuple)):
        try:
            return array("Q", (int(value) & _MASK64 for value in raw))
        except (TypeError, ValueError, OverflowError):
            raise SchematicParseError(f"{fmt} 文件损坏：分块方块数据不是 LongArray。") from None
    raise SchematicParseError(f"{fmt} 文件损坏：缺少分块方块数据 data 或其类型错误。")


def _decode_axiom_indices(longs: array, bits: int, mode: str) -> array:
    """把 Axiom 蓝图一个 16×16×16 分块解包成 4096 个调色板索引。

    ``mode="contiguous"``：Minecraft 1.16+ 连续位流（第 i 个值位于第 ``i*bits`` 位，
    可跨 64 位 long 边界，低比特优先）。
    ``mode="padded"``：每个 long 只放 ``64 // bits`` 个值、值不跨 long 边界
    （兼容少量旧式/自定义打包）。
    """
    total = _AXIOM_BP_CHUNK_BLOCKS
    mask = (1 << bits) - 1
    values = array("H")

    if mode == "padded":
        per_long = 64 // bits
        needed = (total + per_long - 1) // per_long
        if len(longs) < needed:
            raise SchematicParseError(
                f"Axiom 蓝图文件损坏：分块数据只有 {len(longs)} 个 long，"
                f"不足以容纳 {total} 个方块。"
            )
        for index in range(total):
            long_index, slot = divmod(index, per_long)
            values.append((int(longs[long_index]) >> (slot * bits)) & mask)
        return values

    needed = (total * bits + 63) // 64
    if len(longs) < needed:
        raise SchematicParseError(
            f"Axiom 蓝图文件损坏：分块数据只有 {len(longs)} 个 long，"
            f"不足以容纳 {total} 个方块（需要 {needed} 个）。"
        )
    for index in range(total):
        bit_offset = index * bits
        long_index = bit_offset >> 6
        shift = bit_offset & 63
        value = int(longs[long_index]) >> shift
        if shift + bits > 64:
            if long_index + 1 >= len(longs):
                raise SchematicParseError("Axiom 蓝图文件损坏：分块方块数据不完整。")
            value |= int(longs[long_index + 1]) << (64 - shift)
        values.append(value & mask)
    return values


def _axiom_chunk_adjacency(indices: array, is_air: list[bool]) -> int:
    """统计分块内“相邻两个非空气方块”的数量，用于判定位打包方式是否解对。

    解错打包方式时方块位置会被打乱，相邻度会显著低于真实数据（随机水平）；
    正确数据通常具有大量相邻方块（墙壁、地面、树干等）。
    """
    score = 0
    for index, palette_index in enumerate(indices):
        if is_air[palette_index]:
            continue
        if (index & 15) != 15 and not is_air[indices[index + 1]]:
            score += 1
        if ((index >> 4) & 15) != 15 and not is_air[indices[index + 16]]:
            score += 1
        if (index >> 8) != 15 and not is_air[indices[index + 256]]:
            score += 1
    return score


def _axiom_chunk_candidates(
    longs: array, palette_size: int, is_air: list[bool]
) -> tuple[array, str]:
    """解出分块的调色板索引并选择最可能的位打包方式。

    调色板 ≤16（4-bit）或位数能整除 64 时两种打包等价，直接使用；
    其它情况（5/6/7/9~12-bit）两种打包都可能“解得出来但内容错位”，
    用分块内非空气方块的相邻度来评分，取更合理的那个（同分优先 Minecraft
    1.16+ 的逐 long 打包语义）。
    """
    total = _AXIOM_BP_CHUNK_BLOCKS
    if palette_size <= 1:
        return array("H", [0]) * total, "padded"
    bits = max(4, (palette_size - 1).bit_length())
    if 64 % bits == 0:
        # 位数整除 64 时“逐 long 打包”和“连续位流”完全等价，解一次即可
        try:
            indices = _decode_axiom_indices(longs, bits, "padded")
        except SchematicParseError:
            raise
        if max(indices) >= palette_size:
            raise SchematicParseError(
                "Axiom 蓝图文件损坏：分块调色板索引越界，可能使用了暂不支持的方块数据打包方式。"
            )
        return indices, "padded"
    candidates: list[tuple[int, array, str]] = []
    last_error: SchematicParseError | None = None
    for mode in ("padded", "contiguous"):
        try:
            indices = _decode_axiom_indices(longs, bits, mode)
        except SchematicParseError as exc:
            last_error = exc
            continue
        if max(indices) >= palette_size:
            continue
        candidates.append((_axiom_chunk_adjacency(indices, is_air), indices, mode))
    if not candidates:
        if last_error is not None:
            raise last_error
        raise SchematicParseError(
            "Axiom 蓝图文件损坏：分块调色板索引越界，可能使用了暂不支持的方块数据打包方式。"
        )
    best_score, best_indices, best_mode = candidates[0]
    for score, indices, mode in candidates[1:]:
        if score > best_score:
            best_score, best_indices, best_mode = score, indices, mode
    return best_indices, best_mode


def _axiom_chunk_palette(block_states: dict, fmt: str) -> tuple[list[str], list[dict[str, str]]]:
    """读取一个分块的调色板（名字统一为 minecraft: 命名空间 + Properties）。"""
    palette_src = block_states.get("palette")
    if not isinstance(palette_src, list) or not palette_src:
        raise SchematicParseError(f"{fmt} 文件损坏：分块缺少调色板 palette 或为空。")
    names: list[str] = []
    states: list[dict[str, str]] = []
    for entry in palette_src:
        if not isinstance(entry, dict):
            raise SchematicParseError(f"{fmt} 文件损坏：palette 中存在非 Compound 条目。")
        block_name, inline_properties = _parse_block_state(entry.get("Name"))
        properties = _coerce_properties(entry.get("Properties"))
        if inline_properties:
            properties = {**inline_properties, **properties}
        names.append(block_name)
        states.append(properties)
    return names, states


def _parse_axiom_structure(root: dict, metadata: dict) -> ParsedSchematic:
    """解析 Axiom 蓝图的 structure NBT（BlockRegion 分块），返回统一结果。"""
    fmt = "Axiom 蓝图"
    regions = root.get("BlockRegion")
    if not isinstance(regions, list) or not regions:
        raise SchematicParseError(f"{fmt} 文件损坏：缺少 BlockRegion 字段或其为空。")
    if len(regions) > MAX_AXIOM_REGIONS:
        raise SchematicParseError(
            f"{fmt} 文件分块数量异常：{len(regions):,} 个 > 安全上限 {MAX_AXIOM_REGIONS:,}，已拒绝解析。"
        )
    if len(regions) * _AXIOM_BP_CHUNK_BLOCKS > MAX_AXIOM_CHUNK_VOLUME:
        raise SchematicParseError(
            f"{fmt} 文件分块总容量过大：{len(regions) * _AXIOM_BP_CHUNK_BLOCKS:,} 格，"
            f"超过安全上限 {MAX_AXIOM_CHUNK_VOLUME:,} 格，已拒绝解析。"
        )

    data_version = _as_int(root.get("DataVersion"), 0)
    name = _as_str(metadata.get("Name"))
    author = _as_str(metadata.get("Author"))
    raw_tags = metadata.get("Tags")
    tags = [tag for tag in raw_tags if isinstance(tag, str)] if isinstance(raw_tags, list) else []
    description = " / ".join(tags)
    declared_blocks = _as_int(metadata.get("BlockCount"), -1)

    # 分块记录：(cx, cy, cz, palette 名字, palette 状态, long 数组, 打包模式)
    chunks: list[tuple[int, int, int, list[str], list[dict[str, str]], array, str]] = []
    bbox_min: list[int | None] = [None, None, None]
    bbox_max: list[int | None] = [None, None, None]
    total_blocks = 0

    # 第一遍：解码分块、统计非空气方块并计算真实包围盒
    for region in regions:
        if not isinstance(region, dict):
            raise SchematicParseError(f"{fmt} 文件损坏：BlockRegion 中存在非 Compound 条目。")
        chunk_x = _read_required_int(region, "X", fmt)
        chunk_y = _read_required_int(region, "Y", fmt)
        chunk_z = _read_required_int(region, "Z", fmt)
        block_states = _read_compound(region, "BlockStates", fmt)
        names, states = _axiom_chunk_palette(block_states, fmt)
        longs = _axiom_long_array(block_states.get("data"), fmt)
        is_air = [_is_air(block_name) for block_name in names]
        indices, mode = _axiom_chunk_candidates(longs, len(names), is_air)

        if all(is_air):
            continue  # 整个分块都是空气/结构空位，直接跳过

        chunks.append((chunk_x, chunk_y, chunk_z, names, states, longs, mode))
        base_x = chunk_x * 16
        base_y = chunk_y * 16
        base_z = chunk_z * 16
        for index, palette_index in enumerate(indices):
            if is_air[palette_index]:
                continue
            total_blocks += 1
            block_x = base_x + (index & 15)
            block_y = base_y + (index >> 8)
            block_z = base_z + ((index >> 4) & 15)
            if bbox_min[0] is None or block_x < bbox_min[0]:
                bbox_min[0] = block_x
            if bbox_min[1] is None or block_y < bbox_min[1]:
                bbox_min[1] = block_y
            if bbox_min[2] is None or block_z < bbox_min[2]:
                bbox_min[2] = block_z
            if bbox_max[0] is None or block_x > bbox_max[0]:
                bbox_max[0] = block_x
            if bbox_max[1] is None or block_y > bbox_max[1]:
                bbox_max[1] = block_y
            if bbox_max[2] is None or block_z > bbox_max[2]:
                bbox_max[2] = block_z

    if not chunks or bbox_min[0] is None:
        return _empty_result(
            "bp", name=name, author=author, description=description, data_version=data_version
        )

    # 与 metadata 的 BlockCount 交叉校验（仅用于诊断，不一致不致命：可能包含空气标记差异）
    if declared_blocks > 0 and total_blocks != declared_blocks:
        pass  # 解析结果以实际数据为准；保留该信息供未来 warnings 使用

    width = bbox_max[0] - bbox_min[0] + 1
    height = bbox_max[1] - bbox_min[1] + 1
    length = bbox_max[2] - bbox_min[2] + 1
    volume = _validate_volume(width, height, length, fmt)
    blocks = array("i", [0]) * volume
    builder = _PaletteBuilder()
    min_x, min_y, min_z = bbox_min[0], bbox_min[1], bbox_min[2]

    # 第二遍：解码并写入非空气方块（重新解码而不是缓存索引，避免超大蓝图占内存）
    for chunk_x, chunk_y, chunk_z, names, states, longs, mode in chunks:
        palette_size = len(names)
        remap = array("i", [0]) * palette_size
        for palette_index, block_name in enumerate(names):
            remap[palette_index] = builder.index(block_name, states[palette_index])
        indices = _decode_axiom_indices(longs, max(4, (palette_size - 1).bit_length()), mode)
        base_x = chunk_x * 16 - min_x
        base_y = chunk_y * 16 - min_y
        base_z = chunk_z * 16 - min_z
        for index, palette_index in enumerate(indices):
            output_index = remap[palette_index]
            if output_index == 0:
                continue
            block_x = base_x + (index & 15)
            block_y = base_y + (index >> 8)
            block_z = base_z + ((index >> 4) & 15)
            blocks[(block_y * length + block_z) * width + block_x] = output_index

    return ParsedSchematic(
        width=width, height=height, length=length,
        palette=builder.names, blocks=blocks, format="bp",
        name=name, author=author, description=description,
        data_version=data_version, total_blocks=total_blocks,
        palette_states=builder.states,
    )


def _parse_axiom_blueprint(data: bytes) -> ParsedSchematic:
    """解析 Axiom 蓝图（.bp）完整文件字节。"""
    fmt = "Axiom 蓝图（.bp）"
    if len(data) < 16:
        raise SchematicParseError(f"{fmt} 文件太小，不是有效的蓝图文件。")
    if data[:4] != _AXIOM_BP_MAGIC:
        raise SchematicParseError(
            f"不是有效的 Axiom 蓝图文件：文件头 magic 不匹配"
            f"（应为 0A E5 BB 36，实际为 {data[:4].hex(' ').upper()}）。"
        )

    offset = 4
    metadata_length = _read_be_u32(data, offset, "metadata 段")
    offset += 4
    if metadata_length <= 0:
        raise SchematicParseError(f"{fmt} 文件损坏：metadata 段为空。")
    if offset + metadata_length > len(data):
        raise SchematicParseError(f"{fmt} 文件损坏：metadata 段长度超出文件大小，文件可能被截断。")
    _metadata_name, metadata = _parse_nbt(data[offset:offset + metadata_length])
    offset += metadata_length
    if not isinstance(metadata, dict):
        raise SchematicParseError(f"{fmt} 文件损坏：metadata 不是 NBT Compound。")

    preview_length = _read_be_u32(data, offset, "preview 段")
    offset += 4
    if offset + preview_length > len(data):
        raise SchematicParseError(f"{fmt} 文件损坏：preview 段长度超出文件大小，文件可能被截断。")
    offset += preview_length  # 预览缩略图 PNG 对渲染无用，直接跳过

    structure_length = _read_be_u32(data, offset, "structure 段")
    offset += 4
    if structure_length <= 0:
        raise SchematicParseError(f"{fmt} 文件损坏：structure 段为空。")
    if offset + structure_length > len(data):
        raise SchematicParseError(f"{fmt} 文件损坏：structure 段长度超出文件大小，文件可能被截断。")
    compressed = data[offset:offset + structure_length]
    structure_bytes = _bounded_decompress(compressed, 16 + zlib.MAX_WBITS, "Axiom 蓝图结构数据")
    _structure_name, structure_root = _parse_nbt(structure_bytes)
    if not isinstance(structure_root, dict):
        raise SchematicParseError(f"{fmt} 文件损坏：structure 段不是 NBT Compound。")
    return _parse_axiom_structure(structure_root, metadata)


# --------------------------------------------------------------------------- #
# 公开入口
# --------------------------------------------------------------------------- #


def parse_schematic(
    data: bytes, filename: str, max_volume: int | None = None
) -> ParsedSchematic:
    """解析 Minecraft 投影/结构文件字节。

    :param data: 文件完整字节（未解压的原始文件内容）。
    :param filename: 文件名，仅用于判断扩展名和错误提示（不会读取磁盘）。
    :param max_volume: 可选的本次解析体积上限（格）；None 用默认 MAX_VOLUME。
    :return: 统一的 :class:`ParsedSchematic`。
    :raises SchematicParseError: 文件为空/损坏/格式不支持/尺寸超限。
    """
    limit = None
    if max_volume is not None:
        try:
            limit = int(max_volume)
        except (TypeError, ValueError):
            raise SchematicParseError("体积上限必须是整数。") from None
        limit = max(1, min(MAX_VOLUME_LIMIT, limit))
    token = _VOLUME_OVERRIDE.set(limit) if limit is not None else None
    try:
        return _parse_schematic_impl(data, filename)
    finally:
        if token is not None:
            _VOLUME_OVERRIDE.reset(token)


def _parse_schematic_impl(data: bytes, filename: str) -> ParsedSchematic:
    """解析实现（实际体积上限由 parse_schematic 的 ContextVar 控制）。"""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise SchematicParseError("输入数据必须是 bytes。")
    data = bytes(data)
    extension = _extension(filename)
    if extension not in SUPPORTED_EXTENSIONS:
        supported = "、".join(SUPPORTED_EXTENSIONS)
        raise SchematicParseError(
            f"不支持的文件格式：{filename or '(未命名文件)'}；支持的扩展名为 {supported}。"
        )
    if not data:
        raise SchematicParseError("文件为空，无法解析。")

    # Axiom 蓝图（.bp）不是 NBT/gzip 开头，需要单独走容器解析分支；
    # 同时按 magic 嗅探，兼容被改了扩展名的 .bp 文件。
    if extension == ".bp" or data[:4] == _AXIOM_BP_MAGIC:
        try:
            return _parse_axiom_blueprint(data)
        except SchematicParseError:
            raise
        except (KeyError, TypeError, ValueError, IndexError, struct.error, zlib.error) as exc:
            raise SchematicParseError(f"解析 .bp 文件失败：{exc}") from exc

    raw = _decompress(data)
    _root_name, root = _parse_nbt(raw)
    if not isinstance(root, dict):
        raise SchematicParseError("文件不是有效的 NBT Compound。")

    try:
        if extension == ".litematic":
            return _parse_litematic(root)
        if extension == ".schem":
            return _parse_schem(root)
        if extension == ".schematic":
            return _parse_mcedit_schematic(root)
        return _parse_structure_nbt(root)
    except SchematicParseError:
        raise
    except (KeyError, TypeError, ValueError, IndexError, struct.error, zlib.error) as exc:
        # 兜底：把解析器的内部异常转成可直接展示的中文错误
        raise SchematicParseError(f"解析 {extension} 文件失败：{exc}") from exc
