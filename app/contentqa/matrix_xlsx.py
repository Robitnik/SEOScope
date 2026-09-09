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

Малюнок користувача — чотири блоки одне під одним, кожен зі своїми шістьма
колонками (URL/Title/Description/H1/Keywords/Outgoing links), кожен рядок
блоку — одна сторінка матриці, у тому самому порядку в усіх чотирьох
блоках, щоб рядок N будь-якого блоку відповідав тій самій сторінці:

    Стратегія            [заголовок 6 колонок] [N рядків даних]
    Фреймворк             [той самий заголовок] [N рядків даних]
    Сайт                 [той самий заголовок] [N рядків даних]
    Вердикт (галочка)     [той самий заголовок] [N рядків: ✓/✗/—]

Плюс окремий аркуш `Mismatches` — лише рядки з хоч одним `mismatch`-полем,
бо саме їх реально шукає оператор після великого прогону.
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
_LEAD_COLUMNS = ["Page"]


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


def _block_title(ws: Worksheet, row_index: int, title: str, width: int) -> None:
    cell = ws.cell(row_index, 1, title)
    cell.font = Font(name="Calibri", bold=True, size=13)
    cell.fill = _fill(_BLOCK_TITLE_BG)
    for col in range(2, width + 1):
        ws.cell(row_index, col).fill = _fill(_BLOCK_TITLE_BG)


def _header_row(ws: Worksheet, row_index: int, labels: list[str]) -> None:
    for col_index, label in enumerate(labels, start=1):
        cell = ws.cell(row_index, col_index, label)
        cell.font = Font(name="Calibri", bold=True, color=_HEADER_FG, size=11)
        cell.fill = _fill(_HEADER_BG)
        cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)


def _source_block(
    ws: Worksheet, start_row: int, title: str, rows: list[MatrixRowDTO], side: str
) -> int:
    """Один блок (Strategy/Framework/Site) — заголовок блоку, заголовок
    колонок, N рядків даних. `side` — "strategy"/"framework"/"site",
    обирає, яке `MatrixSourceCellsDTO` показати (`None` -> рядки-заглушки з
    "— (no such column)" для сторінок, де цього джерела взагалі нема)."""
    labels = _LEAD_COLUMNS + list(_FIELD_LABELS)
    _block_title(ws, start_row, title, len(labels))
    _header_row(ws, start_row + 1, labels)
    for offset, row in enumerate(rows):
        r = start_row + 2 + offset
        source: MatrixSourceCellsDTO | None = getattr(row, side)
        ws.cell(r, 1, row.label)
        if source is None:
            for col_index in range(2, len(labels) + 1):
                cell = ws.cell(r, col_index, "— (not in this run)")
                cell.font = Font(color=_ABSENT_FG, italic=True)
            continue
        for col_offset, field in enumerate(_FIELDS):
            cell_value = getattr(source, field)
            cell = ws.cell(r, 2 + col_offset, _cell_text(cell_value))
            if cell_value.state == "absent":
                cell.font = Font(color=_ABSENT_FG, italic=True)
            cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=False)
    return start_row + 2 + len(rows)


_VERDICT_SYMBOL = {"match": "✓", "mismatch": "✗", "not_comparable": "—"}
_VERDICT_FILL = {
    "match": _VERDICT_MATCH, "mismatch": _VERDICT_MISMATCH, "not_comparable": _VERDICT_NA,
}


def _verdict_block(ws: Worksheet, start_row: int, rows: list[MatrixRowDTO]) -> int:
    labels = _LEAD_COLUMNS + list(_FIELD_LABELS)
    _block_title(ws, start_row, "Verdict", len(labels))
    _header_row(ws, start_row + 1, labels)
    for offset, row in enumerate(rows):
        r = start_row + 2 + offset
        ws.cell(r, 1, row.label)
        for col_offset, field in enumerate(_FIELDS):
            verdict = getattr(row.verdicts, field)
            cell = ws.cell(r, 2 + col_offset, _VERDICT_SYMBOL[verdict.state])
            cell.fill = _fill(_VERDICT_FILL[verdict.state])
            cell.alignment = Alignment(horizontal="center", vertical="center")
    return start_row + 2 + len(rows)


def _column_widths(ws: Worksheet) -> None:
    widths = [30, 34, 28, 40, 20, 24, 40]
    for col_index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col_index)].width = width


def build_comparison_sheet(ws: Worksheet, rows: list[MatrixRowDTO]) -> None:
    r = 1
    r = _source_block(ws, r, "Strategy", rows, "strategy")
    r += 1
    r = _source_block(ws, r, "Framework", rows, "framework")
    r += 1
    r = _source_block(ws, r, "Site", rows, "site")
    r += 1
    _verdict_block(ws, r, rows)
    _column_widths(ws)
    ws.freeze_panes = "B1"


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
