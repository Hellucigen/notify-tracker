#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把两张 CSV 转成 xlsx，方便在表格查看器里看（表头加粗、冻结首行、自动列宽）。"""

from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
import csv

HERE = Path(__file__).resolve().parent
JOBS = [
    (HERE / "logs" / "notifications.csv", HERE / "logs" / "notifications.xlsx", "常驻记录"),
    (HERE / "logs" / "carved.csv", HERE / "logs" / "carved.xlsx", "已被系统删除"),
    (HERE / "recovered" / "history.csv", HERE / "recovered" / "history.xlsx", "含已恢复"),
]


def convert(src: Path, dst: Path, sheet: str):
    if not src.exists():
        print(f"跳过 {src.name}（文件不存在）")
        return
    rows = list(csv.reader(open(src, encoding="utf-8-sig", newline="")))
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for row in rows:
        ws.append(row)
    ws.freeze_panes = "A2"
    for cell in ws[1]:
        cell.font = Font(bold=True)
    widths = []
    for index in range(1, ws.max_column + 1):
        longest = max((len(str(ws.cell(row=r, column=index).value or "")) for r in range(1, min(ws.max_row, 60) + 1)), default=8)
        widths.append(min(60, max(8, longest + 2)))
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    wb.save(dst)
    print(f"{src.name} -> {dst.name}  ({len(rows) - 1} 行)")


if __name__ == "__main__":
    for source, target, sheet in JOBS:
        convert(source, target, sheet)
