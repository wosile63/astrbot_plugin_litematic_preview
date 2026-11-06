"""投影材料清单（所需物品统计）。

设计依据（详见 ``research/materials/MATERIALS_RESEARCH.md``）：
- 行为语义参考 Litematica（LGPL-3.0，只研究规则、不复制代码）：
  方块状态 → 创造模式选取物品；双台阶×2、雪层=层数、海泡菜/海龟蛋/蜡烛/多面生长/花瓣=数量；
  门上半/床头/高草上半只计一次；活塞头/传送门/火等不可获取方块忽略；花盆=花盆+植物。
- 物品/方块/堆叠数/英文名数据来自 PrismarineJS/minecraft-data（MIT）；
  中文名来自 Mojang 官方 zh_cn 语言文件（与内置原版资源包同类，仅本地个人使用）。

用法::

    from materials import count_materials
    report = count_materials(parsed, locale="zh_cn")
    report.items  # [MaterialEntry(...), ...] 按数量降序
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field

try:  # numpy 在插件运行环境可用；没有时退化为纯 Python 计数
    import numpy as _np
except Exception:  # pragma: no cover - 极端环境
    _np = None


_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_CANDIDATES = [
    os.path.join(_MODULE_DIR, "assets", "materials_data.json"),
    os.path.join(_MODULE_DIR, "materials_data.json"),
]

_ITEMS: dict[str, tuple[str, str, int]] = {}
_NO_ITEM_BLOCKS: set[str] = set()
_DATA_VERSION = ""
_LOADED = False


def _load_data() -> None:
    global _ITEMS, _NO_ITEM_BLOCKS, _DATA_VERSION, _LOADED
    if _LOADED:
        return
    _LOADED = True
    path = next((p for p in _DATA_CANDIDATES if os.path.isfile(p)), "")
    if not path:
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        _DATA_VERSION = str((raw.get("_meta") or {}).get("mc_version") or "")
        _NO_ITEM_BLOCKS = set((raw.get("_meta") or {}).get("blocks_without_same_name_item") or [])
        _ITEMS = {
            str(name): (str(value[0]), str(value[1]), int(value[2]))
            for name, value in (raw.get("items") or {}).items()
            if isinstance(value, list) and len(value) >= 3
        }
    except (OSError, ValueError):
        _ITEMS = {}
        _NO_ITEM_BLOCKS = set()


def dataset_version() -> str:
    _load_data()
    return _DATA_VERSION


def item_count() -> int:
    _load_data()
    return len(_ITEMS)


# --------------------------------------------------------------------------- #
# 方块 → 物品规则
# --------------------------------------------------------------------------- #

_AIR_BLOCKS = {"air", "cave_air", "void_air"}
_SKIP_BLOCKS = {
    # 不可获取 / 技术方块（Litematica MaterialCache 同样忽略）
    "piston_head",
    "moving_piston",
    "nether_portal",
    "end_portal",
    "end_gateway",
    "fire",
    "soul_fire",
    "bubble_column",
    "frosted_ice",
    "structure_void",
}

# 两格高、上半部分不应重复计一次的方块（门/床单独处理）
_TALL_BLOCKS = {
    "tall_seagrass",
    "sunflower",
    "lilac",
    "rose_bush",
    "peony",
    "tall_grass",
    "large_fern",
    "small_dripleaf",
    "pitcher_crop",
    "pitcher_plant",
}

_MULTIFACE_BLOCKS = {"glow_lichen", "sculk_vein"}
_MULTIFACE_FACES = ("north", "east", "south", "west", "up", "down")

_CROP_ITEMS = {
    "wheat": "wheat_seeds",
    "carrots": "carrot",
    "potatoes": "potato",
    "beetroots": "beetroot_seeds",
    "cocoa": "cocoa_beans",
    "melon_stem": "melon_seeds",
    "attached_melon_stem": "melon_seeds",
    "pumpkin_stem": "pumpkin_seeds",
    "attached_pumpkin_stem": "pumpkin_seeds",
    "torchflower_crop": "torchflower_seeds",
    "pitcher_crop": "pitcher_pod",
    "pitcher_plant": "pitcher_pod",
    "sweet_berry_bush": "sweet_berries",
    "cave_vines": "glow_berries",
    "cave_vines_plant": "glow_berries",
    "kelp_plant": "kelp",
    "twisting_vines_plant": "twisting_vines",
    "weeping_vines_plant": "weeping_vines",
    "big_dripleaf_stem": "big_dripleaf",
    "bamboo_sapling": "bamboo",
    "tall_seagrass": "seagrass",
    "nether_wart": "nether_wart",
    "cactus": "cactus",
    "sugar_cane": "sugar_cane",
    "bamboo": "bamboo",
}

# 盆栽方块：potted_ 后名称不是物品名时需要转换
_POTTED_PLANT_ITEMS = {
    "azalea_bush": "azalea",
    "flowering_azalea_bush": "flowering_azalea",
    "closed_eyeblossom": "closed_eyeblossom",
    "open_eyeblossom": "open_eyeblossom",
    "mangrove_propagule": "mangrove_propagule",
    "torchflower": "torchflower",
}

_CAULDRONS = {"cauldron", "water_cauldron", "lava_cauldron", "powder_snow_cauldron"}

_INT_CACHE: dict[str, int] = {}


def _int_prop(props: dict[str, str], key: str, default: int) -> int:
    value = props.get(key)
    if value is None:
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _strip_namespace(block: str) -> str:
    return block.split(":", 1)[1] if ":" in block else block


def _known_item(name: str) -> bool:
    _load_data()
    return name in _ITEMS


def _resolve_items(block: str, props: dict[str, str]) -> list[tuple[str, int]] | None:
    """把单个方块状态解析为 [(物品, 数量), ...]。

    返回 ``None`` 表示“明确忽略”（空气/技术方块，不计入未识别）；
    返回空列表表示“无法识别”，由调用方计入未识别方块。
    """
    name = _strip_namespace(block)
    half = str(props.get("half") or "").lower()

    if name in _AIR_BLOCKS:
        return None
    if name in _SKIP_BLOCKS:
        return None

    # 两格高方块：只算下半 / 作物本体
    if name in _TALL_BLOCKS:
        if half == "upper":
            return None
        if name in ("pitcher_crop", "pitcher_plant"):
            return [("pitcher_pod", 1)]
        if name == "small_dripleaf":
            return [("small_dripleaf", 1)]
        if name == "tall_seagrass":
            return [("seagrass", 1)]
        return [(name, 1)]

    # 门：只算下半；床：只算床尾；高草类通过 half 处理
    if name.endswith("_door") or name in ("oak_door", "iron_door"):
        if half == "upper":
            return None
        return [(name, 1)]
    if name.endswith("_bed"):
        if str(props.get("part") or "").lower() == "head":
            return None
        return [(name, 1)]

    # 双台阶 ×2
    if name.endswith("_slab") and str(props.get("type") or "").lower() == "double":
        return [(name, 2)]

    # 雪层 = 层数
    if name == "snow":
        return [("snow", _clamp(_int_prop(props, "layers", 1), 1, 8))]

    # 海泡菜 / 海龟蛋 / 蜡烛 / 多面生长 / 花瓣类：一个方块包含多个物品
    if name == "sea_pickle":
        return [("sea_pickle", _clamp(_int_prop(props, "pickles", 1), 1, 4))]
    if name == "turtle_egg":
        return [("turtle_egg", _clamp(_int_prop(props, "eggs", 1), 1, 4))]
    if name == "candle" or (name.endswith("_candle") and not name.endswith("_candle_cake")):
        return [(name, _clamp(_int_prop(props, "candles", 1), 1, 4))]
    if name in _MULTIFACE_BLOCKS:
        faces = sum(1 for face in _MULTIFACE_FACES if str(props.get(face) or "").lower() == "true")
        return [(name, max(1, faces))]
    if name in ("pink_petals", "wildflowers"):
        return [(name, _clamp(_int_prop(props, "flower_amount", 1), 1, 4))]
    if name == "leaf_litter":
        return [("leaf_litter", _clamp(_int_prop(props, "segment_amount", 1), 1, 4))]

    # 蜡烛蛋糕 = 蛋糕 + 蜡烛
    if name == "candle_cake" or name.endswith("_candle_cake"):
        candle = "candle" if name == "candle_cake" else name[: -len("_cake")]
        return [("cake", 1), (candle, 1)]

    # 盆栽 = 花盆 + 植物
    if name.startswith("potted_"):
        suffix = name[len("potted_"):]
        plant = _POTTED_PLANT_ITEMS.get(suffix, suffix)
        return [("flower_pot", 1), (plant, 1)]

    # 墙上的变体 → 立式物品
    if name.endswith("_wall_hanging_sign"):
        return [(name[: -len("_wall_hanging_sign")] + "_hanging_sign", 1)]
    if name.endswith("_wall_sign"):
        return [(name[: -len("_wall_sign")] + "_sign", 1)]
    if name.endswith("_wall_banner"):
        return [(name[: -len("_wall_banner")] + "_banner", 1)]
    if name.endswith("_wall_torch"):
        return [(name[: -len("_wall_torch")] + "_torch", 1)]
    if name.endswith("_wall_head"):
        return [(name[: -len("_wall_head")] + "_head", 1)]
    if name.endswith("_wall_skull"):
        return [(name[: -len("_wall_skull")] + "_skull", 1)]
    if name.endswith("_wall_fan"):
        return [(name.replace("_wall_fan", "_fan"), 1)]

    # 液体：只有源方块算桶；流动液体忽略
    if name == "water":
        level = _int_prop(props, "level", 0)
        return [("water_bucket", 1)] if level == 0 else None
    if name == "lava":
        level = _int_prop(props, "level", 0)
        return [("lava_bucket", 1)] if level == 0 else None
    if name == "powder_snow":
        return [("powder_snow_bucket", 1)]
    if name in _CAULDRONS:
        return [("cauldron", 1)]

    # 作物/植物
    if name in _CROP_ITEMS:
        return [(_CROP_ITEMS[name], 1)]

    # 没有同名物品、但语义明确的特殊方块
    if name == "redstone_wire":
        return [("redstone", 1)]
    if name == "tripwire":
        return [("string", 1)]
    if name == "wall_torch":
        return [("torch", 1)]
    if name == "tall_seagrass":
        return [("seagrass", 1)]

    # 耕地 → 泥土（Litematica 语义）
    if name == "farmland":
        return [("dirt", 1)]

    # 默认：同名物品存在则计 1
    if _known_item(name):
        return [(name, 1)]

    # 已知没有同名物品、且没有规则能映射 → 未识别
    _load_data()
    if name in _NO_ITEM_BLOCKS:
        return []

    # 新版本未知方块：按同名物品估算（标记在报告中）
    return [(name, 1)]


# --------------------------------------------------------------------------- #
# 方块状态计数（Excel「方块统计」用）
# --------------------------------------------------------------------------- #


@dataclass
class BlockCount:
    block_id: str                 # 完整 ID，如 minecraft:oak_stairs
    properties: dict[str, str] = field(default_factory=dict)
    count: int = 0

    @property
    def state_text(self) -> str:
        if not self.properties:
            return ""
        return ",".join(f"{key}={self.properties[key]}" for key in sorted(self.properties))

    def to_dict(self) -> dict:
        return {
            "block": self.block_id,
            "state": self.state_text,
            "count": self.count,
        }


def count_block_states(parsed) -> list[BlockCount]:
    """按“方块 ID + 方块状态属性”统计原始方块数量（不含空气，按数量降序）。"""
    _load_data()
    palette = list(getattr(parsed, "palette", []) or [])
    states = list(getattr(parsed, "palette_states", []) or [])
    blocks = getattr(parsed, "blocks", None)
    if not palette or blocks is None:
        return []
    counts = _palette_counts(blocks, len(palette))
    result: list[BlockCount] = []
    for index, block in enumerate(palette):
        count = counts[index] if index < len(counts) else 0
        if count <= 0:
            continue
        name = str(block)
        if _strip_namespace(name) in _AIR_BLOCKS:
            continue
        props = states[index] if index < len(states) and isinstance(states[index], dict) else {}
        result.append(BlockCount(block_id=name, properties=dict(props), count=count))
    result.sort(key=lambda entry: (-entry.count, entry.block_id))
    return result


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class MaterialEntry:
    item_id: str                 # 不含命名空间，如 "stone"
    count: int
    display_name: str = ""
    en_name: str = ""
    stack_size: int = 64

    @property
    def full_id(self) -> str:
        return "minecraft:" + self.item_id

    @property
    def stacks(self) -> int:
        size = self.stack_size if self.stack_size > 0 else 64
        return math.ceil(self.count / size)

    @property
    def shulker_boxes(self) -> int:
        size = self.stack_size if self.stack_size > 0 else 64
        return self.count // (size * 27)

    def to_dict(self, locale: str = "zh_cn") -> dict:
        return {
            "id": self.full_id,
            "name": _display_name(self.item_id, locale),
            "count": self.count,
            "stack_size": self.stack_size,
            "stacks": self.stacks,
            "shulker_boxes": self.shulker_boxes,
        }


@dataclass
class MaterialReport:
    items: list[MaterialEntry] = field(default_factory=list)
    total_items: int = 0
    total_types: int = 0
    block_count: int = 0
    ignored_blocks: int = 0                       # 空气/技术方块等合理忽略
    ignored_detail: dict[str, int] = field(default_factory=dict)
    unresolved_blocks: int = 0                    # 无法映射为物品的方块
    unresolved_detail: dict[str, int] = field(default_factory=dict)
    estimated_blocks: int = 0                     # 数据版本较新、按同名物品估算的方块
    estimated_detail: dict[str, int] = field(default_factory=dict)
    dataset_version: str = ""

    def top(self, limit: int) -> list[MaterialEntry]:
        return self.items[:limit] if limit and limit > 0 else list(self.items)

    def to_dict(self, locale: str = "zh_cn", limit: int = 0, include_items: bool = True) -> dict:
        payload = {
            "total_items": self.total_items,
            "total_types": self.total_types,
            "block_count": self.block_count,
            "ignored_blocks": self.ignored_blocks,
            "unresolved_blocks": self.unresolved_blocks,
            "unresolved_examples": sorted(
                self.unresolved_detail, key=lambda key: -self.unresolved_detail[key]
            )[:8],
            "estimated_blocks": self.estimated_blocks,
            "dataset_version": self.dataset_version,
        }
        if include_items:
            entries = self.top(limit) if limit else self.items
            payload["items"] = [entry.to_dict(locale) for entry in entries]
        return payload


def _display_name(item_id: str, locale: str = "zh_cn") -> str:
    _load_data()
    info = _ITEMS.get(item_id)
    if info is None:
        return _prettify(item_id)
    en, zh, _stack = info
    if locale in ("zh", "zh_cn") and zh:
        return zh
    return en or _prettify(item_id)


def _prettify(item_id: str) -> str:
    return item_id.replace("_", " ").title()


# --------------------------------------------------------------------------- #
# 统计入口
# --------------------------------------------------------------------------- #


def _palette_counts(blocks, palette_size: int) -> list[int]:
    if palette_size <= 0:
        return []
    if _np is not None:
        try:
            indices = _np.asarray(blocks)
            if indices.size == 0:
                return [0] * palette_size
            counts = _np.bincount(indices.astype(_np.int64, copy=False), minlength=palette_size)
            return [int(value) for value in counts[:palette_size]]
        except (TypeError, ValueError, OverflowError):
            pass
    counts = [0] * palette_size
    for index in blocks:
        index = int(index)
        if 0 <= index < palette_size:
            counts[index] += 1
    return counts


def count_materials(parsed, locale: str = "zh_cn") -> MaterialReport:
    """统计投影所需物品。

    ``parsed`` 为 ``schematic_parser.ParsedSchematic``（或同接口对象）。
    """
    _load_data()
    palette = list(getattr(parsed, "palette", []) or [])
    states = list(getattr(parsed, "palette_states", []) or [])
    blocks = getattr(parsed, "blocks", None)
    report = MaterialReport(dataset_version=_DATA_VERSION)
    if not palette or blocks is None:
        return report

    counts = _palette_counts(blocks, len(palette))
    item_counts: Counter[str] = Counter()
    block_count = 0
    ignored = 0
    ignored_detail: Counter[str] = Counter()
    unresolved = 0
    unresolved_detail: Counter[str] = Counter()
    estimated = 0
    estimated_detail: Counter[str] = Counter()

    for index, palette_block in enumerate(palette):
        count = counts[index] if index < len(counts) else 0
        if count <= 0:
            continue
        name = _strip_namespace(str(palette_block))
        props = states[index] if index < len(states) and isinstance(states[index], dict) else {}
        if name in _AIR_BLOCKS:
            ignored += count
            ignored_detail[name] += count
            continue
        block_count += count
        resolved = _resolve_items(str(palette_block), props)
        if resolved is None:
            ignored += count
            ignored_detail[name] += count
            continue
        if not resolved:
            unresolved += count
            unresolved_detail[name] += count
            continue
        known = _known_item(name)
        for item, qty in resolved:
            if not known and item == name:
                estimated += count * qty
                estimated_detail[name] += count
            item_counts[item] += count * qty

    entries: list[MaterialEntry] = []
    for item, total in item_counts.items():
        info = _ITEMS.get(item)
        if info is None:
            en = _prettify(item)
            zh = ""
            stack = 64
        else:
            en, zh, stack = info
        entries.append(
            MaterialEntry(
                item_id=item,
                count=total,
                display_name=zh if locale in ("zh", "zh_cn") and zh else en,
                en_name=en,
                stack_size=stack,
            )
        )
    entries.sort(key=lambda entry: (-entry.count, entry.display_name))
    report.items = entries
    report.total_types = len(entries)
    report.total_items = sum(entry.count for entry in entries)
    report.block_count = block_count
    report.ignored_blocks = ignored
    report.ignored_detail = dict(ignored_detail)
    report.unresolved_blocks = unresolved
    report.unresolved_detail = dict(unresolved_detail)
    report.estimated_blocks = estimated
    report.estimated_detail = dict(estimated_detail)
    return report


def format_count(count: int) -> str:
    """把数量格式化为带千分位的字符串。"""
    return f"{count:,}"


def describe_brief(report: MaterialReport, locale: str = "zh_cn", max_items: int = 10) -> str:
    """单行摘要：``共 23 种 / 12,345 个：石头 × 8,000、橡木木板 × 2,345 …``"""
    parts = [f"{entry.display_name} × {format_count(entry.count)}" for entry in report.top(max_items)]
    head = f"共 {report.total_types} 种 / {format_count(report.total_items)} 个"
    if parts:
        head += "：" + "、".join(parts)
        remaining = report.total_types - min(max_items, report.total_types)
        if remaining > 0:
            head += f" 等（另 {remaining} 种）"
    else:
        head += "（无可统计物品）"
    return head
