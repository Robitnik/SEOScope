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
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.schemas.strategy_package import MatrixCellDTO, MatrixRowDTO, MatrixSourceCellsDTO

_HEADER_BG = "434343"
_HEADER_FG = "FFFFFF"
_BLOCK_TITLE_BG = "D9D9D9"
_VERDICT_MATCH = "D9EAD3"
_VERDICT_MISMATCH = "F4CCCC"
_VERDICT_NA = "EFEFEF"
_ABSENT_FG = "999999"

_FIELDS = ("url", "title", "description", "h1", "keywords", "links")
_FIELD_LABELS = ("URL", "Title", "Description", "H1", "Keywords", "Outgoing links")


def _fill(hex_color: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_color)


def _cell_text(cell: MatrixCellDTO) -> str:
    if cell.state == "absent":
        return "— (no such column)"
    if cell.state == "empty":
        return ""
    if cell.text is not None:
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
    for col in range(1, len(_FIELD_LABELS) + 1):
        ws.cell(row_index, col).fill = _fill(_BLOCK_TITLE_BG)
    return row_index + 1


def _mini_table(
    ws: Worksheet, start_row: int, title: str, cells: MatrixSourceCellsDTO | None
) -> int:
    """Одна маленька таблиця — назва джерела, заголовок шести колонок,
    один рядок даних. `cells=None` -> сторінка не мала цього джерела в
    цьому прогоні (наприклад фреймворк без відповідного рядка стратегії)."""
    r = start_row
    label_cell = ws.cell(r, 1, title)
    label_cell.font = Font(bold=True, size=10, color=_ABSENT_FG)
    r += 1
    _header_row(ws, r, list(_FIELD_LABELS))
    r += 1
    if cells is None:
        note = ws.cell(r, 1, "— not in this run")
        note.font = Font(color=_ABSENT_FG, italic=True)
    else:
        for col_offset, field in enumerate(_FIELDS):
            cell_value = getattr(cells, field)
            cell = ws.cell(r, 1 + col_offset, _cell_text(cell_value))
            if cell_value.state == "absent":
                cell.font = Font(color=_ABSENT_FG, italic=True)
            cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=False)
    return r + 1


def _column_widths(ws: Worksheet) -> None:
    widths = [30, 34, 28, 40, 20, 24, 40]
    for col_index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col_index)].width = width


def build_comparison_sheet(ws: Worksheet, rows: list[MatrixRowDTO]) -> None:
    r = 1
    for row in rows:
        r = _page_header(ws, r, row)
        for side, title in _MINI_TABLES:
            r = _mini_table(ws, r, title, getattr(row, side))
        r += 1  # spacer between page groups
    _column_widths(ws)
    ws.freeze_panes = "A1"


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
