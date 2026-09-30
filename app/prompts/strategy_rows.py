"""Розбір аркушів стратегії для Prompt Launcher (task-102 §3.1–3.2).

Свідомо НЕ `readers/tabular.py::ContentItem`: той мапить `URL` і `New URL` в
один тег «останній перемагає» (для `NEW PAGE` у `URL` стоїть `doesn't exist`,
цільова адреса лише в `New URL`), зливає Primary/Secondary/Longtail у безіменний
кортеж і не знає `Page Status`, `Breadcrumbs`, `Comments`, `Commercial
destination`. Тут — вузький розбір рівно під шапку `PageFramework.md` /
`BlogFramework.md`. Спільним лишається лише `sheet_role` (роль аркуша за назвою).

Порожнє в клітинці — це `None`, ніколи не `""`, і ніколи не `0`; літерал
`-`/`—`/`n/a` (заміряна пастка `0674487`) теж порожнє.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from app.contentqa.model import SheetRole
from app.contentqa.readers.tabular import sheet_role
from app.prompts.strategy_source import Cell, RawRow, RawSheet

_HEADER_MIN_SCORE = 3

_ALIASES: dict[str, str] = {
    "page status": "status",
    "page name": "page_name",
    "page": "page_name",
    "pages": "page_name",
    "url": "url",
    "page url": "url",
    "new url": "new_url",
    "primary key": "primary",
    "primary keyword": "primary",
    "secondary key": "secondary",
    "longtail key": "longtail",
    "h1": "h1",
    "title": "title",
    "meta title": "title",
    "description": "description",
    "meta description": "description",
    "breadcrumbs": "breadcrumbs",
    "comment": "comments",
    "comments": "comments",
    "framework / copy link": "framework",
    "framework link": "framework",
    "framework": "framework",
    "frameworks": "framework",
    "copy link": "framework",
    "article topic": "topic",
    "article short description": "blurb",
    "keyword": "keyword",
    "keywords": "keyword",
    "commercial destination": "commercial",
    "anchor text": "anchors",
    "anchors": "anchors",
    "internal link url": "internal_links",
    "internal links": "internal_links",
}

# `readers/tabular.py::_NO_VALUE` + типографські тире, які стратеги ставлять у
# порожні клітинки (`—` у колонці Framework на GOREPORT).
_PLACEHOLDERS = {"no", "none", "n/a", "-", "—", "–"}
_DASH_ONLY = {"-", "—", "–"}
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HTTP_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)

_STATUS_EXISTING = "optimize page"
_STATUS_NEW = "new page"


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower().rstrip(":")


def clean(text: str | None, *, placeholders: bool = False) -> str | None:
    """Чиста клітинка або `None`. `placeholders=True` додатково відкидає
    `no`/`none`/`n/a` (для URL, назв, посилань на фреймворк); тире відкидаються
    завжди — у Title чи Comments одинокий `—` не значення."""
    if text is None:
        return None
    value = _CONTROL_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value:
        return None
    low = value.lower()
    if low in _DASH_ONLY or (placeholders and low in _PLACEHOLDERS):
        return None
    return value


def valid_http_url(text: str | None) -> str | None:
    value = clean(text, placeholders=True)
    return value if value is not None and _HTTP_RE.match(value) else None


@dataclass(frozen=True)
class PageRow:
    ref: str
    page_name: str | None
    status: str | None
    url: str | None  # ЖИВА адреса; не-URL типу `doesn't exist` -> None
    new_url: str | None
    primary: str | None
    secondary: str | None
    longtail: str | None
    h1: str | None
    title: str | None
    description: str | None
    breadcrumbs: str | None
    comments: str | None
    framework_text: str | None
    framework_link: str | None

    @property
    def mode(self) -> Literal["existing", "new"]:
        """Статус виграє, коли він є; без нього — за фактом наявності живого URL."""
        status = _norm(self.status)
        if status == _STATUS_NEW:
            return "new"
        if status == _STATUS_EXISTING:
            return "existing"
        return "existing" if self.url else "new"


@dataclass(frozen=True)
class ArticleRow:
    ref: str
    topic: str
    blurb: str | None
    keywords: tuple[str, ...]
    commercial: str | None
    title: str | None
    description: str | None
    anchors: tuple[str, ...]
    internal_links: tuple[str, ...]
    dropped_link_pairs: int
    comments: str | None
    framework_text: str | None
    framework_link: str | None


@dataclass
class ParsedSheet:
    name: str
    role: SheetRole
    kind: Literal["page", "blog"] | None  # `None` — заголовка рядків не знайдено
    pages: list[PageRow] = field(default_factory=list)
    articles: list[ArticleRow] = field(default_factory=list)
    orphan_rows: int = 0

    @property
    def row_count(self) -> int:
        return len(self.pages) if self.kind == "page" else len(self.articles)


# ── Заголовки ─────────────────────────────────────────────────────────────────


def _header_map(row: RawRow) -> dict[int, str]:
    mapped: dict[int, str] = {}
    for idx, cell in enumerate(row):
        tag = _ALIASES.get(_norm(cell.text))
        # Перший стовпець із таким тегом виграє: `URL` перед `New URL` — різні
        # теги, а от дубль `Description` далі в аркуші не має перебивати перший.
        if tag and tag not in mapped.values():
            mapped[idx] = tag
    return mapped


def _is_header(mapped: dict[int, str]) -> bool:
    tags = set(mapped.values())
    return len(mapped) >= _HEADER_MIN_SCORE and bool(tags & {"page_name", "topic"})


def _cell(row: RawRow, mapped: dict[int, str], tag: str) -> Cell | None:
    for idx, t in mapped.items():
        if t == tag:
            return row[idx] if idx < len(row) else None
    return None


def _text(
    row: RawRow, mapped: dict[int, str], tag: str, *, placeholders: bool = False
) -> str | None:
    cell = _cell(row, mapped, tag)
    return clean(cell.text, placeholders=placeholders) if cell is not None else None


def _is_blank(row: RawRow) -> bool:
    return all(clean(c.text) is None for c in row)


# ── Аркуш ─────────────────────────────────────────────────────────────────────


def parse_sheet(sheet: RawSheet) -> ParsedSheet:
    name, rows = sheet
    headers = [(i, m) for i, r in enumerate(rows) if _is_header(m := _header_map(r))]
    if not headers:
        return ParsedSheet(name=name, role=sheet_role(name), kind=None)

    kind: Literal["page", "blog"] = "blog" if "topic" in headers[0][1].values() else "page"
    role = sheet_role(name)
    if role == "other":
        role = kind
    parsed = ParsedSheet(name=name, role=role, kind=kind)

    for b, (header_idx, mapped) in enumerate(headers):
        end = headers[b + 1][0] if b + 1 < len(headers) else len(rows)
        block = rows[header_idx + 1 : end]
        block_kind = "blog" if "topic" in mapped.values() else "page"
        if block_kind == "page":
            for offset, row in enumerate(block):
                item = _page_row(name, header_idx + 1 + offset + 1, row, mapped)
                if item is not None:
                    parsed.pages.append(item)
        else:
            _collect_articles(parsed, name, header_idx + 1, block, mapped)
    return parsed


def _framework(row: RawRow, mapped: dict[int, str]) -> tuple[str | None, str | None]:
    cell = _cell(row, mapped, "framework")
    if cell is None:
        return None, None
    text = clean(cell.text, placeholders=True)
    link = cell.links[0] if cell.links else None
    # Чіп без тексту не буває, але посилання без тексту — лишається фактом
    # «фреймворк є»: беремо саме посилання як підпис.
    if text is None and link is not None:
        text = link
    return text, link


def _page_row(sheet: str, row_no: int, row: RawRow, mapped: dict[int, str]) -> PageRow | None:
    page_name = _text(row, mapped, "page_name", placeholders=True)
    url_raw = _text(row, mapped, "url", placeholders=True)
    new_url_raw = _text(row, mapped, "new_url", placeholders=True)
    if page_name is None and url_raw is None and new_url_raw is None:
        return None  # порожній рядок або рядок легенди кольорів
    fw_text, fw_link = _framework(row, mapped)
    return PageRow(
        ref=f"{sheet}!row {row_no}",
        page_name=page_name,
        status=_text(row, mapped, "status"),
        url=valid_http_url(url_raw),
        new_url=valid_http_url(new_url_raw),
        primary=_text(row, mapped, "primary"),
        secondary=_text(row, mapped, "secondary"),
        longtail=_text(row, mapped, "longtail"),
        h1=_text(row, mapped, "h1"),
        title=_text(row, mapped, "title"),
        description=_text(row, mapped, "description"),
        breadcrumbs=_text(row, mapped, "breadcrumbs"),
        comments=_text(row, mapped, "comments"),
        framework_text=fw_text,
        framework_link=fw_link,
    )


def _lines(cell: Cell | None) -> list[str]:
    if cell is None:
        return []
    lines = [clean(part, placeholders=True) for part in (cell.text or "").split("\n")]
    return [line for line in lines if line is not None]


def _pair_lines(
    anchors_cell: Cell | None, links_cell: Cell | None
) -> tuple[list[tuple[str, str]], int]:
    """Пари анкор↔URL ОДНОГО рядка: усередині рядка — позиційно (GOREPORT: усі
    анкори однією клітинкою через `\\n`), між рядками — незалежно (GRP: по парі
    в кожному рядку). Неповна пара не йде в лаунчер, але рахується."""
    anchors = _lines(anchors_cell)
    links = _lines(links_cell)
    if not links and links_cell is not None and links_cell.links:
        links = list(links_cell.links)
    paired = min(len(anchors), len(links))
    pairs = list(zip(anchors[:paired], links[:paired], strict=True))
    return pairs, abs(len(anchors) - len(links))


def _collect_articles(
    parsed: ParsedSheet, sheet: str, first_idx: int, block: list[RawRow], mapped: dict[int, str]
) -> None:
    current: dict[str, object] | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        parsed.articles.append(_article_from(current))
        current = None

    for offset, row in enumerate(block):
        row_no = first_idx + offset + 1
        topic = _text(row, mapped, "topic", placeholders=True)
        if topic is not None:
            flush()
            current = {
                "ref": f"{sheet}!row {row_no}",
                "topic": topic,
                "rows": [row],
                "mapped": mapped,
            }
            continue
        if _is_blank(row):
            continue
        if current is None:
            parsed.orphan_rows += 1
            continue
        rows = current["rows"]
        assert isinstance(rows, list)
        rows.append(row)
    flush()


def _first(
    rows: list[RawRow], mapped: dict[int, str], tag: str, *, placeholders: bool = False
) -> str | None:
    for row in rows:
        value = _text(row, mapped, tag, placeholders=placeholders)
        if value is not None:
            return value
    return None


def _article_from(state: dict[str, object]) -> ArticleRow:
    rows = state["rows"]
    mapped = state["mapped"]
    assert isinstance(rows, list) and isinstance(mapped, dict)

    keywords: list[str] = []
    seen: set[str] = set()
    anchors: list[str] = []
    links: list[str] = []
    dropped = 0
    comments: list[str] = []
    fw_text: str | None = None
    fw_link: str | None = None
    for row in rows:
        kw = _text(row, mapped, "keyword", placeholders=True)
        if kw is not None and kw.lower() not in seen:
            seen.add(kw.lower())
            keywords.append(kw)
        pairs, lost = _pair_lines(
            _cell(row, mapped, "anchors"), _cell(row, mapped, "internal_links")
        )
        anchors.extend(a for a, _ in pairs)
        links.extend(u for _, u in pairs)
        dropped += lost
        comment = _text(row, mapped, "comments")
        if comment is not None:
            comments.append(comment)
        if fw_text is None:
            fw_text, fw_link = _framework(row, mapped)

    ref = state["ref"]
    topic = state["topic"]
    assert isinstance(ref, str) and isinstance(topic, str)
    return ArticleRow(
        ref=ref,
        topic=topic,
        blurb=_first(rows, mapped, "blurb"),
        keywords=tuple(keywords),
        commercial=_first(rows, mapped, "commercial", placeholders=True),
        title=_first(rows, mapped, "title"),
        description=_first(rows, mapped, "description"),
        anchors=tuple(anchors),
        internal_links=tuple(links),
        dropped_link_pairs=dropped,
        comments="\n\n".join(comments) if comments else None,
        framework_text=fw_text,
        framework_link=fw_link,
    )
