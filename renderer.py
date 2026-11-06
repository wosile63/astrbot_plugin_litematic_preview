"""等距（isometric）投影渲染器：把解析后的 schematic 数据渲染成 PNG 预览图。

设计要点：
- 只用 numpy + Pillow（AstrBot 自带），不依赖外部渲染库 / 纹理包；
- 方块颜色优先取 ``block_colors.json``（由 tools/build_block_colors.py 从官方客户端
  jar 的纹理平均色生成），缺失时按方块名做启发式配色，保证任何整合包方块都能画出；
- 经典 2:1 等距投影：每个方块预渲染成 tile_w×tile_w 的立方体 sprite（顶面 + 左面 + 右面），
  按 x+y+z（离相机远近）从远到近贴图，实现遮挡关系（painter's algorithm）；
- 通过 6 邻居判定只渲染"外壳"方块，内部方块直接跳过，几百 MB 的投影也能秒级渲染；
- 自适应缩放：根据结构尺寸自动选 tile 大小，保证输出图不超过配置的最大尺寸；
- 半透明/装饰方块（空气、水、玻璃、火把、花等）默认跳过，避免大面积遮挡内部结构。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
from PIL import Image, ImageDraw, ImageFont

__all__ = [
    "RenderOptions",
    "RenderResult",
    "RenderError",
    "render_schematic",
    "render_from_parsed",
    "resolve_block_color",
    "is_skipped_block",
]

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_COLOR_FILE = os.path.join(_MODULE_DIR, "block_colors.json")


class RenderError(Exception):
    """渲染失败（尺寸过大、方块太多、无可见方块等），消息可直接给用户看。"""


# --------------------------------------------------------------------------- #
# 选项与结果
# --------------------------------------------------------------------------- #


@dataclass
class RenderOptions:
    """渲染参数（由插件配置注入）。"""

    max_width: int = 1600
    max_height: int = 1600
    prefer_tile_size: int = 28
    background: str = "dark"  # dark / light / #RRGGBB
    show_caption: bool = True
    skip_decorations: bool = True
    max_visible_blocks: int = 1_000_000
    add_shadow: bool = True  # 结构底部投影（假阴影），增加立体感


@dataclass
class RenderResult:
    png: bytes
    width: int
    height: int
    tile_size: int
    visible_blocks: int
    total_blocks: int
    elapsed: float
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 颜色表加载与启发式配色
# --------------------------------------------------------------------------- #

_COLOR_CACHE: dict | None = None
_COLOR_VERSION = ""


def _load_color_table() -> dict:
    global _COLOR_CACHE, _COLOR_VERSION
    if _COLOR_CACHE is not None:
        return _COLOR_CACHE
    blocks: dict = {}
    try:
        with open(_COLOR_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        blocks = payload.get("blocks") or {}
        _COLOR_VERSION = str(payload.get("version") or "")
    except FileNotFoundError:
        pass
    except Exception:
        blocks = {}
    _COLOR_CACHE = blocks
    return blocks


def _rgb(color: tuple[int, int, int]) -> tuple[int, int, int]:
    return (int(color[0]) & 0xFF, int(color[1]) & 0xFF, int(color[2]) & 0xFF)


# 16 种染色（羊毛/混凝土/陶瓦/玻璃通用）
_DYE_COLORS: dict[str, tuple[int, int, int]] = {
    "white": (233, 236, 236),
    "light_gray": (154, 161, 161),
    "gray": (74, 79, 81),
    "black": (26, 26, 29),
    "brown": (112, 71, 40),
    "red": (160, 39, 34),
    "orange": (233, 118, 20),
    "yellow": (245, 196, 39),
    "lime": (112, 185, 25),
    "green": (73, 100, 30),
    "cyan": (21, 119, 136),
    "light_blue": (58, 154, 201),
    "blue": (44, 46, 143),
    "purple": (100, 31, 156),
    "magenta": (186, 66, 174),
    "pink": (225, 139, 170),
}

# 木材物种主色
_WOOD_COLORS: dict[str, tuple[int, int, int]] = {
    "oak": (176, 143, 88),
    "spruce": (108, 80, 47),
    "birch": (213, 201, 145),
    "jungle": (160, 115, 80),
    "acacia": (177, 96, 52),
    "dark_oak": (74, 51, 30),
    "mangrove": (117, 74, 60),
    "cherry": (225, 180, 178),
    "bamboo": (201, 180, 85),
    "crimson": (121, 63, 82),
    "warped": (50, 122, 120),
    "pale_oak": (198, 185, 160),
}

# 常见材料名 -> RGB
_MATERIAL_COLORS: dict[str, tuple[int, int, int]] = {
    "stone": (125, 125, 125),
    "cobblestone": (108, 108, 108),
    "mossy_cobblestone": (94, 108, 80),
    "smooth_stone": (158, 158, 158),
    "stone_bricks": (118, 118, 118),
    "mossy_stone_bricks": (94, 110, 82),
    "cracked_stone_bricks": (110, 110, 108),
    "chiseled_stone_bricks": (116, 116, 116),
    "granite": (156, 106, 87),
    "diorite": (190, 190, 190),
    "andesite": (138, 138, 135),
    "deepslate": (80, 80, 84),
    "cobbled_deepslate": (77, 77, 82),
    "polished_deepslate": (72, 72, 76),
    "deepslate_bricks": (70, 70, 74),
    "deepslate_tiles": (66, 66, 70),
    "tuff": (108, 108, 100),
    "dirt": (134, 96, 67),
    "coarse_dirt": (119, 85, 59),
    "rooted_dirt": (144, 103, 75),
    "grass_block": (121, 160, 80),
    "podzol": (92, 66, 29),
    "mycelium": (111, 99, 105),
    "sand": (219, 207, 163),
    "red_sand": (190, 102, 33),
    "sandstone": (216, 205, 160),
    "red_sandstone": (181, 96, 31),
    "gravel": (136, 126, 123),
    "clay": (160, 166, 179),
    "terracotta": (152, 94, 67),
    "bricks": (150, 84, 70),
    "mud_bricks": (138, 108, 88),
    "netherrack": (97, 38, 38),
    "nether_bricks": (44, 22, 26),
    "soul_sand": (85, 65, 51),
    "soul_soil": (78, 60, 48),
    "basalt": (80, 78, 82),
    "blackstone": (42, 36, 42),
    "end_stone": (219, 219, 174),
    "end_stone_bricks": (217, 214, 170),
    "purpur_block": (169, 125, 169),
    "quartz_block": (235, 229, 222),
    "smooth_quartz": (236, 230, 224),
    "prismarine": (99, 156, 151),
    "dark_prismarine": (51, 91, 75),
    "prismarine_bricks": (99, 171, 158),
    "sea_lantern": (172, 199, 190),
    "glowstone": (255, 214, 132),
    "shroomlight": (255, 166, 80),
    "obsidian": (20, 18, 29),
    "crying_obsidian": (44, 20, 60),
    "bedrock": (85, 85, 85),
    "coal_ore": (110, 110, 110),
    "iron_ore": (143, 130, 120),
    "copper_ore": (130, 130, 120),
    "gold_ore": (160, 137, 89),
    "redstone_ore": (140, 100, 100),
    "lapis_ore": (95, 108, 140),
    "diamond_ore": (120, 145, 148),
    "emerald_ore": (110, 145, 120),
    "iron_block": (220, 220, 220),
    "gold_block": (246, 208, 61),
    "copper_block": (192, 107, 79),
    "diamond_block": (98, 237, 228),
    "emerald_block": (52, 211, 123),
    "lapis_block": (38, 67, 137),
    "redstone_block": (171, 27, 9),
    "netherite_block": (66, 61, 63),
    "amethyst_block": (133, 97, 191),
    "copper": (192, 107, 79),
    "iron": (213, 213, 213),
    "gold": (246, 208, 61),
    "diamond": (98, 227, 218),
    "emerald": (52, 211, 123),
    "lapis": (38, 67, 137),
    "redstone": (171, 27, 9),
    "netherite": (66, 61, 63),
    "amethyst": (133, 97, 191),
    "water": (63, 118, 228),
    "lava": (207, 92, 20),
    "ice": (145, 183, 238),
    "packed_ice": (141, 180, 236),
    "blue_ice": (116, 167, 236),
    "snow": (240, 244, 246),
    "snow_block": (240, 244, 246),
    "hay_block": (176, 140, 23),
    "pumpkin": (196, 116, 24),
    "melon": (112, 156, 44),
    "moss_block": (89, 121, 46),
    "moss_carpet": (89, 121, 46),
    "sponge": (196, 192, 74),
    "wet_sponge": (152, 168, 60),
    "slime_block": (111, 192, 91),
    "honey_block": (234, 154, 39),
    "sculk": (24, 33, 40),
    "calcite": (223, 224, 220),
    "dripstone_block": (134, 107, 92),
    "pointed_dripstone": (134, 107, 92),
    "mud": (60, 57, 60),
    "packed_mud": (142, 106, 79),
    "smooth_basalt": (72, 72, 78),
    "ancient_debris": (94, 62, 50),
    "lodestone": (130, 130, 130),
    "resin_block": (222, 140, 54),
    "creaking_heart": (94, 84, 70),
}

# 木头/树叶等由材质名推导
_WOOD_TOKENS = ("planks", "wood", "log", "stem", "hyphae", "sign", "button", "pressure_plate",
                "fence", "fence_gate", "door", "trapdoor", "stairs", "slab", "stairs", "leaves",
                "sapling", "boat", "chest", "barrel", "bookshelf", "crafting_table", "lectern",
                "composter", "beehive", "scaffolding", "ladder")

_LEAF_COLORS: dict[str, tuple[int, int, int]] = {
    "oak": (88, 140, 52),
    "birch": (128, 166, 85),
    "spruce": (63, 102, 55),
    "jungle": (75, 156, 47),
    "acacia": (110, 156, 58),
    "dark_oak": (53, 92, 28),
    "mangrove": (76, 127, 51),
    "cherry": (231, 168, 189),
    "azalea": (93, 140, 61),
    "flowering_azalea": (110, 157, 75),
    "pale_oak": (143, 155, 118),
}

# 跳过渲染的方块（不画、也不遮挡后方）
_AIR_BLOCKS = {
    "air", "cave_air", "void_air", "structure_void", "light", "barrier", "moving_piston",
    "water", "flowing_water", "bubble_column", "seagrass", "tall_seagrass", "kelp", "kelp_plant",
    "nether_portal", "end_portal", "end_gateway", "fire", "soul_fire",
}

# 名字包含这些片段视为"装饰性/非完整方块"，默认跳过
_SKIP_SUBSTR = (
    "torch", "lantern", "candle", "flower", "sapling", "fern", "_bush", "sprout", "roots",
    "vine", "lily_pad", "carpet", "_rail", "button", "pressure_plate", "_sign", "banner",
    "ladder", "chain", "pane", "bars", "tripwire", "lever", "repeater", "comparator",
    "redstone_wire", "cobweb", "_coral", "coral_fan", "sculk_vein", "glow_lichen",
    "dripleaf", "spore_blossom", "mushroom", "sugar_cane", "bamboo", "wheat", "carrots",
    "potatoes", "beetroots", "melon_stem", "pumpkin_stem", "cocoa", "nether_wart",
    "sweet_berry", "cave_vines", "weeping_vines", "twisting_vines", "hanging_roots",
    "pink_petals", "leaf_litter", "wildflowers", "eyeblossom", "torchflower", "pitcher_plant",
    "big_dripleaf", "small_dripleaf", "azalea_leaves_flowers",
)

_GLASS_SUBSTR = ("glass",)


def _base_name(name: str) -> str:
    """'minecraft:oak_planks' -> 'oak_planks'，去掉命名空间和属性。"""
    n = str(name or "").strip().lower()
    if ":" in n:
        n = n.split(":", 1)[1]
    return n


def is_skipped_block(name: str) -> bool:
    """判断是否是不渲染的半透明/装饰方块。"""
    base = _base_name(name)
    if base in _AIR_BLOCKS:
        return True
    if base.endswith("glass") or base.endswith("glass_pane"):
        return True
    if any(tok in base for tok in _SKIP_SUBSTR):
        return True
    return False


def _hash_color(name: str) -> tuple[int, int, int]:
    """未知方块兜底颜色：按名字散列出柔和的颜色。"""
    h = hashlib.md5(name.encode("utf-8", "ignore")).digest()
    hue = h[0] / 255.0
    sat = 0.25 + (h[1] / 255.0) * 0.3
    light = 0.38 + (h[2] / 255.0) * 0.24
    # HSL -> RGB
    import colorsys

    r, g, b = colorsys.hls_to_rgb(hue, light, sat)
    return (int(r * 255), int(g * 255), int(b * 255))


def _darken(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(c * factor))) for c in color)  # type: ignore[return-value]


def _species_color(base: str) -> tuple[int, int, int] | None:
    for species, color in _WOOD_COLORS.items():
        if species in base:
            return color
    return None


def _heuristic_color(base: str) -> tuple[int, int, int]:
    """按方块名猜颜色：染色词 > 木头物种 > 材料词 > 哈希兜底。"""
    # 1) 染色
    for dye, color in _DYE_COLORS.items():
        if dye in base:
            if "concrete" in base:
                return _darken(color, 0.92)
            if "terracotta" in base:
                return _darken(color, 0.82)
            if "glass" in base:
                return _darken(color, 0.95)
            return color
    # 2) 树叶
    if "leaves" in base:
        for species, color in _LEAF_COLORS.items():
            if species in base:
                return color
        return (80, 130, 50)
    # 3) 材料词（先长词优先）
    for word in sorted(_MATERIAL_COLORS, key=len, reverse=True):
        if word in base:
            color = _MATERIAL_COLORS[word]
            if "ore" in base and word not in ("ore",):
                # 矿石：石头底 + 材料色
                return color
            return color
    # 4) 木头物种
    if any(tok in base for tok in _WOOD_TOKENS):
        species = _species_color(base)
        if species:
            if "log" in base or "stem" in base or "hyphae" in base:
                return _darken(species, 0.78)
            return species
    # 5) 铜氧化程度
    if "copper" in base:
        if "oxidized" in base:
            return (83, 162, 136)
        if "weathered" in base:
            return (108, 158, 124)
        if "exposed" in base:
            return (154, 120, 88)
        return _MATERIAL_COLORS["copper"]
    # 6) 兜底
    return _hash_color(base)


def resolve_block_color(name: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """返回 (顶面颜色, 侧面颜色)。优先使用 block_colors.json 的实测颜色。"""
    table = _load_color_table()
    key = str(name or "").strip().lower()
    entry = table.get(key)
    if entry is None:
        base = _base_name(key)
        entry = table.get("minecraft:" + base)
        if entry is None:
            base_color = _heuristic_color(base)
            return base_color, _darken(base_color, 0.86)
    try:
        top = _rgb(tuple(entry["top"]))  # type: ignore[index]
        side = _rgb(tuple(entry["side"]))  # type: ignore[index]
    except Exception:
        base_color = _heuristic_color(_base_name(key))
        return base_color, _darken(base_color, 0.86)
    return top, side


# --------------------------------------------------------------------------- #
# sprite 构建
# --------------------------------------------------------------------------- #


class _SpriteBuilder:
    """为每种方块生成 1 个 tile_w×tile_w 的等距立方体 sprite（顶面 + 左面 + 右面）。"""

    def __init__(self, tile_size: int):
        self.t = tile_size
        self.hw = tile_size // 2
        self.hh = tile_size // 4
        self.th = tile_size // 2

    def build(self, top_color: tuple[int, int, int], side_color: tuple[int, int, int],
              seed: str) -> tuple[np.ndarray, np.ndarray]:
        t, hw, hh = self.t, self.hw, self.hh
        ys, xs = np.mgrid[0:t, 0:t]
        px = xs.astype(np.float64) + 0.5
        py = ys.astype(np.float64) + 0.5

        # 顶面菱形（offset: top 顶点在 (hw,0)，参数 s/t ∈[0,1]）
        s = (px - hw) / hw
        tt = (py - s * hh) / (2.0 * hh)
        top_mask = (s >= 0.0) & (s <= 1.0) & (tt >= 0.0) & (tt <= 1.0)
        # 左面（+z 面）
        sl = px / hw
        tl = (py - hh - sl * hh) / (2.0 * hh)
        left_mask = (sl >= 0.0) & (sl <= 1.0) & (tl >= 0.0) & (tl <= 1.0)
        # 右面（+x 面）
        sr = (2.0 * hw - px) / hw
        tr = (py - hh - sr * hh) / (2.0 * hh)
        right_mask = (sr >= 0.0) & (sr <= 1.0) & (tr >= 0.0) & (tr <= 1.0)

        sprite = np.zeros((t, t, 3), dtype=np.float64)
        top = np.array(top_color, dtype=np.float64)
        side = np.array(side_color, dtype=np.float64)
        left_color = side * 0.62
        right_color = side * 0.82
        sprite[top_mask] = top
        sprite[left_mask] = left_color
        sprite[right_mask] = right_color

        # 每个方块类型固定的纹理噪声，避免纯色过于扁平
        digest = hashlib.md5(seed.encode("utf-8", "ignore")).digest()
        rng = np.random.default_rng(int.from_bytes(digest[:8], "little"))
        noise = rng.uniform(0.93, 1.05, size=(t, t, 1))
        sprite *= noise

        mask = top_mask | left_mask | right_mask
        # 外轮廓描边：让相邻方块有清晰边界（tile 太小时取消，避免画面全是线）
        if t >= 12:
            padded = np.pad(mask, 1, mode="constant", constant_values=False)
            erode = (padded[1:-1, 1:-1] & padded[:-2, 1:-1] & padded[2:, 1:-1]
                     & padded[1:-1, :-2] & padded[1:-1, 2:])
            edge = mask & ~erode
            sprite[edge] *= 0.80

        sprite_u8 = np.clip(sprite, 0, 255).astype(np.uint8)
        return sprite_u8, mask


# --------------------------------------------------------------------------- #
# 背景 / 字体 / 文案
# --------------------------------------------------------------------------- #


def _parse_color(value: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    v = (value or "").strip()
    if v.startswith("#") and len(v) == 7:
        try:
            return (int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16))
        except ValueError:
            return default
    return default


def _make_background(width: int, height: int, mode: str) -> np.ndarray:
    """生成竖直渐变背景（float32 RGB 画布）。"""
    mode = (mode or "dark").strip().lower()
    if mode == "light":
        top = np.array([235, 242, 250], dtype=np.float32)
        bottom = np.array([201, 215, 231], dtype=np.float32)
    elif mode == "dark":
        top = np.array([30, 36, 50], dtype=np.float32)
        bottom = np.array([15, 18, 25], dtype=np.float32)
    else:
        color = np.array(_parse_color(mode, (22, 26, 34)), dtype=np.float32)
        top = np.clip(color * 1.22, 0, 255)
        bottom = color * 0.82
    ramp = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
    grad = top[None, :] * (1.0 - ramp) + bottom[None, :] * ramp
    return np.repeat(grad[:, None, :], width, axis=1)


_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)
_font_cache: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
_cjk_font_ok: bool | None = None


def _get_font(size: int):
    global _cjk_font_ok
    if size in _font_cache:
        return _font_cache[size]
    font = None
    for path in _FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, size)
            _cjk_font_ok = True
            break
        except Exception:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size=size)
        except Exception:
            font = ImageFont.load_default()
        _cjk_font_ok = False
    _font_cache[size] = font
    return font


def _safe_text(text: str) -> str:
    if _cjk_font_ok is False:
        return text.encode("ascii", "replace").decode("ascii")
    return text


def _draw_caption(img: Image.Image, title: str, subtitle: str) -> None:
    if not title and not subtitle:
        return
    draw = ImageDraw.Draw(img, "RGBA")
    w, h = img.size
    title_font = _get_font(max(14, min(26, w // 45)))
    sub_font = _get_font(max(11, min(18, w // 70)))
    pad_x, pad_y = 18, 10
    texts = []
    if title:
        texts.append((_safe_text(title), title_font))
    if subtitle:
        texts.append((_safe_text(subtitle), sub_font))
    if not texts:
        return
    widths = [draw.textlength(t, font=f) for t, f in texts]
    # 标题过长时截断，避免文字框超出图片
    max_text_w = w - 32 - pad_x * 2
    if max_text_w > 40:
        for idx, ((text, font), tw) in enumerate(zip(texts, widths)):
            if tw > max_text_w:
                cut = text
                while cut and draw.textlength(cut + "…", font=font) > max_text_w:
                    cut = cut[:-1]
                texts[idx] = (cut + "…", font)
                widths[idx] = draw.textlength(texts[idx][0], font=font)
    heights = [draw.textbbox((0, 0), t, font=f)[3] for t, f in texts]
    box_w = int(max(widths)) + pad_x * 2
    box_h = sum(heights) + 6 * (len(texts) - 1) + pad_y * 2
    x0 = 16
    y0 = h - box_h - 16
    draw.rounded_rectangle([x0, y0, x0 + box_w, y0 + box_h], radius=10,
                           fill=(10, 12, 18, 170), outline=(255, 255, 255, 28), width=1)
    y = y0 + pad_y
    for (text, font), hh in zip(texts, heights):
        draw.text((x0 + pad_x, y), text, font=font, fill=(238, 242, 248, 235))
        y += hh + 6


# --------------------------------------------------------------------------- #
# 主渲染流程
# --------------------------------------------------------------------------- #


def _pick_tile_size(width: int, height: int, length: int, opts: RenderOptions) -> int:
    """在不超过最大图片尺寸的前提下，选择尽量大的 tile 宽度。"""
    candidates = [32, 28, 24, 20, 16, 12, 8, 4]
    preferred = max(4, min(32, int(opts.prefer_tile_size)))
    candidates = [c for c in candidates if c <= preferred] or [4]
    for tile_w in candidates:
        hw, hh, th = tile_w // 2, tile_w // 4, tile_w // 2
        img_w = (width + length) * hw + 2 * 24
        img_h = height * th + (width + length) * hh + 2 * 24
        if img_w <= opts.max_width and img_h <= opts.max_height:
            return tile_w
    return 4


# 渲染画布最多像素数（约 4000×4000，防止超大投影把内存打爆）
_MAX_CANVAS_PIXELS = 16_000_000


def render_schematic(
    blocks,
    palette: Iterable[str],
    width: int,
    height: int,
    length: int,
    *,
    title: str = "",
    subtitle: str = "",
    options: RenderOptions | None = None,
    format_name: str = "",
) -> RenderResult:
    """把扁平方块数组渲染成 PNG 预览图。

    参数：
        blocks: 长度为 width*height*length 的扁平数组，索引 (y*length+z)*width+x，
                值是 palette 下标；支持 array.array / numpy.ndarray / list。
        palette: 方块名列表（如 ["minecraft:air", "minecraft:stone"]）。
        width/height/length: X/Y/Z 尺寸。
        title/subtitle: 图片左下角文案。
    """
    started = time.time()
    opts = options or RenderOptions()
    width, height, length = int(width), int(height), int(length)
    if width <= 0 or height <= 0 or length <= 0:
        raise RenderError("投影尺寸为空，无法渲染。")

    total = width * height * length
    arr = np.asarray(blocks, dtype=np.int64)
    if arr.size != total:
        raise RenderError(f"方块数组长度 {arr.size} 与尺寸 {width}×{height}×{length} 不一致。")
    arr = arr.reshape(height, length, width).astype(np.int32)

    palette = list(palette)
    if not palette:
        raise RenderError("投影调色板为空，无法渲染。")

    # 每个 palette 项的分类与颜色
    uniq = np.unique(arr)
    n_uniq = len(uniq)
    id_map = np.full(int(max(palette.__len__(), int(uniq[-1]) + 1)), -1, dtype=np.int64)
    id_map[uniq] = np.arange(n_uniq)
    inv = id_map[arr]  # shape (H, L, W) 映射到 0..n_uniq-1

    draw_flags = np.zeros(n_uniq, dtype=bool)
    colors_top = np.zeros((n_uniq, 3), dtype=np.uint8)
    colors_side = np.zeros((n_uniq, 3), dtype=np.uint8)
    for local_id, pid in enumerate(uniq.tolist()):
        name = palette[pid] if 0 <= pid < len(palette) else "minecraft:air"
        if is_skipped_block(name) and opts.skip_decorations:
            continue
        top, side = resolve_block_color(name)
        draw_flags[local_id] = True
        colors_top[local_id] = top
        colors_side[local_id] = side

    solid = draw_flags[inv]
    if not solid.any():
        raise RenderError("投影里没有可渲染的方块（可能全是空气/水/装饰方块）。")

    # 只保留外壳方块（6 个邻居都是实心方块则跳过）
    op = np.zeros((height + 2, length + 2, width + 2), dtype=bool)
    op[1:-1, 1:-1, 1:-1] = solid
    occluded = (
        op[0:height, 1:length + 1, 1:width + 1] & op[2:height + 2, 1:length + 1, 1:width + 1]
        & op[1:height + 1, 0:length, 1:width + 1] & op[1:height + 1, 2:length + 2, 1:width + 1]
        & op[1:height + 1, 1:length + 1, 0:width] & op[1:height + 1, 1:length + 1, 2:width + 2]
    )
    visible = solid & ~occluded
    count = int(visible.sum())
    if count == 0:
        raise RenderError("投影里没有可渲染的方块（可能全是空气/水/装饰方块）。")
    if count > opts.max_visible_blocks:
        raise RenderError(
            f"投影可见方块数 {count:,} 超过上限 {opts.max_visible_blocks:,}，"
            "可在插件配置里调大 max_visible_blocks 或裁剪投影后重试。"
        )

    pad = max(14, min(40, opts.prefer_tile_size))
    tile_w = _pick_tile_size(width, height, length, opts)
    hw, hh, th = tile_w // 2, tile_w // 4, tile_w // 2
    render_w = (width + length) * hw + pad * 2
    render_h = height * th + (width + length) * hh + pad * 2
    if render_w * render_h > _MAX_CANVAS_PIXELS:
        raise RenderError(
            f"投影过大（{width}×{height}×{length}），渲染画布约 {render_w}×{render_h} 像素，"
            "请裁剪投影后重试。"
        )
    img_w, img_h = render_w, render_h

    canvas = _make_background(img_w, img_h, opts.background)

    offset_x = length * hw + pad
    offset_y = height * th + pad

    # 坐标（向量化）
    ys, zs, xs = np.nonzero(visible)
    depth = xs + zs + ys
    order = np.argsort(depth, kind="stable")
    xs = xs[order].astype(np.int64)
    zs = zs[order].astype(np.int64)
    ys = ys[order].astype(np.int64)
    local_id = inv[ys, zs, xs]

    sx = (xs - zs) * hw + offset_x - hw
    sy = (xs + zs) * hh - ys * th + offset_y - th

    # sprite 缓存（每种出现的方块只构建一次）
    builder = _SpriteBuilder(tile_w)
    sprites: list[np.ndarray] = []
    masks: list[np.ndarray] = []
    remap: dict[int, int] = {}
    for lid in np.unique(local_id).tolist():
        top = tuple(int(v) for v in colors_top[lid])
        side = tuple(int(v) for v in colors_side[lid])
        name = str(palette[int(uniq[lid])]) if 0 <= int(uniq[lid]) < len(palette) else str(lid)
        spr, mask = builder.build(top, side, _base_name(name))
        remap[int(lid)] = len(sprites)
        sprites.append(spr)
        masks.append(mask)

    # 底部阴影：在结构下方画一层暗色菱形（在方块之前画，会被方块覆盖）
    if opts.add_shadow:
        _draw_ground_shadow(canvas, width, length, hw, hh, offset_x, offset_y)

    sx_l = sx.tolist()
    sy_l = sy.tolist()
    lid_l = local_id.tolist()
    tile = tile_w
    vis_count = len(sx_l)
    img_h, img_w = canvas.shape[:2]
    for i in range(vis_count):
        x0 = sx_l[i]
        y0 = sy_l[i]
        if x0 < 0 or y0 < 0 or x0 + tile > img_w or y0 + tile > img_h:
            continue
        si = remap[lid_l[i]]
        np.copyto(canvas[y0:y0 + tile, x0:x0 + tile], sprites[si],
                  where=masks[si][:, :, None])

    img = Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8), mode="RGB")
    if img_w > opts.max_width or img_h > opts.max_height:
        scale = min(opts.max_width / img_w, opts.max_height / img_h)
        img = img.resize((max(1, int(img_w * scale)), max(1, int(img_h * scale))),
                         Image.LANCZOS)
    if opts.show_caption and (title or subtitle):
        _draw_caption(img, title, subtitle)
    buf = _BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return RenderResult(
        png=buf.getvalue(),
        width=img.size[0],
        height=img.size[1],
        tile_size=tile_w,
        visible_blocks=vis_count,
        total_blocks=int(solid.sum()),
        elapsed=time.time() - started,
        extra={"format": format_name},
    )


def _draw_ground_shadow(canvas: np.ndarray, width: int, length: int, hw: int, hh: int,
                        offset_x: int, offset_y: int) -> None:
    """在方块底部平面上画一个半透明暗色底座，增强立体感。"""
    img_h, img_w = canvas.shape[:2]
    # 底面菱形中心与半宽/半高
    center_x = (width - length) * hw / 2.0 + offset_x
    center_y = (width + length) * hh / 2.0 + offset_y
    half_w = (width + length) * hw / 2.0
    half_h = (width + length) * hh / 2.0
    if half_w <= 0 or half_h <= 0:
        return
    x_min = max(0, int(center_x - half_w) - 2)
    x_max = min(img_w, int(center_x + half_w) + 3)
    y_min = max(0, int(center_y - half_h) - 2)
    y_max = min(img_h, int(center_y + half_h) + 3)
    if x_max <= x_min or y_max <= y_min:
        return
    ys, xs = np.mgrid[y_min:y_max, x_min:x_max]
    dx = xs - center_x
    dy = ys - center_y
    inside = (np.abs(dx) / half_w + np.abs(dy) / half_h) <= 1.0
    if not inside.any():
        return
    region = canvas[y_min:y_max, x_min:x_max]
    shade = np.array([6, 8, 12], dtype=np.float32)
    region[inside] = region[inside] * 0.55 + shade * 0.45


def _BytesIO():
    import io

    return io.BytesIO()


def render_from_parsed(parsed, *, title: str = "", subtitle: str = "",
                       options: RenderOptions | None = None) -> RenderResult:
    """对 schematic_parser.ParsedSchematic 做渲染（鸭子类型，避免循环 import）。"""
    if title == "":
        title = getattr(parsed, "name", "") or ""
    return render_schematic(
        parsed.blocks,
        parsed.palette,
        parsed.width,
        parsed.height,
        parsed.length,
        title=title,
        subtitle=subtitle,
        options=options,
        format_name=getattr(parsed, "format", ""),
    )
