"""Порівняльна матриця Стратегія/Фреймворк/Сайт/Вердикт (task-91 §2.2/§2.3).

Чиста функція над уже готовими `ContentDoc`/`FrameworkOutcome` — не читає
жодного нового джерела, лише зводить те, що вже витягнули `readers/
gsheet.py`, `readers/gdoc.py` і `site.py` (`FrameworkOutcome.frame_item`/
`.site_page`, task-91 §2.2 доповнення до `package.py`). П'ять колонок:
URL, Title, Description, H1, Ключі — плюс окремий блок «Вихідні посилання»
(`MatrixRow.links: LinkAudit`, task-95), що більше не є шостим полем цих
трьох мінітаблиць: до task-95 тут порівнювались приписані посилання з
УСІМА `<a href>` сторінки (94-97% — навігаційний каркас), і копія в цю
звірку не входила взагалі — `contentqa/links.py` замінює це ланцюгом
Framework → Copy → Site.

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
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from app.contentqa.crosscheck import _item_key  # noqa: PLC2701 - see note below
from app.contentqa.model import ContentDoc, ContentItem
from app.contentqa.package import FrameworkOutcome
from app.contentqa.site import LivePage
from app.contentqa.verdict import FieldVerdict, Verdict
from app.urlinspector.urlkey import inspector_key

if TYPE_CHECKING:
    # `links.py` тепер несе ЛАНЦЮГ Framework -> Copy -> Site (task-95) і
    # сам імпортує `FieldVerdict` із `verdict.py` (не звідси) — типовий
    # імпорт `LinkAudit` тут лише для анотації `MatrixRow.links`, під
    # `from __future__ import annotations` рядковий, тож `TYPE_CHECKING`-
    # гілка вистачає й не створює цикл `matrix -> package -> links ->
    # matrix` на рівні виконання.
    from app.contentqa.links import LinkAudit

# `FrameworkOutcome.matched_strategy_key` is `_item_key(known_row)`
# (`crosscheck.py`), NOT `item.ref` — live-verified (task-91 3.3), a naive
# `item.ref`-based lookup found ZERO matches on real SHB data because the
# key format is `"url:..."`/`"name:..."`. Importing the private function
# reverses the EXACT mapping the matching engine produced, rather than
# re-deriving a second copy of that logic here that could silently drift
# from crosscheck.py's own matching rules.

CellState = Literal["value", "empty", "absent"]
RowKind = Literal["matched", "strategy_only", "framework_only"]

_FIELD_NAMES = ("url", "title", "description", "h1", "keywords")


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


@dataclass(frozen=True)
class RowVerdicts:
    url: FieldVerdict
    title: FieldVerdict
    description: FieldVerdict
    h1: FieldVerdict
    keywords: FieldVerdict


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
    # Ланцюг Framework -> Copy -> Site (task-95) — НЕ per-source клітинка
    # (сайт/стратегія не мають "своєї сторони" цієї звірки, §2.5 плану),
    # тому окреме поле рядка, не шосте поле `SourceCells`/`RowVerdicts`.
    # `None` для `strategy_only` (фреймворка нема — нічого звіряти) і для
    # матриць, персистованих до task-95 — не вгадується заднім числом.
    links: LinkAudit | None = None
    # Google Doc за цим рядком (live feedback на Excel: «додати посилання на
    # фрейм» у Comparison). `None` для `strategy_only` (фреймворка нема) і
    # для матриць, персистованих до цього поля — не вгадується заднім
    # числом.
    framework_document_id: str | None = None
    framework_document_name: str | None = None


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
    return frozenset(present)


# ── клітинки per-source ─────────────────────────────────────────────────


def _strategy_cells(item: ContentItem | None, presence: frozenset[str]) -> SourceCells:
    if item is None:
        return SourceCells(_ABSENT, _ABSENT, _ABSENT, _ABSENT, _ABSENT)

    def text(field: str, value: str | None) -> Cell:
        if field not in presence:
            return _ABSENT
        return Cell(state="value", text=value) if value else _EMPTY

    def multi(field: str, values: tuple[str, ...]) -> Cell:
        if field not in presence:
            return _ABSENT
        return Cell(state="value", items=values) if values else _EMPTY

    return SourceCells(
        url=text("url", item.url),
        title=text("title", item.title),
        description=text("description", item.description),
        h1=text("h1", item.h1),
        keywords=multi("keywords", item.keywords),
    )


def _framework_cells(item: ContentItem | None) -> SourceCells:
    """Один Framework-документ = один рядок — `readers/gdoc.py:_first`
    уже точно розрізняє `None`/`""` per-field, тож на відміну від
    `_strategy_cells` тут не потрібна евристика рівня документа. Для
    keywords (завжди `tuple`, без `None`) присутність колонки читається з
    `item.raw`, який `gdoc.py` заповнює для КОЖНОГО розпізнаного лейбла,
    навіть якщо значення порожнє."""
    if item is None:
        return SourceCells(_ABSENT, _ABSENT, _ABSENT, _ABSENT, _ABSENT)

    def text(value: str | None) -> Cell:
        if value is None:
            return _ABSENT
        return Cell(state="value", text=value) if value else _EMPTY

    def multi(raw_keys: tuple[str, ...], values: tuple[str, ...]) -> Cell:
        if not any(key in item.raw for key in raw_keys):
            return _ABSENT
        return Cell(state="value", items=values) if values else _EMPTY

    return SourceCells(
        url=text(item.url),
        title=text(item.title),
        description=text(item.description),
        h1=text(item.h1),
        keywords=multi(("keywords",), item.keywords),
    )


def _site_cells(page: LivePage | None, claimed_keywords: frozenset[str]) -> SourceCells:
    """Сайт не має "колонок" — недоступність сторінки чи відсутність
    сигналу вже точно `None`/`""` (`PageSignals`). Ключі — похідне поле
    (task-91 §1.3): "скільки із заявлених ключів реально знайдено", тому
    їй потрібен `claimed_keywords` ззовні, а не власне значення."""
    if page is None or not page.ok or page.signals is None:
        return SourceCells(_ABSENT, _ABSENT, _ABSENT, _ABSENT, _ABSENT)

    def text(value: str | None) -> Cell:
        if value is None:
            return _ABSENT
        return Cell(state="value", text=value) if value else _EMPTY

    keywords_cell = _site_keyword_coverage(page, claimed_keywords)
    return SourceCells(
        url=Cell(state="value", text=page.url),
        title=text(page.signals.title),
        description=text(page.signals.description),
        h1=text(page.signals.h1),
        keywords=keywords_cell,
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


def _row_verdicts(
    strategy: SourceCells | None,
    framework: SourceCells | None,
    site: SourceCells | None,
    *,
    strategy_item: ContentItem | None,
    framework_item: ContentItem | None,
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

    strategy_kw = _norm_keyword_set(strategy_item.keywords) if strategy_item else frozenset()
    framework_kw = _norm_keyword_set(framework_item.keywords) if framework_item else frozenset()

    return RowVerdicts(
        url=_scalar_verdict(cells_for("url"), url=True),
        title=_scalar_verdict(cells_for("title")),
        description=_scalar_verdict(cells_for("description")),
        h1=_scalar_verdict(cells_for("h1")),
        keywords=_keyword_verdict(strategy_kw, framework_kw, site.keywords if site else _ABSENT),
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
                ),
                links=outcome.link_audit,
                framework_document_id=outcome.source.document_id,
                framework_document_name=outcome.source.document_name,
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
