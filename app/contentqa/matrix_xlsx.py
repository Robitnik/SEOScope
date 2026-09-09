"""Excel-експорт порівняльної матриці (task-91 §1.8/§3.5).

Той самий принцип, що `app/pageinventory/xlsx.py`/`app/reports/monthly_xlsx.py`
(докстрінг `pageinventory/xlsx.py`): білдер читає `list[MatrixRowDTO]` —
той самий DTO, що `GET /reports/{id}` віддає екрану, не внутрішній
`MatrixRow` dataclass (`app/contentqa/matrix.py`) напряму. Свідомо:
`/reports/{id}/matrix.xlsx` читає вже ПЕРСИСТОВАНУ (в `content_qa_
packages.matrix_json`) матрицю з БД, а не перераховує її заново викликом
Drive/Docs API — DTO є в обох джерелах (щойно з `build_matrix()` через
`asdict()`, і з БД як є), тоді як сирий dataclass має сенс лише одразу
після `build_matrix()`.

Групування — за сторінкою, не за джерелом (виправлено живо, task-91 §3.6,
за прямим фідбеком оператора на першому чорновику: "10 URL = 30 таблиць",
не 4 гігантські таблиці по 10 рядків кожна). Для кожної сторінки — заголовок
із URL і зведеними вердиктами, а під ним три маленькі таблиці одна за одною,
кожна з тим самим набором шести колонок (URL/Title/Description/H1/Keywords/
Outgoing links):

    https://example.com/page             URL:✓  Title:✗  Description:✗  …
      Strategy   [заголовок 6 колонок] [1 рядок даних]
      Framework  [той самий заголовок] [1 рядок даних]
      Site       [той самий заголовок] [1 рядок даних]

    https://example.com/next-page        URL:✓  Title:✓  …
      Strategy   …
      Framework  …
      Site       …

Той самий порядок, що на екрані (`strategy-package-tool.js:renderMatrixPageGroup`)
— оператор звіряє три таблиці однієї сторінки одним поглядом, не гортаючи
через усі інші сторінки. Плюс окремий аркуш `Mismatches` — лише рядки з
хоч одним `mismatch`-полем, бо саме їх реально шукає оператор після
великого прогону.
"""

from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.schemas.strategy_package import (
    MatrixCellDTO,
    MatrixFieldVerdictDTO,
    MatrixRowDTO,
    MatrixRowVerdictsDTO,
    MatrixSourceCellsDTO,
)

_HEADER_BG = "434343"
_HEADER_FG = "FFFFFF"
_BLOCK_TITLE_BG = "D9D9D9"
_VERDICT_MATCH = "D9EAD3"
_VERDICT_MISMATCH = "F4CCCC"
_VERDICT_NA = "EFEFEF"
_ABSENT_FG = "999999"
# Live feedback ("Файл дуже важко читабельний"): mismatched fields need
# the SAME bold-red flag Comparison tab cells get on screen
# (`.sp-matrix-cell--mismatch`), not just the header's ✓/✗ summary line —
# otherwise the operator has to cross-reference the summary against six
# plain-looking columns by hand. Green mirrors it for sources that AGREE
# on a field that still has a mismatch somewhere (task-91 §3.6, live
# feedback): Strategy+Framework agreeing while only Site drifts should
# read green/green/red, not red/red/red.
_MISMATCH_FG = "CC0000"
_MISMATCH_BG = "FDEAEA"
_MISMATCH_BORDER = Border(left=Side(style="medium", color=_MISMATCH_FG))
_MATCH_FG = "1E7E34"
_MATCH_BG = "EAF7ED"
_MATCH_BORDER = Border(left=Side(style="medium", color=_MATCH_FG))

_FIELDS = ("url", "title", "description", "h1", "keywords", "links")
_FIELD_LABELS = ("URL", "Title", "Description", "H1", "Keywords", "Outgoing links")

# Live feedback: one packed "Outgoing links" cell (semicolon-joined, most
# of it invisible behind Excel's own row clipping) is unreadable — split
# into its own sub-column per link instead. Scalar fields keep one column
# each. Link columns beyond `_MAX_LINK_COLUMNS` collapse into a "(+N more)"
# note on the last one, same cap `_cell_text` already used for the single-
# cell display.
_SCALAR_FIELDS = ("url", "title", "description", "h1", "keywords")
_SCALAR_LABELS = ("URL", "Title", "Description", "H1", "Keywords")
_MAX_LINK_COLUMNS = 6
_LINK_COLUMN_LABELS = tuple(f"Link {i + 1}" for i in range(_MAX_LINK_COLUMNS))
_ALL_LABELS = _SCALAR_LABELS + _LINK_COLUMN_LABELS
_TOTAL_COLUMNS = len(_ALL_LABELS)
# Character widths tuned to each field's typical content (task-91 §3.5,
# corrected live twice: the original list had 7 entries for what was then
# a 6-column table — one column short of the dropped "Page" lead column,
# silently shifting every column — and columns/rows were later widened
# again live for readability after the "Outgoing links" split).
_COLUMN_WIDTHS = (32, 30, 44, 30, 34) + (30,) * _MAX_LINK_COLUMNS
_DATA_ROW_HEIGHT = 70


def _fill(hex_color: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_color)


_LENGTH_FIELDS = ("title", "description")


def _cell_text(cell: MatrixCellDTO, field: str | None = None) -> str:
    if cell.state == "absent":
        return "— (no such column)"
    if cell.state == "empty":
        return ""
    if cell.text is not None:
        if field in _LENGTH_FIELDS:
            return f"{cell.text}\n({len(cell.text)} chars)"
        return cell.text
    shown = "; ".join(cell.items[:8])
    if len(cell.items) > 8:
        shown += f" (+{len(cell.items) - 8} more)"
    return shown


def _header_row(ws: Worksheet, row_index: int, labels: list[str]) -> None:
    for col_index, label in enumerate(labels, start=1):
        cell = ws.cell(row_index, col_index, label)
        cell.font = Font(name="Calibri", bold=True, color=_HEADER_FG, size=11)
        cell.fill = _fill(_HEADER_BG)
        cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)


_VERDICT_SYMBOL = {"match": "✓", "mismatch": "✗", "not_comparable": "—"}
_VERDICT_FILL = {
    "match": _VERDICT_MATCH, "mismatch": _VERDICT_MISMATCH, "not_comparable": _VERDICT_NA,
}
_MINI_TABLES: tuple[tuple[str, str], ...] = (
    ("strategy", "Strategy"), ("framework", "Framework"), ("site", "Site"),
)


def _cell_tone(verdict: MatrixFieldVerdictDTO, side: str) -> str | None:
    """Mirrors `strategy-package-tool.js:matrixCellTone` — `outlier_sources`
    (task-91 §3.6) says which source(s) actually drifted; keywords/links
    verdicts don't compute per-source outliers (different claim-vs-confirm
    shape), so a mismatch there still flags every source, same as before."""
    if verdict.state == "match":
        return "match"
    if verdict.state == "mismatch":
        if verdict.outlier_sources:
            return "mismatch" if side in verdict.outlier_sources else "match"
        return "mismatch"
    return None


def _page_header(ws: Worksheet, row_index: int, row: MatrixRowDTO) -> int:
    """URL/label + зведені вердикти шести полів в один рядок над трьома
    міні-таблицями цієї сторінки — та сама інформація, що бейджі в шапці
    групи на екрані (`renderMatrixVerdictBadges`)."""
    title_cell = ws.cell(row_index, 1, row.label)
    title_cell.font = Font(name="Calibri", bold=True, size=13)
    summary = "   ".join(
        f"{label}:{_VERDICT_SYMBOL[getattr(row.verdicts, field).state]}"
        for field, label in zip(_FIELDS, _FIELD_LABELS, strict=True)
    )
    summary_cell = ws.cell(row_index, 2, summary)
    summary_cell.font = Font(italic=True, size=10, color=_ABSENT_FG)
    for col in range(1, _TOTAL_COLUMNS + 1):
        ws.cell(row_index, col).fill = _fill(_BLOCK_TITLE_BG)
    return row_index + 1


def _tone_font_fill_border(tone: str | None) -> tuple[Font, PatternFill | None, Border | None]:
    if tone == "mismatch":
        return Font(bold=True, color=_MISMATCH_FG), _fill(_MISMATCH_BG), _MISMATCH_BORDER
    if tone == "match":
        return Font(bold=True, color=_MATCH_FG), _fill(_MATCH_BG), _MATCH_BORDER
    return Font(), None, None


def _mini_table(
    ws: Worksheet, start_row: int, side: str, title: str,
    cells: MatrixSourceCellsDTO | None, verdicts: MatrixRowVerdictsDTO,
) -> int:
    """Одна маленька таблиця — назва джерела, заголовок колонок (5
    скалярних полів + до `_MAX_LINK_COLUMNS` окремих підколонок-посилань),
    один рядок даних. `cells=None` -> сторінка не мала цього джерела в
    цьому прогоні (наприклад фреймворк без відповідного рядка стратегії).
    `side` ("strategy"/"framework"/"site") вирішує, чи ЦЕ джерело — той
    самий "outlier", що відрізняється (червоний), чи один із тих, що
    погоджуються між собою (зелений) — `_cell_tone()`. Кожна підколонка
    посилання успадковує тон усього поля `links` — самих посилань
    попарно не звіряємо, лише факт "поле розходиться десь"."""

    def tone_for(field: str) -> str | None:
        if cells is None or getattr(cells, field).state != "value":
            return None
        return _cell_tone(getattr(verdicts, field), side)

    r = start_row
    label_cell = ws.cell(r, 1, title)
    label_cell.font = Font(bold=True, size=10, color=_ABSENT_FG)
    r += 1

    links_tone = tone_for("links")
    for col_index, label in enumerate(_ALL_LABELS, start=1):
        is_link_col = col_index > len(_SCALAR_FIELDS)
        tone = links_tone if is_link_col else tone_for(_SCALAR_FIELDS[col_index - 1])
        header_fg = _HEADER_FG
        if tone == "mismatch":
            header_fg = _MISMATCH_FG
        elif tone == "match":
            header_fg = _MATCH_FG
        header_cell = ws.cell(r, col_index, label)
        header_cell.font = Font(name="Calibri", bold=True, size=11, color=header_fg)
        header_cell.fill = _fill(_HEADER_BG)
        header_cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    r += 1

    if cells is None:
        note = ws.cell(r, 1, "— not in this run")
        note.font = Font(color=_ABSENT_FG, italic=True)
    else:
        for col_offset, field in enumerate(_SCALAR_FIELDS):
            cell_value = getattr(cells, field)
            cell = ws.cell(r, 1 + col_offset, _cell_text(cell_value, field))
            cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
            tone = tone_for(field)
            font, fill, border = _tone_font_fill_border(tone)
            if tone:
                cell.font, cell.fill, cell.border = font, fill, border
            elif cell_value.state == "absent":
                cell.font = Font(color=_ABSENT_FG, italic=True)

        links_cell = cells.links
        link_items = links_cell.items if links_cell.state == "value" else ()
        base_col = len(_SCALAR_FIELDS) + 1
        for i in range(_MAX_LINK_COLUMNS):
            col = base_col + i
            if i < len(link_items):
                text = link_items[i]
                if i == _MAX_LINK_COLUMNS - 1 and len(link_items) > _MAX_LINK_COLUMNS:
                    text += f"  (+{len(link_items) - _MAX_LINK_COLUMNS} more)"
                cell = ws.cell(r, col, text)
                font, fill, border = _tone_font_fill_border(links_tone)
                if links_tone:
                    cell.font, cell.fill, cell.border = font, fill, border
            elif i == 0 and links_cell.state == "absent":
                cell = ws.cell(r, col, "— (no such column)")
                cell.font = Font(color=_ABSENT_FG, italic=True)
            else:
                cell = ws.cell(r, col, "")
            cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
    ws.row_dimensions[r].height = _DATA_ROW_HEIGHT
    return r + 1


def _column_widths(ws: Worksheet) -> None:
    for col_index, width in enumerate(_COLUMN_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(col_index)].width = width


def build_comparison_sheet(ws: Worksheet, rows: list[MatrixRowDTO]) -> None:
    r = 1
    for row in rows:
        r = _page_header(ws, r, row)
        for side, title in _MINI_TABLES:
            r = _mini_table(ws, r, side, title, getattr(row, side), row.verdicts)
        r += 1  # spacer between page groups
    _column_widths(ws)
    ws.freeze_panes = "A1"
    ws.sheet_view.showGridLines = False


def build_mismatches_sheet(ws: Worksheet, rows: list[MatrixRowDTO]) -> None:
    """Лише рядки з хоч одним `mismatch`-полем (task-91 §3.5) — те, що
    оператор реально шукає після прогону на десятках сторінок."""
    mismatched = [
        row for row in rows
        if any(getattr(row.verdicts, f).state == "mismatch" for f in _FIELDS)
    ]
    labels = ["Page", "Field", "Detail"]
    _header_row(ws, 1, labels)
    r = 2
    for row in mismatched:
        for field, label in zip(_FIELDS, _FIELD_LABELS, strict=True):
            verdict = getattr(row.verdicts, field)
            if verdict.state != "mismatch":
                continue
            ws.cell(r, 1, row.label)
            ws.cell(r, 2, label)
            ws.cell(r, 3, verdict.detail or "").alignment = Alignment(wrap_text=True)
            r += 1
    for col_index, width in ((1, 40), (2, 16), (3, 90)):
        ws.column_dimensions[get_column_letter(col_index)].width = width
    ws.freeze_panes = "A2"
    if not mismatched:
        ws.cell(2, 1, "No mismatches in this run.").font = Font(italic=True, color=_ABSENT_FG)


def build_matrix_xlsx(rows: list[MatrixRowDTO]) -> bytes:
    workbook = Workbook()
    comparison_ws = workbook.active
    comparison_ws.title = "Comparison"
    build_comparison_sheet(comparison_ws, rows)

    mismatches_ws = workbook.create_sheet("Mismatches")
    build_mismatches_sheet(mismatches_ws, rows)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


__all__ = ["build_matrix_xlsx"]
