"""Табличне ядро, спільне для XLSX-ридера (task-53/87) і Google Sheets-ридера
(task-90 §2.2). Розбір мапінгу колонок, блоків-заголовків і побудова
`ContentItem` не залежать від того, чи рядки прийшли з openpyxl, чи з
`SheetsClient.get_grid()` — обидва зводяться до `list[tuple[object, ...]]`
(значення клітинок зліва направо) на аркуш.

Виокремлено з `readers/xlsx.py` (task-53 §2.3/§4.1, task-87 §1.3) дослівно —
жодна логіка тут не змінилась, лише вхід узагальнено з "openpyxl worksheet"
на "будь-яка послідовність рядків клітинок". Гейт рефактору:
`tests/unit/contentqa/test_readers_xlsx.py` лишається зеленим без жодної
правки (task-90 §4.2) — якщо тест довелось змінити, це ознака регресії, а не
прогресу.

Мапінг колонок — за нормалізованою назвою заголовка, не хардкодом: живі
воркбуки різняться (`GRP_PAGE_STRATEGY_*.xlsx`, `SAPHNY_SEO_STRATEGY_*`,
`SHB_SEO_STRATEGY_*`). Заголовок шукається по **всьому** аркушу, не лише
перших рядках (task-53 §2.3: дві незалежні таблиці різної форми одна під
одною на живому файлі зламали б наївний пошук у перших 5 рядках).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.contentqa.model import ContentDoc, ContentItem, SheetInfo

_HEADER_MIN_SCORE = 2

# {нормалізований заголовок: тег поля}
_ALIASES: dict[str, str] = {
    "title": "title", "meta title": "title", "title tag": "title", "meta page title": "title",
    "description": "description", "meta description": "description",
    "h1": "h1",
    "url": "url", "page url": "url",
    "page": "page_name", "pages": "page_name", "page name": "page_name",
    "article topic": "page_name",
    "intent": "intent",
    "primary key": "primary_key", "primary keyword": "primary_key",
    "primary page keyword": "primary_key",
    "secondary key": "secondary_key",
    "longtail key": "longtail_key",
    "keywords": "keywords_csv",
    "internal link": "internal_links", "internal links": "internal_links",
    "internal link + anchor text": "internal_links", "anchors": "internal_links",
    "external link": "external_links", "external links": "external_links",
    "external link + anchor text": "external_links",
    # task-90 §1.8: колонка з посиланням на фреймворк — заміряні заголовки
    # `SHB_SEO_STRATEGY_082626` ("Framework / Copy Link"), `SDS`/`SAPHNY`
    # ("Framework"/"Framework Link"). Значення тут — лише текст клітинки
    # (назва документа); фактичний URI/chip живе в `SheetCell.links` і
    # витягується окремо `readers/gsheet.py:find_framework_links()`, бо
    # тут, у tabular-ядрі, клітинка — це вже гола `str(cell)`, без метаданих
    # посилання.
    "framework / copy link": "framework_link", "framework link": "framework_link",
    "framework doc": "framework_link", "framework": "framework_link",
    "copy link": "framework_link",
}
_LOAD_BEARING = {"title", "description"}


class UnknownSheetError(ValueError):
    """Оператор попросив аркуш, якого немає у файлі — 400, не тиха
    перевірка «всього файлу» (task-87 §1.3, крок 7 плану)."""


def _norm(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower().rstrip(":")


def _split_links(text: str) -> tuple[str, ...]:
    pieces: list[str] = []
    for line in str(text).split("\n"):
        pieces.extend(p.strip() for p in line.split("|"))
    return tuple(p for p in pieces if p)


def _score_row(row: tuple[object, ...]) -> dict[int, str]:
    col_map: dict[int, str] = {}
    for col_idx, cell in enumerate(row):
        tag = _ALIASES.get(_norm(cell))
        if tag:
            col_map[col_idx] = tag
    return col_map


def _iter_header_blocks(all_rows: list[tuple[object, ...]]) -> list[tuple[int, dict[int, str]]]:
    """[(header_row_idx, col_map), ...] для кожного рядка з >=2 впізнаними
    колонками, у порядку появи — незалежно від load-bearing (title/description):
    це і є межа блоку, придатний він для перевірки чи ні вирішує викликач."""
    blocks: list[tuple[int, dict[int, str]]] = []
    for row_idx, row in enumerate(all_rows):
        col_map = _score_row(row)
        if len(col_map) >= _HEADER_MIN_SCORE:
            blocks.append((row_idx, col_map))
    return blocks


def _row_to_item(
    ref: str, row: tuple[object, ...], col_map: dict[int, str], *, sheet: str
) -> ContentItem | None:
    values: dict[str, str] = {}
    keywords: list[str] = []
    for col_idx, tag in col_map.items():
        if col_idx >= len(row):
            continue
        cell = row[col_idx]
        if cell is None:
            continue
        text = str(cell).strip()
        if tag == "keywords_csv":
            keywords.extend(k.strip() for k in text.split(",") if k.strip())
        elif tag in {"primary_key", "secondary_key", "longtail_key"}:
            # col_map зберігає порядок зліва направо (побудований у _score_row),
            # а в живих воркбуках ці три колонки завжди йдуть Primary -> Secondary ->
            # Longtail зліва направо, тож просте append тримає primary першим.
            if text:
                keywords.append(text)
        else:
            values[tag] = text

    if not values and not keywords:
        return None

    return ContentItem(
        ref=ref,
        page_name=values.get("page_name"),
        url=values.get("url"),
        intent=values.get("intent"),
        title=values.get("title"),
        description=values.get("description"),
        h1=values.get("h1"),
        keywords=tuple(keywords),
        internal_links=_split_links(values["internal_links"]) if "internal_links" in values else (),
        external_links=_split_links(values["external_links"]) if "external_links" in values else (),
        raw=values,
        sheet=sheet,
    )


def _non_empty_row_count(all_rows: list[tuple[object, ...]], start: int, end: int) -> int:
    return sum(1 for i in range(start, end) if not all(cell is None for cell in all_rows[i]))


@dataclass(frozen=True)
class SheetScan:
    """Один аркуш, повністю просканований (блоки заголовків + межі) —
    спільний проміжний результат для `build_content_doc` (будує items лише
    для обраних аркушів) і `build_inspection` (нічого не будує, лише
    прев'ю). Джерело рядків (openpyxl worksheet / `SheetGrid`) байдуже —
    вхід уже зведений до `list[tuple[object, ...]]`."""

    name: str
    index: int
    all_rows: list[tuple[object, ...]]
    blocks: list[tuple[int, dict[int, str]]]
    load_bearing: list[tuple[int, dict[int, str]]]

    @property
    def is_load_bearing(self) -> bool:
        return bool(self.load_bearing)

    def row_count(self) -> int:
        """Рядків-кандидатів під усіма несучими блоками цього аркуша —
        показується і для обраних (у Scope як 'checked'), і для необраних
        аркушів ('154 rows, not selected'), тому рахується завжди, не лише
        коли аркуш дійсно перевіряється."""
        total = 0
        for b, (header_row_idx, col_map) in enumerate(self.blocks):
            if not (_LOAD_BEARING & set(col_map.values())):
                continue
            data_end = self.blocks[b + 1][0] if b + 1 < len(self.blocks) else len(self.all_rows)
            total += _non_empty_row_count(self.all_rows, header_row_idx + 1, data_end)
        return total

    def columns(self) -> list[str]:
        """Об'єднання розпізнаних тегів по всіх несучих блоках аркуша, у
        порядку першої появи — для прев'ю /inspect."""
        seen: list[str] = []
        for _, col_map in self.load_bearing:
            for tag in col_map.values():
                if tag not in seen:
                    seen.append(tag)
        return seen

    def build_items(self) -> list[ContentItem]:
        items: list[ContentItem] = []
        for b, (header_row_idx, col_map) in enumerate(self.blocks):
            if not (_LOAD_BEARING & set(col_map.values())):
                continue
            data_end = self.blocks[b + 1][0] if b + 1 < len(self.blocks) else len(self.all_rows)
            for i in range(header_row_idx + 1, data_end):
                row = self.all_rows[i]
                if all(cell is None for cell in row):
                    continue
                ref = f"{self.name}!row {i + 1}"
                item = _row_to_item(ref, row, col_map, sheet=self.name)
                if item is not None:
                    items.append(item)
        return items


def scan_sheets(raw_sheets: list[tuple[str, list[tuple[object, ...]]]]) -> list[SheetScan]:
    """`[(sheet_name, all_rows), ...]` -> повністю просканований список
    аркушів. Джерело `all_rows` — openpyxl (`readers/xlsx.py`) чи Sheets API
    grid (`readers/gsheet.py`), обидва байдужі звідси."""
    scans: list[SheetScan] = []
    for index, (name, all_rows) in enumerate(raw_sheets):
        blocks = _iter_header_blocks(all_rows)
        load_bearing = [(idx, cm) for idx, cm in blocks if _LOAD_BEARING & set(cm.values())]
        scans.append(SheetScan(
            name=name, index=index, all_rows=all_rows,
            blocks=blocks, load_bearing=load_bearing,
        ))
    return scans


def check_requested_sheets(scans: list[SheetScan], sheets: list[str] | None) -> None:
    if sheets is None:
        return
    known = {s.name for s in scans}
    unknown = [s for s in sheets if s not in known]
    if unknown:
        raise UnknownSheetError(f"Sheet(s) not found in this workbook: {', '.join(unknown)}")


def build_content_doc(
    scans: list[SheetScan], *, source: str, doc_type: str, sheets: list[str] | None = None
) -> ContentDoc:
    """`sheets=None` (дефолт) перевіряє всі несучі аркуші. `sheets=[...]`
    обмежує items лише цими аркушами; решта аркушів workbook усе одно
    потрапляє в `ContentDoc.sheet_states` зі станом `not_selected`, а не
    зникає мовчки (task-87 §1.3)."""
    check_requested_sheets(scans, sheets)
    selected = set(sheets) if sheets is not None else {s.name for s in scans}

    items: list[ContentItem] = []
    skipped: list[str] = []
    sheet_states: list[SheetInfo] = []

    for scan in scans:
        if scan.name not in selected:
            sheet_states.append(SheetInfo(scan.name, "not_selected", scan.row_count()))
            continue
        if not scan.is_load_bearing:
            skipped.append(scan.name)
            sheet_states.append(SheetInfo(scan.name, "no_recognized_columns", 0))
            continue
        sheet_items = scan.build_items()
        items.extend(sheet_items)
        sheet_states.append(SheetInfo(scan.name, "checked", len(sheet_items)))

    return ContentDoc(
        source=source, doc_type=doc_type,
        items=tuple(items), skipped_sheets=tuple(skipped),
        sheet_states=tuple(sheet_states),
    )


@dataclass(frozen=True)
class SheetPreview:
    name: str
    index: int
    load_bearing: bool
    columns: tuple[str, ...]
    row_count: int
    blocks: int


@dataclass(frozen=True)
class WorkbookInspection:
    sheets: tuple[SheetPreview, ...]
    suggested_sheets: tuple[str, ...]  # усі несучі — дефолтний вибір форми


def build_inspection(scans: list[SheetScan]) -> WorkbookInspection:
    """Крок 1 вибору аркуша (task-87 §1.3): прев'ю без побудови items і без
    жодного побічного ефекту."""
    previews = tuple(
        SheetPreview(
            name=scan.name, index=scan.index, load_bearing=scan.is_load_bearing,
            columns=tuple(scan.columns()), row_count=scan.row_count(),
            blocks=len(scan.blocks),
        )
        for scan in scans
    )
    suggested = tuple(p.name for p in previews if p.load_bearing)
    return WorkbookInspection(sheets=previews, suggested_sheets=suggested)
