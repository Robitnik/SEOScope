"""Порівняльна матриця Стратегія/Фреймворк/Сайт/Вердикт (task-91 §2.2/§2.3).

Чиста функція над уже готовими `ContentDoc`/`FrameworkOutcome` — не читає
жодного нового джерела, лише зводить те, що вже витягнули `readers/
gsheet.py`, `readers/gdoc.py` і `site.py` (`FrameworkOutcome.frame_item`/
`.site_page`, task-91 §2.2 доповнення до `package.py`). Шість колонок з
малюнка користувача: URL, Title, Description, H1, Ключі, Вихідні посилання.

**Три стани клітинки** (`CellState`) — той самий інваріант PLAN §12
(`None` ≠ `""`), піднятий на рівень таблиці: заміряно живо (task-91 §1.2) —
11/12 стратегій портфеля мають колонку URL, але одна (SHB) не має її
ВЗАГАЛІ, і показати відсутню колонку як порожню клітинку означало б
збрехати. Для багаторядкової стратегії (на відміну від Framework, де один
документ = один рядок і `None` уже точний — `readers/gdoc.py:_first`) сама
клітинка не може надійно відрізнити "у цьому рядку порожньо" від "колонки
нема в аркуші взагалі" (заміряно: `SDS_SEO_STRATEGY_032026`, 14 рядків, де
`url`-тег відсутній у `item.raw`, хоча колонка `URL` існує й заповнена в
403 інших рядках) — тому присутність колонки для Strategy визначається на
рівні ВСЬОГО документа (`_document_presence`): якщо хоч один рядок має
значення, колонка вважається наявною для всіх рядків цього документа.

**Формат посилань — п'ять живих варіантів, не один** (task-91 §1.4/§1.4a):
`_extract_link_target()` пробує абсолютний URL, шлях у дужках (SDS/SAPHNY),
шлях одразу після `→` (3XE, і власний формат `site.py`) — і чесно здається
(`None`), коли жодного шаблону нема (LAXTON: `→` є, але після нього опис
місця вставки, не шлях; PJC/EFFYS: посилання нема взагалі, лише проза для
копірайтера). Клітинка, для якої не вдалось екстрагувати ціль, лишається
`value` для показу (сирий текст), але не бере участі у вердикті.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urljoin

from app.contentqa.crosscheck import _item_key  # noqa: PLC2701 - see note below
from app.contentqa.model import ContentDoc, ContentItem
from app.contentqa.package import FrameworkOutcome
from app.contentqa.site import LivePage
from app.urlinspector.urlkey import inspector_key

# `FrameworkOutcome.matched_strategy_key` is `_item_key(known_row)`
# (`crosscheck.py`), NOT `item.ref` — live-verified (task-91 3.3), a naive
# `item.ref`-based lookup found ZERO matches on real SHB data because the
# key format is `"url:..."`/`"name:..."`. Importing the private function
# reverses the EXACT mapping the matching engine produced, rather than
# re-deriving a second copy of that logic here that could silently drift
# from crosscheck.py's own matching rules.

CellState = Literal["value", "empty", "absent"]
Verdict = Literal["match", "mismatch", "not_comparable"]
RowKind = Literal["matched", "strategy_only", "framework_only"]

_FIELD_NAMES = ("url", "title", "description", "h1", "keywords", "links")


@dataclass(frozen=True)
class Cell:
    """Одна клітинка матриці. `text` — для скалярних полів (url/title/
    description/h1), `items` — для множинних (keywords/links). Рівно один
    з двох непорожній, коли `state == "value"`."""

    state: CellState
    text: str | None = None
    items: tuple[str, ...] = ()


_ABSENT = Cell(state="absent")
_EMPTY = Cell(state="empty")


@dataclass(frozen=True)
class SourceCells:
    url: Cell
    title: Cell
    description: Cell
    h1: Cell
    keywords: Cell
    links: Cell


@dataclass(frozen=True)
class FieldVerdict:
    state: Verdict
    detail: str | None = None
    # Live feedback (task-91 §3.6): a "mismatch" must say WHICH source is
    # the odd one out, not paint all three sources red — when Strategy and
    # Framework agree and only Site differs, Strategy/Framework should read
    # green (they agree with each other), only Site red. Names of sources
    # ("strategy"/"framework"/"site") whose value is not shared by any
    # other source with a value; `()` for "match"/"not_comparable" states.
    outlier_sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class RowVerdicts:
    url: FieldVerdict
    title: FieldVerdict
    description: FieldVerdict
    h1: FieldVerdict
    keywords: FieldVerdict
    links: FieldVerdict


@dataclass(frozen=True)
class MatrixRow:
    """Один рядок матриці = одна сторінка (task-91 §2.1). `label` — що
    показати як заголовок рядка: канонічний URL, коли відомий хоч
    звідкись, інакше точний рядок стратегії (`Sheet!row N`) чи назва
    Framework-документа. `kind` каже, яких блоків очікувати порожніми:
    `strategy_only` не має `framework`/`site`, `framework_only` не має
    `strategy`."""

    key: str
    kind: RowKind
    label: str
    strategy: SourceCells | None
    framework: SourceCells | None
    site: SourceCells | None
    verdicts: RowVerdicts


# ── нормалізатори (task-91 §1.4-1.6/§2.3) ──────────────────────────────────


def _norm_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def _norm_keyword_set(values: tuple[str, ...]) -> frozenset[str]:
    """Ключі в стратегії часто злиті переносами рядків в один елемент
    tuple (task-91 §1.5, живо заміряно на SHB): `splitlines` розбирає це
    так само, як фреймворк уже розбиває їх на власні елементи — множини
    стають порівнюваними, а не хибно різними через саму структуру tuple."""
    out: set[str] = set()
    for value in values:
        for line in value.splitlines():
            token = line.strip().casefold()
            if token:
                out.add(token)
    return frozenset(out)


_ABS_URL_RE = re.compile(r"https?://\S+")
_PAREN_PATH_RE = re.compile(r"\((/[^\s)]*)\)")
_ARROW_PATH_RE = re.compile(r"→\s*(/\S*)")
_TRAILING_PUNCT = ").,;:'\""


def _extract_link_target(raw: str, *, base_url: str | None) -> str | None:
    """Найкраща спроба видобути URL з довільного рядка посилання (task-91
    §1.4a) — три живо заміряні форми, по черзі, перша знахідка перемагає.
    `None`, коли жодна не спрацювала (не здогадка) — типово для Framework-
    клітин, які є прозою для людини, не машинним посиланням."""
    match = _ABS_URL_RE.search(raw)
    if match:
        return match.group(0).rstrip(_TRAILING_PUNCT)
    match = _PAREN_PATH_RE.search(raw)
    if match:
        path = match.group(1)
        return urljoin(base_url, path) if base_url else path
    match = _ARROW_PATH_RE.search(raw)
    if match:
        path = match.group(1).rstrip(_TRAILING_PUNCT)
        return urljoin(base_url, path) if base_url else path
    return None


def _norm_link_set(values: tuple[str, ...], *, base_url: str | None) -> frozenset[str]:
    keys: set[str] = set()
    for value in values:
        target = _extract_link_target(value, base_url=base_url)
        if target:
            keys.add(inspector_key(target))
    return frozenset(keys)


# ── видимість колонки на рівні документа (Strategy) ────────────────────────


def _document_presence(doc: ContentDoc) -> frozenset[str]:
    present: set[str] = set()
    for item in doc.items:
        if item.url:
            present.add("url")
        if item.title:
            present.add("title")
        if item.description:
            present.add("description")
        if item.h1:
            present.add("h1")
        if item.keywords:
            present.add("keywords")
        if item.internal_links or item.external_links:
            present.add("links")
    return frozenset(present)


# ── клітинки per-source ─────────────────────────────────────────────────


def _strategy_cells(item: ContentItem | None, presence: frozenset[str]) -> SourceCells:
    if item is None:
        return SourceCells(_ABSENT, _ABSENT, _ABSENT, _ABSENT, _ABSENT, _ABSENT)

    def text(field: str, value: str | None) -> Cell:
        if field not in presence:
            return _ABSENT
        return Cell(state="value", text=value) if value else _EMPTY

    def multi(field: str, values: tuple[str, ...]) -> Cell:
        if field not in presence:
            return _ABSENT
        return Cell(state="value", items=values) if values else _EMPTY

    links = item.internal_links + item.external_links
    return SourceCells(
        url=text("url", item.url),
        title=text("title", item.title),
        description=text("description", item.description),
        h1=text("h1", item.h1),
        keywords=multi("keywords", item.keywords),
        links=multi("links", links),
    )


def _framework_cells(item: ContentItem | None) -> SourceCells:
    """Один Framework-документ = один рядок — `readers/gdoc.py:_first`
    уже точно розрізняє `None`/`""` per-field, тож на відміну від
    `_strategy_cells` тут не потрібна евристика рівня документа. Для
    keywords/links (завжди `tuple`, без `None`) присутність колонки
    читається з `item.raw`, який `gdoc.py` заповнює для КОЖНОГО
    розпізнаного лейбла, навіть якщо значення порожнє."""
    if item is None:
        return SourceCells(_ABSENT, _ABSENT, _ABSENT, _ABSENT, _ABSENT, _ABSENT)

    def text(value: str | None) -> Cell:
        if value is None:
            return _ABSENT
        return Cell(state="value", text=value) if value else _EMPTY

    def multi(raw_keys: tuple[str, ...], values: tuple[str, ...]) -> Cell:
        if not any(key in item.raw for key in raw_keys):
            return _ABSENT
        return Cell(state="value", items=values) if values else _EMPTY

    links = item.internal_links + item.external_links
    return SourceCells(
        url=text(item.url),
        title=text(item.title),
        description=text(item.description),
        h1=text(item.h1),
        keywords=multi(("keywords",), item.keywords),
        links=multi(("internal_links", "external_links"), links),
    )


def _site_cells(page: LivePage | None, claimed_keywords: frozenset[str]) -> SourceCells:
    """Сайт не має "колонок" — недоступність сторінки чи відсутність
    сигналу вже точно `None`/`""` (`PageSignals`). Ключі — похідне поле
    (task-91 §1.3): "скільки із заявлених ключів реально знайдено", тому
    їй потрібен `claimed_keywords` ззовні, а не власне значення."""
    if page is None or not page.ok or page.signals is None:
        return SourceCells(_ABSENT, _ABSENT, _ABSENT, _ABSENT, _ABSENT, _ABSENT)

    def text(value: str | None) -> Cell:
        if value is None:
            return _ABSENT
        return Cell(state="value", text=value) if value else _EMPTY

    links = page.internal_links + page.external_links
    links_cell = Cell(state="value", items=links) if links else _EMPTY
    keywords_cell = _site_keyword_coverage(page, claimed_keywords)
    return SourceCells(
        url=Cell(state="value", text=page.url),
        title=text(page.signals.title),
        description=text(page.signals.description),
        h1=text(page.signals.h1),
        keywords=keywords_cell,
        links=links_cell,
    )


def _site_keyword_coverage(page: LivePage, claimed: frozenset[str]) -> Cell:
    if not claimed or page.signals is None:
        return _ABSENT
    haystack = " ".join(
        filter(
            None,
            [
                page.signals.title,
                page.signals.h1,
                page.signals.description,
                page.signals.visible_text,
            ],
        )
    ).casefold()
    items = tuple(f"{'✓' if kw in haystack else '✗'} {kw}" for kw in sorted(claimed))
    return Cell(state="value", items=items)


# ── вердикти (task-91 §2.3) ────────────────────────────────────────────


def _scalar_verdict(cells: dict[str, Cell], *, url: bool = False) -> FieldVerdict:
    known: list[tuple[str, str]] = []
    for src, cell in cells.items():
        if cell.state != "value" or not cell.text:
            continue
        norm = inspector_key(cell.text) if url else _norm_text(cell.text)
        known.append((src, norm))
    if len(known) < 2:
        return FieldVerdict("not_comparable")
    distinct = {v for _, v in known}
    if len(distinct) == 1:
        return FieldVerdict("match", detail=f"{len(known)} sources agree")
    # Live feedback: don't paint every source red — only the one(s) whose
    # value nobody else shares. Strategy+Framework agreeing while only Site
    # drifted is the common real case (§1.7); both should read green, only
    # Site red. A value shared by exactly one source (count == 1) is an
    # outlier; with 2 sources disagreeing 1-vs-1, both are outliers (no
    # majority to side with) — same for 3-way all-different (1-vs-1-vs-1).
    value_counts: dict[str, int] = {}
    for _, v in known:
        value_counts[v] = value_counts.get(v, 0) + 1
    outliers = tuple(src for src, v in known if value_counts[v] == 1)
    return FieldVerdict(
        "mismatch", detail="; ".join(f"{s}: {v}" for s, v in known), outlier_sources=outliers,
    )


def _keyword_verdict(
    strategy_kw: frozenset[str], framework_kw: frozenset[str], site_cell: Cell
) -> FieldVerdict:
    claimed_sources = [s for s in (strategy_kw, framework_kw) if s]
    if len(claimed_sources) >= 2:
        overlap = strategy_kw & framework_kw
        if overlap:
            return FieldVerdict("match", detail=f"{len(overlap)} shared keyword(s)")
        return FieldVerdict("mismatch", detail="strategy and framework keywords do not overlap")
    if claimed_sources and site_cell.state == "value":
        missing = [i for i in site_cell.items if i.startswith("✗")]
        if missing:
            return FieldVerdict(
                "mismatch", detail=f"{len(missing)} keyword(s) not found on the live page"
            )
        return FieldVerdict("match", detail="all claimed keywords found on the live page")
    return FieldVerdict("not_comparable")


def _link_verdict(
    strategy_links: frozenset[str],
    framework_links: frozenset[str],
    site_links: frozenset[str],
    *,
    site_known: bool,
) -> FieldVerdict:
    claimed = strategy_links | framework_links
    if claimed and site_known:
        missing = claimed - site_links
        if missing:
            return FieldVerdict(
                "mismatch", detail=f"{len(missing)} claimed link(s) not found on the live page"
            )
        return FieldVerdict("match", detail="all claimed links found on the live page")
    claimed_sources = [s for s in (strategy_links, framework_links) if s]
    if len(claimed_sources) >= 2:
        overlap = strategy_links & framework_links
        return FieldVerdict("match") if overlap else FieldVerdict("mismatch")
    return FieldVerdict("not_comparable")


def _row_verdicts(
    strategy: SourceCells | None,
    framework: SourceCells | None,
    site: SourceCells | None,
    *,
    strategy_item: ContentItem | None,
    framework_item: ContentItem | None,
    site_page: LivePage | None,
) -> RowVerdicts:
    def cells_for(field: str) -> dict[str, Cell]:
        out: dict[str, Cell] = {}
        if strategy is not None:
            out["strategy"] = getattr(strategy, field)
        if framework is not None:
            out["framework"] = getattr(framework, field)
        if site is not None:
            out["site"] = getattr(site, field)
        return out

    base_url = (
        (framework_item.url if framework_item else None)
        or (strategy_item.url if strategy_item else None)
        or (site_page.url if site_page else None)
    )
    strategy_kw = _norm_keyword_set(strategy_item.keywords) if strategy_item else frozenset()
    framework_kw = _norm_keyword_set(framework_item.keywords) if framework_item else frozenset()
    strategy_links = (
        _norm_link_set(
            strategy_item.internal_links + strategy_item.external_links, base_url=base_url
        )
        if strategy_item
        else frozenset()
    )
    framework_links = (
        _norm_link_set(
            framework_item.internal_links + framework_item.external_links, base_url=base_url
        )
        if framework_item
        else frozenset()
    )
    site_links = (
        _norm_link_set(site_page.internal_links + site_page.external_links, base_url=site_page.url)
        if site_page and site_page.ok
        else frozenset()
    )
    site_known = site_page is not None and site_page.ok

    return RowVerdicts(
        url=_scalar_verdict(cells_for("url"), url=True),
        title=_scalar_verdict(cells_for("title")),
        description=_scalar_verdict(cells_for("description")),
        h1=_scalar_verdict(cells_for("h1")),
        keywords=_keyword_verdict(strategy_kw, framework_kw, site.keywords if site else _ABSENT),
        links=_link_verdict(strategy_links, framework_links, site_links, site_known=site_known),
    )


def _row_label(
    strategy_item: ContentItem | None,
    framework_item: ContentItem | None,
    site_page: LivePage | None,
    *,
    fallback: str,
) -> str:
    url = (
        (framework_item.url if framework_item else None)
        or (strategy_item.url if strategy_item else None)
        or (site_page.url if site_page else None)
    )
    return url or fallback


def build_matrix(strategy: ContentDoc, outcomes: list[FrameworkOutcome]) -> list[MatrixRow]:
    """Зводить стратегію + результати всіх фреймворків у рядки-сторінки
    (task-91 §2.1): зіставлені пари, самотні рядки стратегії без
    фреймворку, самотні фреймворки без рядка стратегії. Чиста функція —
    жодного HTTP/БД, той самий принцип, що `run_package` (task-90 §2.6)."""
    strategy_by_key = {
        key: item for item in strategy.items if (key := _item_key(item)) is not None
    }
    strategy_presence = _document_presence(strategy)
    matched_refs: set[str] = set()
    rows: list[MatrixRow] = []

    for outcome in outcomes:
        if outcome.state != "checked" or outcome.frame_item is None:
            continue
        framework_item = outcome.frame_item
        strategy_item = strategy_by_key.get(outcome.matched_strategy_key or "")
        if strategy_item is not None:
            matched_refs.add(strategy_item.ref)

        strategy_cells = (
            _strategy_cells(strategy_item, strategy_presence) if strategy_item else None
        )
        framework_cells = _framework_cells(framework_item)
        claimed_kw = _norm_keyword_set(framework_item.keywords) | (
            _norm_keyword_set(strategy_item.keywords) if strategy_item else frozenset()
        )
        site_cells = _site_cells(outcome.site_page, claimed_kw)

        rows.append(
            MatrixRow(
                key=strategy_item.ref if strategy_item else f"framework:{framework_item.ref}",
                kind="matched" if strategy_item else "framework_only",
                label=_row_label(
                    strategy_item,
                    framework_item,
                    outcome.site_page,
                    fallback=outcome.source.document_name or outcome.source.document_id,
                ),
                strategy=strategy_cells,
                framework=framework_cells,
                site=site_cells,
                verdicts=_row_verdicts(
                    strategy_cells,
                    framework_cells,
                    site_cells,
                    strategy_item=strategy_item,
                    framework_item=framework_item,
                    site_page=outcome.site_page,
                ),
            )
        )

    for item in strategy.items:
        if item.ref in matched_refs:
            continue
        strategy_cells = _strategy_cells(item, strategy_presence)
        rows.append(
            MatrixRow(
                key=item.ref,
                kind="strategy_only",
                label=_row_label(item, None, None, fallback=item.ref),
                strategy=strategy_cells,
                framework=None,
                site=None,
                verdicts=_row_verdicts(
                    strategy_cells,
                    None,
                    None,
                    strategy_item=item,
                    framework_item=None,
                    site_page=None,
                ),
            )
        )

    return rows


__all__ = [
    "Cell",
    "CellState",
    "FieldVerdict",
    "MatrixRow",
    "RowKind",
    "RowVerdicts",
    "SourceCells",
    "Verdict",
    "build_matrix",
]
