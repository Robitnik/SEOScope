"""SEO-стратегія (ручний Google-Sheets воркбук) -> ContentDoc.

Мапінг колонок — за нормалізованою назвою заголовка (§2.3 плану task-53), не
хардкодом: живі воркбуки різняться (`GRP_PAGE_STRATEGY_*.xlsx`,
`19CLV_PAGE_STRATEGY_*.xlsx`, `GOREPORT_SEO_STRATEGY_*.xlsx`).

Заголовок шукається по **всьому** аркушу, не лише перших рядках: живий файл
(`GOREPORT_SEO_STRATEGY_07272026.xlsx`, аркуш "Structure Features
Description") містить дві незалежні таблиці різної форми одна під одною —
"Top Performing Pages" (Page/URL/Keyword/Traffic/Title/Description/H1) і
нижче, після банера-розділювача, "URL Structure Review & Update"
(PageStatus/Intent/Pages/URL/RecommendedURL/Breadcrumbs/WordCount/Comments,
без title/description). Пошук заголовка лише в перших 5 рядках зафіксував би
мапінг першої таблиці назавжди — тоді колонка URL другої таблиці читалась би
як title, а Word count як description (саме так і сталось при першому
прогоні на живих даних: 120 "meta-title-length" замість очікуваних ~8, бо
довгі URL і числа підмінили собою копірайт). Кожен рядок із двома і більше
впізнаними заголовками починає новий блок — навіть якщо в ньому немає
title/description: такий блок просто не дає жодного item, а не поглинається
попереднім.

Аркуш, де жоден блок не має ні title, ні description, -> `skipped_sheets`, а
не тиха «чистота» (PLAN §12: не перевірили ≠ порушень немає).
"""

from __future__ import annotations

import io
import re

from openpyxl import load_workbook

from app.contentqa.model import ContentDoc, ContentItem

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
}
_LOAD_BEARING = {"title", "description"}


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


def _row_to_item(ref: str, row: tuple[object, ...], col_map: dict[int, str]) -> ContentItem | None:
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
    )


def read_strategy_xlsx(content: bytes, *, source: str) -> ContentDoc:
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    items: list[ContentItem] = []
    skipped: list[str] = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        all_rows = [tuple(row) for row in ws.iter_rows(values_only=True)]
        blocks = _iter_header_blocks(all_rows)
        load_bearing = [(idx, cm) for idx, cm in blocks if _LOAD_BEARING & set(cm.values())]

        if not load_bearing:
            skipped.append(sheet_name)
            continue

        for b, (header_row_idx, col_map) in enumerate(blocks):
            if not (_LOAD_BEARING & set(col_map.values())):
                continue
            data_end = blocks[b + 1][0] if b + 1 < len(blocks) else len(all_rows)
            for i in range(header_row_idx + 1, data_end):
                row = all_rows[i]
                if all(cell is None for cell in row):
                    continue
                ref = f"{sheet_name}!row {i + 1}"
                item = _row_to_item(ref, row, col_map)
                if item is not None:
                    items.append(item)

    return ContentDoc(
        source=source, doc_type="strategy_xlsx",
        items=tuple(items), skipped_sheets=tuple(skipped),
    )
