"""Нативний Google Sheet (SEO-стратегія) -> ContentDoc + список посилань на
фреймворки (task-90 §2.2/§1.8).

`read_strategy_gsheet` — тонкий адаптер над `readers/tabular.py`, той самий
принцип, що `readers/xlsx.py`: перетворити джерело-специфічну сітку у
`list[tuple[object, ...]]` на аркуш і віддати спільному ядру. Тут джерело —
`SheetsClient.get_grid()`, а не openpyxl worksheet; кожна `SheetCell`
зводиться до `formatted_value` (або `None` для порожньої клітинки) — це
рівно те, що бачив би оператор у Google Sheets UI, і рівно той самий текст,
що `_score_row`/`_row_to_item` очікують.

`find_framework_links` — окрема функція, бо посилання на фреймворк несе
метадані (звідки взяте, яка форма, mimeType), яких немає в голому тексті
клітинки і які `ContentItem`/`tabular.py` навмисно не моделюють (task-90
§2.2 docstring). Основна форма — смарт-чип (`SheetCell.form == "chip"`), не
гіперпосилання: заміряно живо 148 chip-посилань проти 61 hyperlink на 25
живих стратегіях (task-90 §1.8) — перший скан цього проєкту без урахування
`chipRuns` пропустив 148 із 209 реальних посилань. `mime_type` чипа
(`application/vnd.google-apps.kix` = Google Doc) фільтрує кандидатів без
жодного звернення до Drive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.contentqa.model import ContentDoc
from app.contentqa.readers.tabular import (
    WorkbookInspection,
    build_content_doc,
    build_inspection,
    check_requested_sheets,
    scan_sheets,
)
from app.integrations.google_api.sheets_client import CellLink, SheetGrid

_GOOGLE_DOC_MIME = "application/vnd.google-apps.kix"
_DOC_ID_RE = re.compile(r"docs\.google\.com/document/d/([A-Za-z0-9_-]+)")


def _grid_to_raw_sheets(grid: tuple[SheetGrid, ...]) -> list[tuple[str, list[tuple[object, ...]]]]:
    return [
        (
            sheet.title,
            [tuple(cell.formatted_value for cell in row) for row in sheet.rows],
        )
        for sheet in sorted(grid, key=lambda s: s.index)
    ]


def read_strategy_gsheet(
    grid: tuple[SheetGrid, ...], *, source: str, sheets: list[str] | None = None
) -> ContentDoc:
    """Ті самі семантика й гарантії, що `read_strategy_xlsx` (task-87 §1.3):
    `sheets=None` перевіряє все несуче, `sheets=[...]` обмежує items, решта
    аркушів усе одно потрапляє в `sheet_states` зі станом `not_selected`."""
    raw_sheets = _grid_to_raw_sheets(grid)
    scans = scan_sheets(raw_sheets)
    return build_content_doc(scans, source=source, doc_type="strategy_gsheet", sheets=sheets)


def inspect_strategy_gsheet(grid: tuple[SheetGrid, ...]) -> WorkbookInspection:
    scans = scan_sheets(_grid_to_raw_sheets(grid))
    check_requested_sheets(scans, None)
    return build_inspection(scans)


@dataclass(frozen=True)
class FrameworkLinkCandidate:
    """Один знайдений кандидат-фреймворк: звідки взятий (для тексту
    походження в UI, task-90 §5 крок 2 "linked from …!row N") і на що
    вказує. `document_id` — вже витягнутий id, готовий для
    `DocsClient.get_document`."""

    sheet: str
    row: int  # 1-based, як `ContentItem.ref` ("!row 8")
    col: int  # 0-based індекс колонки
    form: str  # "chip" | "hyperlink" | "text_run" | "formula"
    document_id: str
    display_text: str | None  # formattedValue клітинки — назва документа, якщо є


def find_framework_links(grid: tuple[SheetGrid, ...]) -> tuple[FrameworkLinkCandidate, ...]:
    """Кожне посилання на Google Doc, знайдене в будь-якій клітинці —
    незалежно від того, чи впізнана колонка як `framework_link` за
    `_ALIASES` (task-90 §1.8: індекс цієї колонки скаче навіть у межах
    одного воркбука — 12/14/15 на трьох аркушах SHB), тому пошук тут не
    прив'язаний до назви колонки. Дублікати (той самий документ у кількох
    клітинках) НЕ згортаються — це робота викликача (`contentqa/package.py`),
    бо саме там відомо, який кандидат обраний оператором."""
    found: list[FrameworkLinkCandidate] = []
    for sheet in grid:
        for row_idx, row in enumerate(sheet.rows, start=1):
            for col_idx, cell in enumerate(row):
                for link in cell.links:
                    doc_id = _extract_doc_id(link)
                    if doc_id:
                        found.append(FrameworkLinkCandidate(
                            sheet=sheet.title, row=row_idx, col=col_idx,
                            form=link.form, document_id=doc_id,
                            display_text=cell.formatted_value,
                        ))
    return tuple(found)


def _extract_doc_id(link: CellLink) -> str | None:
    if link.mime_type is not None and link.mime_type != _GOOGLE_DOC_MIME:
        return None  # chip on a non-Doc target (Sheet, Slide, PDF) — not a framework
    match = _DOC_ID_RE.search(link.uri)
    return match.group(1) if match else None
