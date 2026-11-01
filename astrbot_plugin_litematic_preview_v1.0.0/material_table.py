"""把材料清单渲染成适合 QQ 查看的表格图片（PNG，分页）。

不依赖 AstrBot，只依赖 Pillow；供插件在 `material_list_mode=image` 时调用。
表格优先使用 Windows 自带中文字体（微软雅黑/黑体），找不到时退回 PIL 默认字体。
"""

from __future__ import annotations

import io
import math
import os
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

_FONT_CANDIDATES = [
    (r"C:\Windows\Fonts\msyh.ttc", 0),
    (r"C:\Windows\Fonts\msyhbd.ttc", 0),
    (r"C:\Windows\Fonts\simhei.ttf", 0),
    (r"C:\Windows\Fonts\simsun.ttc", 0),
    ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", 0),
    ("/System/Library/Fonts/PingFang.ttc", 0),
]

# 版式
PAGE_WIDTH = 1120
MARGIN = 28
ROW_HEIGHT = 34
HEADER_HEIGHT = 42
PAGE_ROWS = 42
BG = (250, 251, 253)
CARD = (255, 255, 255)
BORDER = (214, 220, 230)
HEAD_BG = (30, 41, 59)
HEAD_FG = (255, 255, 255)
TEXT = (30, 41, 59)
MUTED = (100, 116, 139)
ZEBRA = (244, 247, 251)
COUNT_FG = (13, 110, 92)

COLUMNS = [
    ("序号", 54, "center"),
    ("物品名称", 330, "left"),
    ("物品 ID", 340, "left"),
    ("数量", 140, "right"),
    ("折合（盒/组/个）", 198, "left"),
]


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = _FONT_CANDIDATES if not bold else (
        [(r"C:\Windows\Fonts\msyhbd.ttc", 0), (r"C:\Windows\Fonts\simhei.ttf", 0)] + _FONT_CANDIDATES
    )
    for path, index in candidates:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size, index=index)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10
        return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    try:
        return int(draw.textlength(text, font=font))
    except AttributeError:  # pragma: no cover - 老版本 Pillow
        return int(font.getsize(text)[0])


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    if _text_width(draw, text, font) <= max_width:
        return text
    ellipsis = "…"
    left, right = 0, len(text)
    while left < right:
        mid = (left + right) // 2
        candidate = text[:mid] + ellipsis
        if _text_width(draw, candidate, font) <= max_width:
            left = mid + 1
        else:
            right = mid
    return text[: max(1, left - 1)] + ellipsis


def _format_pack(entry) -> str:
    """把数量折合成「盒 + 组 + 个」。"""
    count = int(entry.count)
    stack = int(entry.stack_size) if int(entry.stack_size) > 0 else 64
    if stack <= 1:
        return f"{count:,} 个"
    per_box = stack * 27
    boxes, rest = divmod(count, per_box)
    stacks, items = divmod(rest, stack)
    parts: list[str] = []
    if boxes:
        parts.append(f"{boxes:,} 盒")
    if stacks or boxes:
        parts.append(f"{stacks} 组")
    if items or not parts:
        parts.append(f"{items} 个")
    return " + ".join(parts)


def _summary_lines(report, title: str, parsed) -> list[tuple[str, tuple[int, int, int], int, bool]]:
    """返回 (文本, 颜色, 字号, 是否粗体)。"""
    name = (getattr(parsed, "name", "") or "").strip() or "(未命名)"
    width = int(getattr(parsed, "width", 0) or 0)
    height = int(getattr(parsed, "height", 0) or 0)
    length = int(getattr(parsed, "length", 0) or 0)
    return [
        (f"投影材料清单", HEAD_BG, 30, True),
        (f"{name} · {width}×{height}×{length} · 非空气方块 {int(getattr(parsed, 'total_blocks', 0) or 0):,}",
         MUTED, 16, False),
        (f"共 {report.total_types} 种 / {report.total_items:,} 个 · 数据版本 {report.dataset_version or '?'}",
         COUNT_FG, 17, True),
    ]


def render_material_table_pages(
    report, parsed=None, *, page_rows: int = PAGE_ROWS, width: int = PAGE_WIDTH,
    title: str = "",
) -> list[bytes]:
    """把 MaterialReport 渲染成若干张表格 PNG（每页最多 page_rows 行）。"""
    items = list(report.items or [])
    if not items:
        return []
    page_rows = max(10, int(page_rows or PAGE_ROWS))
    width = max(760, int(width or PAGE_WIDTH))

    font_cell = _load_font(17)
    font_cell_bold = _load_font(17, bold=True)
    font_small = _load_font(14)
    font_title = _load_font(30, bold=True)
    title_lines = _summary_lines(report, title, parsed)
    title_lines[0] = (title_lines[0][0], title_lines[0][1], title_lines[0][2], True)
    if title:
        title_lines[0] = (title, title_lines[0][1], title_lines[0][2], True)

    notes: list[str] = []
    if report.unresolved_blocks:
        examples = "、".join(list(report.unresolved_detail)[:5])
        notes.append(f"未计入：{report.unresolved_blocks:,} 个方块无法对应物品"
                     + (f"（示例：{examples}）" if examples else ""))
    if report.estimated_blocks:
        notes.append(f"估算：{report.estimated_blocks:,} 个新版本方块按同名物品计算")

    pages = math.ceil(len(items) / page_rows)
    images: list[bytes] = []
    for page in range(pages):
        chunk = items[page * page_rows : (page + 1) * page_rows]
        # 动态列宽：物品名称/ID 按剩余宽度分配
        fixed = sum(col[1] for col in COLUMNS)
        table_w = max(760, width - MARGIN * 2)
        if fixed > table_w:
            scale = table_w / fixed
            cols = [(name, max(40, int(col_w * scale)), align) for name, col_w, align in COLUMNS]
        else:
            extra = table_w - fixed
            cols = []
            for name, col_w, align in COLUMNS:
                if name == "物品名称":
                    col_w += int(extra * 0.55)
                elif name == "物品 ID":
                    col_w += int(extra * 0.45)
                cols.append((name, col_w, align))
        table_w = sum(col[1] for col in cols)

        header_h = 26 + 3 * 28
        table_h = HEADER_HEIGHT + len(chunk) * ROW_HEIGHT
        footer_h = 34 + (len(notes) * 20 if page == pages - 1 else 0)
        height = MARGIN * 2 + header_h + 18 + table_h + footer_h

        img = Image.new("RGB", (width, height), BG)
        draw = ImageDraw.Draw(img)
        # 顶部信息
        y = MARGIN
        for text, color, size, bold in title_lines:
            font = font_title if size >= 26 else (_load_font(size, bold=bold))
            draw.text((MARGIN, y), text, font=font, fill=color)
            y += (font.size if hasattr(font, "size") else size) + 8
        # 页信息
        page_text = f"第 {page + 1}/{pages} 页 · 共 {len(items)} 种"
        draw.text((width - MARGIN - _text_width(draw, page_text, font_small), MARGIN + 8),
                  page_text, font=font_small, fill=MUTED)

        # 表格卡片
        card_y = MARGIN + header_h + 18
        draw.rounded_rectangle(
            (MARGIN, card_y, MARGIN + table_w, card_y + table_h),
            radius=10, fill=CARD, outline=BORDER, width=1,
        )
        # 表头
        x = MARGIN
        draw.rectangle((MARGIN, card_y, MARGIN + table_w, card_y + HEADER_HEIGHT), fill=HEAD_BG)
        for name, col_w, align in cols:
            text_w = _text_width(draw, name, font_cell_bold)
            tx = x + (col_w - text_w) // 2 if align == "center" else (
                x + col_w - text_w - 12 if align == "right" else x + 12
            )
            draw.text((tx, card_y + (HEADER_HEIGHT - 20) // 2), name, font=font_cell_bold, fill=HEAD_FG)
            x += col_w
        # 行
        for row_index, entry in enumerate(chunk):
            row_y = card_y + HEADER_HEIGHT + row_index * ROW_HEIGHT
            if row_index % 2 == 1:
                draw.rectangle((MARGIN, row_y, MARGIN + table_w, row_y + ROW_HEIGHT), fill=ZEBRA)
            values = [
                str(page * page_rows + row_index + 1),
                entry.display_name or entry.item_id,
                entry.full_id,
                f"{entry.count:,}",
                _format_pack(entry),
            ]
            x = MARGIN
            for (name, col_w, align), value in zip(cols, values):
                font = font_cell_bold if name == "数量" else font_cell
                color = COUNT_FG if name == "数量" else (MUTED if name == "物品 ID" else TEXT)
                text = _ellipsize(draw, value, font, col_w - 16)
                text_w = _text_width(draw, text, font)
                if align == "center":
                    tx = x + (col_w - text_w) // 2
                elif align == "right":
                    tx = x + col_w - text_w - 12
                else:
                    tx = x + 10
                draw.text((tx, row_y + (ROW_HEIGHT - 19) // 2), text, font=font, fill=color)
                x += col_w
            if row_index < len(chunk) - 1:
                draw.line((MARGIN + 8, row_y + ROW_HEIGHT, MARGIN + table_w - 8, row_y + ROW_HEIGHT),
                          fill=(235, 239, 244), width=1)

        # 底部说明
        footer_y = card_y + table_h + 14
        if page == pages - 1 and notes:
            for note in notes:
                draw.text((MARGIN, footer_y), note, font=font_small, fill=MUTED)
                footer_y += 20
        draw.text(
            (MARGIN, footer_y),
            "提示：数量按方块状态统计（双台阶×2、雪层按层数、门/床只算一次）；容器/实体内容未计入。",
            font=font_small, fill=MUTED,
        )

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        images.append(buf.getvalue())
    return images
