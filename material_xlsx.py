"""生成材料清单 Excel（.xlsx）文件。

AstrBot 自带 openpyxl（requirements: openpyxl>=3.1.5），这里直接使用；
生成三个工作表：
1. 材料清单：需合成/准备的物品与数量；
2. 方块统计：原始方块（ID + 方块状态）数量；
3. 投影信息：尺寸、方块数、统计口径等元信息。
"""

from __future__ import annotations

import io
import os
import time

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import materials

HEADER_FILL = PatternFill("solid", fgColor="1E293B")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
TITLE_FONT = Font(bold=True, size=14, color="1E293B")
MUTED_FONT = Font(color="64748B", size=10)
COUNT_FONT = Font(bold=True, color="0D6E5C")
THIN = Side(style="thin", color="E2E8F0")
BORDER = Border(bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center")
RIGHT = Alignment(horizontal="right", vertical="center")


def _pack_text(entry) -> str:
    count = int(entry.count)
    stack = int(entry.stack_size) if int(entry.stack_size) > 0 else 64
    if stack <= 1:
        return f"{count:,} 个"
    per_box = stack * 27
    boxes, rest = divmod(count, per_box)
    stacks, items = divmod(rest, stack)
    parts: list[str] = []
    if boxes:
        parts.append(f"{boxes} 盒")
    if stacks or boxes:
        parts.append(f"{stacks} 组")
    if items or not parts:
        parts.append(f"{items} 个")
    return " + ".join(parts)


def _style_sheet(ws, widths: list[int], freeze: str = "A2") -> None:
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.freeze_panes = freeze
    ws.auto_filter.ref = ws.dimensions
    for cell in ws[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = CENTER
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.border = BORDER


def build_material_workbook(report, parsed=None, filename: str = "") -> bytes:
    """把 MaterialReport（可选 parsed）生成 xlsx 字节内容。"""
    wb = Workbook()
    wb.properties.creator = "astrbot_plugin_litematic_preview"
    wb.properties.title = "Minecraft 投影材料清单"
    wb.properties.description = "由 Minecraft 投影文件自动统计，规则参考 Litematica 行为。"

    # ---------------- 材料清单 ----------------
    ws = wb.active
    ws.title = "材料清单"
    headers = ["序号", "物品名称", "物品ID", "数量", "组数", "潜影盒数", "折合", "堆叠上限"]
    ws.append(headers)
    for index, entry in enumerate(report.items, start=1):
        ws.append([
            index,
            entry.display_name or entry.item_id,
            entry.full_id,
            int(entry.count),
            int(entry.stacks),
            int(entry.shulker_boxes),
            _pack_text(entry),
            int(entry.stack_size),
        ])
    for row in ws.iter_rows(min_row=2, min_col=4, max_col=6):
        for cell in row:
            cell.number_format = "#,##0"
            cell.alignment = RIGHT
    _style_sheet(ws, [7, 26, 34, 13, 10, 12, 20, 10])

    # ---------------- 方块统计 ----------------
    ws2 = wb.create_sheet("方块统计")
    ws2.append(["序号", "方块ID", "方块状态", "数量"])
    for index, block in enumerate(materials.count_block_states(parsed), start=1):
        ws2.append([index, block.block_id, block.state_text, int(block.count)])
    for row in ws2.iter_rows(min_row=2, min_col=4, max_col=4):
        for cell in row:
            cell.number_format = "#,##0"
            cell.alignment = RIGHT
    _style_sheet(ws2, [7, 42, 46, 14])

    # ---------------- 投影信息 ----------------
    ws3 = wb.create_sheet("投影信息")
    name = (getattr(parsed, "name", "") or "").strip() if parsed is not None else ""
    size = ""
    if parsed is not None:
        size = f"{int(getattr(parsed, 'width', 0) or 0)} × {int(getattr(parsed, 'height', 0) or 0)} × {int(getattr(parsed, 'length', 0) or 0)}"
    rows = [
        ("投影文件", filename or "-"),
        ("投影名称", name or "(未命名)"),
        ("尺寸（X×Y×Z）", size or "-"),
        ("非空气方块数", int(getattr(parsed, "total_blocks", 0) or 0) if parsed is not None else report.block_count),
        ("材料种类", report.total_types),
        ("材料合计数量", report.total_items),
        ("数据版本", report.dataset_version or "-"),
        ("生成时间", time.strftime("%Y-%m-%d %H:%M:%S")),
        ("未计入方块（无法对应物品）", report.unresolved_blocks),
        ("按同名物品估算的方块", report.estimated_blocks),
        ("已忽略方块（空气/技术方块等）", report.ignored_blocks),
        ("统计口径", "参考 Litematica：双台阶×2、雪层按层数、门/床/高草只算一次、盆栽=花盆+植物；"
                     "容器/物品展示框/实体内容未计入。"),
    ]
    ws3.append(["项目", "值"])
    for key, value in rows:
        ws3.append([key, value])
    ws3["A1"].font = TITLE_FONT
    for row in ws3.iter_rows(min_row=3):
        row[0].font = Font(bold=True)
    for row in ws3.iter_rows(min_row=3, min_col=2, max_col=2):
        for cell in row:
            if isinstance(cell.value, int) and cell.value > 1000:
                cell.number_format = "#,##0"
    ws3.column_dimensions["A"].width = 30
    ws3.column_dimensions["B"].width = 78
    for row in ws3.iter_rows(min_row=3):
        row[0].border = BORDER
        row[1].border = BORDER
        row[1].alignment = Alignment(wrap_text=True, vertical="center")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
