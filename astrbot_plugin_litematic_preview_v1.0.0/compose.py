"""把多张渲染结果拼成带标签的总览图（用于"四对角视角"等模式）。

只依赖 Pillow；输入输出都是 PNG bytes，方便和 Java 渲染结果、AstrBot 图片组件对接。
"""

from __future__ import annotations

import io
import os
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

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


def _get_font(size: int):
    if size in _font_cache:
        return _font_cache[size]
    font = None
    for path in _FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, size)
            break
        except Exception:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size=size)
        except Exception:
            font = ImageFont.load_default()
    _font_cache[size] = font
    return font


def _parse_color(value: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    v = (value or "").strip()
    if v.startswith("#") and len(v) == 7:
        try:
            return int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16)
        except ValueError:
            return default
    return default


def compose_views(
    images: Iterable[bytes],
    labels: Iterable[str] = (),
    *,
    columns: int = 2,
    padding: int = 16,
    gap: int = 16,
    label_height: int = 42,
    background: str = "#0e1016",
    transparent: bool = False,
    max_size: int = 1800,
    trim: bool = False,
) -> bytes:
    """把多张等尺寸 PNG 拼成网格总览图（每个格子上方带标签），返回 PNG bytes。

    Args:
        images: PNG 字节列表（尺寸可以不同，会取最大边长作为格大小）。
        labels: 与 images 等长的标签；空字符串表示该格不画标签。
        columns: 列数（4 张图默认 2×2）。
        padding: 画布四周留白。
        gap: 格子间距。
        label_height: 每个格子上方标签条高度；为 0 时不画标签。
        background: 不透明背景色（transparent=False 时生效）。
        transparent: True 时输出透明背景 RGBA 图。
        max_size: 输出图最长边上限，超过则等比缩小。
        trim: True 时按 alpha 通道裁掉每张图的空白边缘（透明背景默认应开启，
            可让主体在格子里更大）。
    """
    image_list = [Image.open(io.BytesIO(data)).convert("RGBA") for data in images]
    if not image_list:
        raise ValueError("compose_views 需要至少一张图片")
    if trim:
        trimmed = []
        for img in image_list:
            bbox = img.getbbox()
            trimmed.append(img.crop(bbox) if bbox else img)
        image_list = trimmed
    label_list = list(labels)
    while len(label_list) < len(image_list):
        label_list.append("")

    cols = max(1, int(columns))
    rows = (len(image_list) + cols - 1) // cols
    label_h = max(0, int(label_height)) if any(label_list) else 0

    # 先按 max_size 反推每个格子允许的最大边长，必要时先缩小图片再建画布，
    # 避免高分辨率（如 4096/8192）四视角拼图时分配几十 GB 的中间画布。
    cell = max(max(img.width, img.height) for img in image_list)
    if max_size:
        fixed_w = padding * 2 + (cols - 1) * gap
        fixed_h = padding * 2 + rows * label_h + (rows - 1) * gap
        max_cell_w = max(1, (int(max_size) - fixed_w) // cols)
        max_cell_h = max(1, (int(max_size) - fixed_h) // rows)
        target_cell = min(cell, max_cell_w, max_cell_h)
        if target_cell < cell:
            ratio = target_cell / float(cell)
            resized = []
            for img in image_list:
                size = (
                    max(1, int(img.width * ratio)),
                    max(1, int(img.height * ratio)),
                )
                resized.append(img.resize(size, Image.LANCZOS) if size != img.size else img)
            image_list = resized
            cell = target_cell

    width = padding * 2 + cols * cell + (cols - 1) * gap
    height = padding * 2 + rows * (cell + label_h) + (rows - 1) * gap

    if transparent:
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    else:
        rgb = _parse_color(background, (14, 16, 22))
        canvas = Image.new("RGB", (width, height), rgb)

    draw = ImageDraw.Draw(canvas, "RGBA")
    label_font = _get_font(max(16, min(30, cell // 22))) if label_h else None
    badge_pad_x, badge_pad_y = 14, 6

    for index, img in enumerate(image_list):
        row, col = divmod(index, cols)
        x = padding + col * (cell + gap)
        y = padding + row * (cell + label_h + gap)
        # 图片等比缩放居中放进格子
        scaled = img
        if img.width != cell or img.height != cell:
            ratio = min(cell / img.width, cell / img.height)
            scaled = img.resize(
                (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                Image.LANCZOS,
            )
        paste_x = x + (cell - scaled.width) // 2
        paste_y = y + label_h + (cell - scaled.height) // 2
        canvas.paste(scaled, (paste_x, paste_y), scaled)

        text = label_list[index]
        if label_h and text:
            font = label_font or _get_font(18)
            bbox = draw.textbbox((0, 0), text, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            badge_w = text_w + badge_pad_x * 2
            badge_h = text_h + badge_pad_y * 2
            bx = x + (cell - badge_w) // 2
            by = y + max(0, (label_h - badge_h) // 2)
            draw.rounded_rectangle(
                [bx, by, bx + badge_w, by + badge_h],
                radius=min(10, badge_h // 2),
                fill=(8, 10, 14, 165),
                outline=(255, 255, 255, 35),
                width=1,
            )
            draw.text(
                (bx + badge_pad_x, by + badge_pad_y - bbox[1]),
                text,
                font=font,
                fill=(240, 244, 250, 245),
            )

    longest = max(canvas.width, canvas.height)
    if max_size and longest > max_size:
        ratio = max_size / longest
        size = (max(1, int(canvas.width * ratio)), max(1, int(canvas.height * ratio)))
        canvas = canvas.resize(size, Image.LANCZOS)

    buf = io.BytesIO()
    if transparent:
        canvas.save(buf, format="PNG", optimize=True)
    else:
        canvas.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def trim_png(data: bytes, margin: int = 4) -> bytes:
    """按 alpha 通道裁掉 PNG 的空白边缘（用于透明背景单图发送），返回 PNG bytes。"""
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    bbox = img.getbbox()
    if bbox:
        left, top, right, bottom = bbox
        left = max(0, left - margin)
        top = max(0, top - margin)
        right = min(img.width, right + margin)
        bottom = min(img.height, bottom + margin)
        img = img.crop((left, top, right, bottom))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
