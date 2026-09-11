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
import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.schemas.strategy_package import (
    CopyRowDTO,
    CopyTabResultDTO,
    FrameworkOutcomeDTO,
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
_MATCH_FG = "1E7E34"
_MATCH_BG = "EAF7ED"
# Live feedback ("Покращ стиль та читабельність"): with `showGridLines =
# False` and no border on untoned cells, plain fields (URL, absent cells,
# not-toned H1...) had no visible boundary at all — only the colored left
# edge on a toned cell hinted where one column ended and the next began.
# A thin gray grid on every cell (data AND header) makes the sheet read as
# an actual table again; toned cells keep their colored left edge, now
# layered on top of the same thin gray on the other three sides instead of
# replacing the border outright.
_GRID_COLOR = "D9D9D9"
_GRID_SIDE = Side(style="thin", color=_GRID_COLOR)
_GRID_BORDER = Border(top=_GRID_SIDE, bottom=_GRID_SIDE, left=_GRID_SIDE, right=_GRID_SIDE)
_MISMATCH_BORDER = Border(
    top=_GRID_SIDE, bottom=_GRID_SIDE, right=_GRID_SIDE,
    left=Side(style="medium", color=_MISMATCH_FG),
)
_MATCH_BORDER = Border(
    top=_GRID_SIDE, bottom=_GRID_SIDE, right=_GRID_SIDE,
    left=Side(style="medium", color=_MATCH_FG),
)
# A thicker divider above each page's title row separates one page's three
# mini-tables from the next page's — otherwise the last (toned, colored)
# data row of one page sits directly against the next page's header with
# nothing but a one-row gap to signal "new group starts here" (live
# feedback screenshot: the previous page's red-bordered row read as if it
# bled into the next page's header).
_PAGE_DIVIDER_SIDE = Side(style="medium", color=_HEADER_BG)
_SOURCE_LABEL_BG = "F3F3F3"
_TITLE_ROW_HEIGHT = 22
_SUMMARY_ROW_HEIGHT = 16

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
        cell.border = _GRID_BORDER
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
    """URL/label + зведені вердикти шести полів над трьома міні-таблицями
    цієї сторінки — та сама інформація, що бейджі в шапці групи на екрані
    (`renderMatrixVerdictBadges`). Title-рядок злитий на всю ширину, не
    одна колонка поруч з summary (live feedback: сусідня клітинка з
    summary-текстом обрізала довгий URL замість перенесення — "Юрл не
    обрізай"). Summary-рядок — НЕ злитий: кожне поле у своїй клітинці,
    бо одна клітинка має рівно один шрифт на весь текст — не можна
    пофарбувати ✓ зеленим і ✗ червоним в одному спільному рядку
    (live feedback: "галочки зелені. хрестики червоні"). Плюс потовщена
    верхня межа-роздільник між сторінками."""
    title_row = row_index
    ws.merge_cells(
        start_row=title_row, start_column=1, end_row=title_row, end_column=_TOTAL_COLUMNS
    )
    title_cell = ws.cell(title_row, 1, row.label)
    title_cell.font = Font(name="Calibri", bold=True, size=13)
    if row.label.startswith(("http://", "https://")):
        # Same as the per-page Copy sheets: a URL in the header is a link,
        # not just text (live feedback round on the Excel export).
        _link_cell(title_cell, row.label, bold=True, size=13)
    title_cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    for col in range(1, _TOTAL_COLUMNS + 1):
        cell = ws.cell(title_row, col)
        cell.fill = _fill(_BLOCK_TITLE_BG)
        cell.border = Border(top=_PAGE_DIVIDER_SIDE)
    ws.row_dimensions[title_row].height = _TITLE_ROW_HEIGHT

    # One cell per field (task-91 §6, live feedback: "галочки зелені.
    # хрестики червоні") instead of one joined string — a single cell can
    # only carry one font, so a match's ✓ and a mismatch's ✗ in the same
    # string were forced to share one (muted gray) color. `_FIELDS` has
    # exactly 6 entries, aligned one-to-one with the sheet's first 6
    # columns (5 scalar fields + the "Outgoing links" column), so each
    # badge lands directly above the column it summarizes.
    summary_row = title_row + 1
    summary_fg = {"match": _MATCH_FG, "mismatch": _MISMATCH_FG, "not_comparable": _ABSENT_FG}
    for col_index, (field, label) in enumerate(zip(_FIELDS, _FIELD_LABELS, strict=True), start=1):
        state = getattr(row.verdicts, field).state
        badge_cell = ws.cell(summary_row, col_index, f"{label}: {_VERDICT_SYMBOL[state]}")
        badge_cell.font = Font(bold=state != "not_comparable", size=10, color=summary_fg[state])
        badge_cell.alignment = Alignment(horizontal="left", vertical="center")
    for col in range(1, _TOTAL_COLUMNS + 1):
        ws.cell(summary_row, col).fill = _fill(_BLOCK_TITLE_BG)
    ws.row_dimensions[summary_row].height = _SUMMARY_ROW_HEIGHT
    next_row = summary_row + 1

    # Live feedback («додати посилання на фрейм» у першому аркуші): third
    # header line pointing at the framework's Google Doc — same
    # `Framework: <name>` -> link line the per-page Copy sheets carry, so
    # the two exports read the same. `strategy_only` rows have no
    # framework and get no line (not an empty one).
    if row.framework_document_id:
        ws.merge_cells(
            start_row=next_row, start_column=1, end_row=next_row, end_column=_TOTAL_COLUMNS
        )
        doc_cell = ws.cell(
            next_row, 1,
            f"Framework: {row.framework_document_name or row.framework_document_id}",
        )
        _link_cell(doc_cell, _gdoc_link(row.framework_document_id), size=10)
        doc_cell.alignment = Alignment(horizontal="left", vertical="center")
        for col in range(1, _TOTAL_COLUMNS + 1):
            ws.cell(next_row, col).fill = _fill(_BLOCK_TITLE_BG)
        ws.row_dimensions[next_row].height = _SUMMARY_ROW_HEIGHT
        next_row += 1
    return next_row


def _tone_font_fill_border(tone: str | None) -> tuple[Font, PatternFill | None, Border]:
    if tone == "mismatch":
        return Font(bold=True, color=_MISMATCH_FG), _fill(_MISMATCH_BG), _MISMATCH_BORDER
    if tone == "match":
        return Font(bold=True, color=_MATCH_FG), _fill(_MATCH_BG), _MATCH_BORDER
    return Font(), None, _GRID_BORDER


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
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=_TOTAL_COLUMNS)
    label_cell = ws.cell(r, 1, title)
    label_cell.font = Font(bold=True, size=10, color=_ABSENT_FG)
    label_cell.alignment = Alignment(horizontal="left", vertical="center")
    for col in range(1, _TOTAL_COLUMNS + 1):
        ws.cell(r, col).fill = _fill(_SOURCE_LABEL_BG)
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
        header_cell.border = _GRID_BORDER
        header_cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    r += 1

    if cells is None:
        note = ws.cell(r, 1, "— not in this run")
        note.font = Font(color=_ABSENT_FG, italic=True)
        note.border = _GRID_BORDER
    else:
        for col_offset, field in enumerate(_SCALAR_FIELDS):
            cell_value = getattr(cells, field)
            cell = ws.cell(r, 1 + col_offset, _cell_text(cell_value, field))
            cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
            tone = tone_for(field)
            font, fill, border = _tone_font_fill_border(tone)
            cell.border = border
            if tone:
                cell.font, cell.fill = font, fill
            elif cell_value.state == "absent":
                cell.font = Font(color=_ABSENT_FG, italic=True)

        links_cell = cells.links
        link_items = links_cell.items if links_cell.state == "value" else ()
        base_col = len(_SCALAR_FIELDS) + 1
        link_font, link_fill, link_border = _tone_font_fill_border(links_tone)
        for i in range(_MAX_LINK_COLUMNS):
            col = base_col + i
            if i < len(link_items):
                text = link_items[i]
                if i == _MAX_LINK_COLUMNS - 1 and len(link_items) > _MAX_LINK_COLUMNS:
                    text += f"  (+{len(link_items) - _MAX_LINK_COLUMNS} more)"
                cell = ws.cell(r, col, text)
                if links_tone:
                    cell.font, cell.fill = link_font, link_fill
            elif i == 0 and links_cell.state == "absent":
                cell = ws.cell(r, col, "— (no such column)")
                cell.font = Font(color=_ABSENT_FG, italic=True)
            else:
                cell = ws.cell(r, col, "")
            cell.border = link_border
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


# ── аркуші Copy (task-92 §2.6, переписано за live feedback) ─────────────────
# Перший чорновик — один плаский аркуш на ВСІ сторінки з колонками
# Page/Tab/Section/Doc/Site/Verdict/Detail: заміряно на EFFYS — 436 рядків
# на 4 сторінки, з них 294 (67%) `match` і 79 (18%) `extra` — 6 із 7 рядків
# шум для того, чия робота — виправити копію. Плюс фіксована висота 46pt
# із переносом обрізала довгі речення знизу. Тепер: аркуш `Copy` — індекс
# (рядок на сторінку/вкладку: URL, документ, к-сть проблем, лінк на
# аркуш), далі ОДИН аркуш НА СТОРІНКУ лише з тим, що треба виправити
# (`missing` + `changed`), без колонок Site sentence / Verdict — послівний
# diff у колонці Issue (медіана 29 символів на 70 живих `changed`-рядках)
# достатній, щоб зрозуміти, що саме не так, без другого речення поруч.
# Висота рядка НЕ задається — Excel сам підганяє під перенесений текст.

_GDOC_URL = "https://docs.google.com/document/d/{document_id}/edit"
_LINK_FG = "1155CC"
_ACTIONABLE_STATES = frozenset({"missing", "changed"})
# Excel: назва аркуша ≤ 31 символ, без `[ ] : * ? / \`, унікальна без
# урахування регістру. Останній сегмент URL (`columbus-ave`,
# `ouija-board-by-laxton`) коротший і впізнаваніший за назву документа
# (51+ символів); на 203 живих URL — 193 унікальні імені, 8 колізій
# (`faq`, `menu`, `home`…), звідси дедуп-суфікс ` (2)`.
_EXCEL_SHEET_NAME_MAX = 31
_EXCEL_SHEET_FORBIDDEN_RE = re.compile(r"[\[\]:*?/\\]")

_COPY_INDEX_LABELS = ("Page", "Framework document", "Copy tab", "Issues", "Sheet")
_COPY_INDEX_WIDTHS = (56, 46, 26, 34, 30)
_COPY_PAGE_LABELS = ("#", "Section", "Sentence (from Copy)", "Issue")
_COPY_PAGE_WIDTHS = (5, 30, 100, 44)
# `missing` — речення взагалі нема на сторінці (червоний, як mismatch у
# Comparison); `changed` — є, але з іншим текстом (бурштиновий). Колір
# ніколи не єдиний сигнал (AGENTS.md §7) — колонка Issue каже те саме
# словами.
_COPY_ISSUE_FG = {"missing": _MISMATCH_FG, "changed": "BF9000"}
_COPY_ISSUE_BG = {"missing": _MISMATCH_BG, "changed": "FFF6D9"}
_COPY_STATE_NOTE = {
    "no_url": "no URL to compare",
    "not_checked": "site not checked",
    "not_applicable": "n/a",
}


def _gdoc_link(document_id: str) -> str:
    return _GDOC_URL.format(document_id=document_id)


def _sheet_title_for(
    page_url: str | None, document_name: str, *, suffix: str, used: set[str]
) -> str:
    """URL's last path segment (fallback: document name), made Excel-safe
    and unique against `used` (lower-cased — Excel treats `Menu`/`menu`
    as the same sheet)."""
    base = ""
    if page_url:
        path = urlsplit(page_url).path.strip("/")
        base = path.rsplit("/", 1)[-1] if path else "home"
    if not base:
        base = document_name
    base = _EXCEL_SHEET_FORBIDDEN_RE.sub("-", base).strip() or "page"
    if suffix:
        base = f"{base} {suffix}"
    candidate = base[:_EXCEL_SHEET_NAME_MAX].rstrip()
    n = 2
    while candidate.lower() in used:
        tail = f" ({n})"
        candidate = base[: _EXCEL_SHEET_NAME_MAX - len(tail)].rstrip() + tail
        n += 1
    used.add(candidate.lower())
    return candidate


def _copy_issue(row: CopyRowDTO) -> str:
    if row.state == "missing":
        return "Not on the page"
    if row.state == "changed":
        return row.diff_summary or "Differs from the page"
    return row.state


def _link_cell(cell: Any, url: str, *, bold: bool = False, size: int = 11) -> None:
    cell.hyperlink = url
    cell.font = Font(name="Calibri", bold=bold, size=size, color=_LINK_FG, underline="single")


def _merged_line(ws: Worksheet, row_index: int, text: str, width: int) -> Any:
    ws.merge_cells(start_row=row_index, start_column=1, end_row=row_index, end_column=width)
    cell = ws.cell(row_index, 1, text)
    cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=False)
    return cell


def _copy_page_sheet(
    ws: Worksheet, *, page_url: str | None, document_name: str, document_id: str,
    tab: CopyTabResultDTO, rows: Sequence[CopyRowDTO],
) -> None:
    """Один аркуш = одна сторінка: шапка (URL → лінк, документ → лінк на
    Google Doc, вкладка + лічильник), далі лише те, що треба виправити,
    одне речення на рядок. Висота рядка не задається — Excel/LibreOffice/
    Sheets автопідганяють під перенесений текст; фіксовані 46pt у першому
    чорновику обрізали довгі речення (live feedback: «1 речення 1 рядок»)."""
    width = len(_COPY_PAGE_LABELS)
    r = 1
    url_cell = _merged_line(ws, r, page_url or "(no page URL in this run)", width)
    if page_url:
        _link_cell(url_cell, page_url, bold=True, size=13)
    else:
        url_cell.font = Font(bold=True, size=13, italic=True, color=_ABSENT_FG)
    ws.row_dimensions[r].height = _TITLE_ROW_HEIGHT
    r += 1
    doc_cell = _merged_line(ws, r, f"Framework: {document_name}", width)
    _link_cell(doc_cell, _gdoc_link(document_id))
    r += 1
    n = len(rows)
    counter = f"{n} issue{'' if n == 1 else 's'} to fix"
    note = _merged_line(ws, r, f"Copy tab: {tab.tab_title} · {counter}", width)
    note.font = Font(size=10, color=_ABSENT_FG)
    r += 2  # порожній рядок між шапкою і таблицею

    _header_row(ws, r, list(_COPY_PAGE_LABELS))
    header_row = r
    r += 1
    for i, row in enumerate(rows, start=1):
        values = (i, row.section or "", row.doc_text or "", _copy_issue(row))
        fg, bg = _COPY_ISSUE_FG.get(row.state), _COPY_ISSUE_BG.get(row.state)
        for col, value in enumerate(values, start=1):
            cell = ws.cell(r, col, value)
            cell.border = _GRID_BORDER
            cell.alignment = Alignment(
                horizontal="left", vertical="top", wrap_text=col in (2, 3, 4)
            )
            if fg and col == 4:
                cell.font = Font(bold=True, color=fg)
            if bg:
                cell.fill = _fill(bg)
        r += 1

    for col_index, w in enumerate(_COPY_PAGE_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(col_index)].width = w
    ws.freeze_panes = f"A{header_row + 1}"
    ws.sheet_view.showGridLines = False


def build_copy_sheets(workbook: Workbook, frameworks: Sequence[FrameworkOutcomeDTO]) -> None:
    """Індексний аркуш `Copy` + один аркуш на кожну сторінку, де є що
    виправити. Сторінки без проблем і вкладки зі станом `no_url`/
    `not_checked`/`not_applicable` — теж рядок в індексі (перевірено-чисто
    ≠ не перевірено, PLAN §12), але без окремого аркуша."""
    index_ws = workbook.create_sheet("Copy")
    used = {name.lower() for name in workbook.sheetnames}
    index_rows: list[tuple[Any, ...]] = []
    page_sheets: list[tuple[str, FrameworkOutcomeDTO, CopyTabResultDTO, list[CopyRowDTO]]] = []

    for outcome in frameworks:
        if not outcome.copy_diffs:
            continue
        document_name = outcome.source.document_name or outcome.source.document_id
        actionable = {
            id(tab): [row for row in tab.rows if row.state in _ACTIONABLE_STATES]
            for tab in outcome.copy_diffs
        }
        with_sheet = [
            tab for tab in outcome.copy_diffs if tab.state == "checked" and actionable[id(tab)]
        ]
        for tab in outcome.copy_diffs:
            rows = actionable[id(tab)]
            sheet_title: str | None = None
            if tab in with_sheet:
                # Language suffix only when this page gets more than one
                # sheet (a genuinely bilingual, both-published site — none
                # in the portfolio, but the general case costs nothing).
                suffix = ""
                if len(with_sheet) > 1:
                    suffix = f"({tab.language_hint or tab.role})"
                sheet_title = _sheet_title_for(
                    outcome.page_url, document_name, suffix=suffix, used=used
                )
                page_sheets.append((sheet_title, outcome, tab, rows))
            if tab.state == "checked":
                issues: Any = len(rows) if rows else "0 — clean"
            else:
                reason = f" — {tab.reason}" if tab.reason else ""
                issues = f"{_COPY_STATE_NOTE.get(tab.state, tab.state)}{reason}"
            tab_label = tab.tab_title if tab.role == "primary" else f"{tab.tab_title} (secondary)"
            index_rows.append((outcome, document_name, tab_label, issues, sheet_title))

    _header_row(index_ws, 1, list(_COPY_INDEX_LABELS))
    r = 2
    for outcome, document_name, tab_label, issues, sheet_title in index_rows:
        page_cell = index_ws.cell(r, 1, outcome.page_url or document_name)
        if outcome.page_url:
            _link_cell(page_cell, outcome.page_url)
        doc_cell = index_ws.cell(r, 2, document_name)
        _link_cell(doc_cell, _gdoc_link(outcome.source.document_id))
        index_ws.cell(r, 3, tab_label)
        issues_cell = index_ws.cell(r, 4, issues)
        if isinstance(issues, int):
            issues_cell.font = Font(bold=True, color=_MISMATCH_FG)
        elif str(issues).startswith("0"):
            issues_cell.font = Font(color=_MATCH_FG)
        else:
            issues_cell.font = Font(italic=True, color=_ABSENT_FG)
        sheet_cell = index_ws.cell(r, 5, sheet_title or "—")
        if sheet_title:
            # Internal link: Excel quotes the sheet name, a literal `'`
            # inside it is doubled.
            _link_cell(sheet_cell, f"#'{sheet_title.replace(chr(39), chr(39) * 2)}'!A1")
        else:
            sheet_cell.font = Font(color=_ABSENT_FG)
        for col in range(1, len(_COPY_INDEX_LABELS) + 1):
            cell = index_ws.cell(r, col)
            cell.border = _GRID_BORDER
            cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
        r += 1
    for col_index, w in enumerate(_COPY_INDEX_WIDTHS, start=1):
        index_ws.column_dimensions[get_column_letter(col_index)].width = w
    index_ws.freeze_panes = "A2"
    index_ws.sheet_view.showGridLines = False
    if not index_rows:
        index_ws.cell(2, 1, "No copy data in this run.").font = Font(italic=True, color=_ABSENT_FG)

    for sheet_title, outcome, tab, rows in page_sheets:
        ws = workbook.create_sheet(sheet_title)
        _copy_page_sheet(
            ws, page_url=outcome.page_url,
            document_name=outcome.source.document_name or outcome.source.document_id,
            document_id=outcome.source.document_id, tab=tab, rows=rows,
        )


def build_matrix_xlsx(
    rows: list[MatrixRowDTO], *, frameworks: Sequence[FrameworkOutcomeDTO] = ()
) -> bytes:
    workbook = Workbook()
    comparison_ws = workbook.active
    comparison_ws.title = "Comparison"
    build_comparison_sheet(comparison_ws, rows)

    mismatches_ws = workbook.create_sheet("Mismatches")
    build_mismatches_sheet(mismatches_ws, rows)

    build_copy_sheets(workbook, frameworks)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


__all__ = ["build_matrix_xlsx"]
