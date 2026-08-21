"""XLSX export for the Monthly Performance table (task-54 §7): byte-identical
color scheme to the operator's own Google Sheet ("1 в 1") — the same header
colors, the same column order, and MoM direction expressed as colored text
(no arrow glyph, since the source sheet has none either) rather than
conditional-formatting rules, so the file opens identically in Excel,
LibreOffice and Google Sheets.
"""

from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.reports.monthly_table import MonthlyRow

# Standard Google Sheets header palette, sampled from the operator's own
# sheet — matches `frontend/css/monthly-table.css`'s `--mt-header-*` tokens
# exactly, so the on-screen table and this export never drift apart.
_HEADER_GREEN = "93C47D"
_HEADER_YELLOW = "FFFF00"
_HEADER_BLUE = "6D9EEB"
_HEADER_RED = "E06666"
_HEADER_MONTH_BG = "D9D9D9"
_HEADER_FG = "000000"
_ALT_ROW_BG = "F3F3F3"
_UP_COLOR = "1A7A3C"
_DOWN_COLOR = "CC0000"

# "0" (no digit grouping) for every column except Impressions — task-54 §3.3:
# the operator's own sheet renders `1208`, not `1,208`, everywhere but there.
_FMT_PLAIN = "0"
_FMT_GROUPED = "#,##0"
_FMT_PCT = "0.00%"

_COLUMNS: tuple[tuple[str, str | None, str, str], ...] = (
    ("organic_users", "Total organic Users", _HEADER_GREEN, _FMT_PLAIN),
    ("gsc_impressions", "Impressions", _HEADER_YELLOW, _FMT_GROUPED),
    ("gsc_clicks", "Clicks", _HEADER_BLUE, _FMT_PLAIN),
    ("organic_conversions", "Organic Conversions", _HEADER_RED, _FMT_PLAIN),
    ("conversion_rate", "Conversion rate", _HEADER_GREEN, _FMT_PCT),
    ("key_event_count", None, _HEADER_GREEN, _FMT_PLAIN),
    ("ctr", "CTR", _HEADER_BLUE, _FMT_PCT),
)


def _fill(hex_color: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_color)


def _direction_font(direction: str | None) -> Font:
    if direction == "up":
        return Font(name="Calibri", color=_UP_COLOR)
    if direction == "down":
        return Font(name="Calibri", color=_DOWN_COLOR)
    return Font(name="Calibri")


def build_monthly_worksheet(
    ws: Worksheet,
    rows: list[MonthlyRow],
    *,
    key_event_label: str | None,
    has_key_event_column: bool,
) -> None:
    columns = [c for c in _COLUMNS if c[0] != "key_event_count" or has_key_event_column]

    ws.cell(1, 1, "")
    ws.cell(1, 1).fill = _fill(_HEADER_MONTH_BG)
    for col_index, (_key, label, color, _fmt) in enumerate(columns, start=2):
        header_text = label or f'Organic "{key_event_label or "Event"}"'
        cell = ws.cell(1, col_index, header_text)
        cell.font = Font(name="Calibri", bold=True, color=_HEADER_FG, size=11)
        cell.fill = _fill(color)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "B2"

    for row_index, row in enumerate(rows, start=2):
        alt = row_index % 2 == 0
        month_cell = ws.cell(row_index, 1, row.label)
        if alt:
            month_cell.fill = _fill(_ALT_ROW_BG)
        for col_index, (key, _label, _color, number_format) in enumerate(columns, start=2):
            value = getattr(row, key)
            cell = ws.cell(row_index, col_index)
            if alt:
                cell.fill = _fill(_ALT_ROW_BG)
            if value is None:
                # PLAN §12 — a missing value is an em dash, never a blank cell
                # that reads as "zero" once pasted somewhere else.
                cell.value = "—"
                cell.alignment = Alignment(horizontal="right")
                continue
            cell.value = value
            cell.number_format = number_format
            cell.font = _direction_font(row.directions.get(key))
            cell.alignment = Alignment(horizontal="right")

    ws.column_dimensions["A"].width = 16
    for col_index in range(2, len(columns) + 2):
        ws.column_dimensions[get_column_letter(col_index)].width = 15


def build_monthly_xlsx(
    rows: list[MonthlyRow], *, key_event_label: str | None, has_key_event_column: bool
) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Monthly Performance"
    build_monthly_worksheet(
        worksheet, rows, key_event_label=key_event_label, has_key_event_column=has_key_event_column
    )
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
