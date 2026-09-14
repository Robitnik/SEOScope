"""Оркестратор пакета (task-90 §2.6/§4.6): QA стратегії -> bounded fan-out
по фреймворках (кожен: Frame QA + Copy QA + три звірки) -> `PackageResult`.

Чиста функція — жодного HTTP-звʼязку, жодної сесії БД, жодного FastAPI-
об'єкта. Причина конкретна (task-90 §2.6): повний прогін на ~10 фреймворків
— це ~1 виклик Sheets + N Docs + до 2N live-фетчів, і чи влазить це в
синхронний HTTP-запит — питання заміру (task-90 DoD 9), не здогадки. Якщо
замір скаже «не влазить», перенесення `run_package` у job-рушій буде тонким
адаптером навколо цієї самої функції.

**Ізоляція клітинки (PLAN §9.1, task-90 §2.5):** один фреймворк = одна
клітинка. Його збій (недоступний документ, невпізнані вкладки, необроблений
виняток десь у ланцюжку) ніколи не валить прогін — повертається як чесний
`FrameworkOutcome.state`, решта фреймворків обробляються далі. Між
фреймворками — паралельно (`ThreadPoolExecutor`, `max_workers`); усередині
одного фреймворка — послідовно (Docs -> Frame QA -> звірки -> Copy QA), бо
це одна доріжка (PLAN §9.1: між сервісами паралельно, усередині сервісу
послідовно — той самий принцип тут застосований до фреймворків одного
пакета).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from typing import Literal

from app.contentqa.copydiff import CopyTabResult, build_copy_tab_result, genre_of
from app.contentqa.crosscheck import (
    CrossCheckInfo,
    check_copy_site,
    check_frame_site,
    check_frame_strategy,
    find_strategy_rows_without_framework,
)
from app.contentqa.engine import run_checks
from app.contentqa.findings import Finding
from app.contentqa.links import LinkAudit, audit_links
from app.contentqa.model import ContentDoc, ContentItem
from app.contentqa.readers.gdoc import copy_tab_blocks, copy_tab_links, read_framework_document
from app.contentqa.rules import Profile
from app.contentqa.site import LivePage, fetch_page
from app.integrations.google_api.docs_client import DocsClient, DocTab
from app.monitor.fetcher import FetchStats
from app.tools.http_safety import HostThrottle

# Нижче цієї довжини вкладка `Content` вважається "копію ще не написали", не
# "копія без порушень" (task-90 §2.3 DoD 4a). Перекалібровано на task-92
# §1.2: 6000 (task-90's число, зважений лише на блоги SHB, 8-11k символів)
# відкидав 56/96 живих копі-вкладок ЯК ГРУПУ, включно з повністю готовими
# продуктовими сторінками LAXTON (2034-3811 символів) і локейшн-сторінками
# EFFYS. Живий розподіл довжини прози по всіх 96 копі-вкладках портфеля
# (task-92 §1.2): 50, 313, 319 | 1630, 1974, 2034, ... 5831 | 6158, ...
# 623287 — справжня прогалина "ще не написано" лежить між **319 і 1630**,
# не між 5674 і 8038. 600 узяте усередині цієї прогалини, ближче до
# "порожньої" групи (найдовше заміряне "порожнє" значення — 319).
MIN_COPY_CHARS = 600

FrameworkState = Literal["checked", "not_accessible", "no_recognized_tabs"]


@dataclass(frozen=True)
class FrameworkSource:
    """Один кандидат-фреймворк, готовий до обробки — уніфікує два джерела
    пошуку (task-90 §1.8): посилання зі стратегії (`origin="strategy_link"`,
    точний рядок відомий заздалегідь) і резервний пошук по Диску за іменем
    клієнта (`origin="drive_search"`, прив'язки до рядка немає)."""

    document_id: str
    document_name: str | None
    origin: Literal["strategy_link", "drive_search"]
    # `ContentItem.ref` рядка-джерела в стратегії — лише для `strategy_link`,
    # дає `check_frame_strategy` точний рядок замість вгадування за URL/
    # назвою (task-90 §4.5, живо заміряна пастка на SHB: стратегія без
    # розпізнаної колонки URL робить вгадування ненадійним, хоча зв'язок
    # відомий точно).
    strategy_ref: str | None = None


@dataclass(frozen=True)
class FrameworkOutcome:
    source: FrameworkSource
    state: FrameworkState
    reason: str | None = None
    frame_tab_title: str | None = None
    copy_tab_title: str | None = None
    findings: list[Finding] = field(default_factory=list)
    frame_strategy_info: CrossCheckInfo | None = None
    frame_site_info: CrossCheckInfo | None = None
    copy_site_info: CrossCheckInfo | None = None
    matched_strategy_key: str | None = None
    # task-91 §2.2: findings above say WHAT is wrong; the comparison matrix
    # needs the raw values themselves (url/title/description/h1/keywords/
    # links) — discarded after building findings until now. Both `None`
    # when there is no Frame tab / `check_site=False` / the fetch failed;
    # `matrix.py` treats that the same as any other "nothing to compare".
    frame_item: ContentItem | None = None
    site_page: LivePage | None = None
    # Лейн K (task-92): посегментна звірка КОЖНОЇ копі-вкладки документа з
    # живою сторінкою — окрема таблиця "речення в Copy / речення на сайті
    # / чи сходиться", не findings. `()` коли документ не має копі-вкладок
    # ВЗАГАЛІ, `check_site=False`, чи `check_published=False`.
    copy_diffs: tuple[CopyTabResult, ...] = ()
    # Ланцюг Framework -> Copy -> Site (task-95): чи приписані Framework-
    # посилання реально потрапили в копію, і чи посилання копії реально є
    # на живій сторінці. Незалежно від `copy_diffs`/task-92 — рахується,
    # доки є Framework-документ, навіть коли `check_published=False`.
    # `None`, коли рахувати нема з чого взагалі (нема ні Frame, ні Copy
    # документа — `no_recognized_tabs`).
    link_audit: LinkAudit | None = None


# ── Лейн K: Copy ↔ живий сайт, посегментно (task-92 §2.1/§2.5) ──────────────

# "English Content" -> "en", "Swedish Content" -> "sv" — найкраще
# припущення з НАЗВИ вкладки (task-92 §2.5), не з вмісту: документи з
# однією безіменною "Content"-вкладкою (переважна більшість) дають
# `None` — "нема думки", не "англійська за замовчуванням".
_LANGUAGE_HINT_WORDS: tuple[tuple[str, str], ...] = (
    ("english", "en"), ("swedish", "sv"), ("spanish", "es"), ("french", "fr"),
    ("german", "de"), ("italian", "it"), ("portuguese", "pt"), ("dutch", "nl"),
    ("polish", "pl"), ("danish", "da"), ("norwegian", "no"), ("finnish", "fi"),
)
_LANGUAGE_HINT_RES = tuple(
    (re.compile(rf"\b{word}\b", re.IGNORECASE), code) for word, code in _LANGUAGE_HINT_WORDS
)
# Живо заміряно на LAXTON (task-92 §1.8): найгірша "своя" мовна вкладка —
# 9% покриття (стара копія, справжня зміна на сайті), найкраща "чужа" —
# 13% (спільні токени-запозичення дають випадкові збіги). Поріг лежить
# усередині цієї прогалини — нижче нього різномовна вкладка вважається
# "нема опублікованої сторінки цією мовою", не провалом звірки.
_LANGUAGE_MISMATCH_COVERAGE_FLOOR = 0.25


def _language_hint(tab_title: str) -> str | None:
    folded = re.sub(r"[_\-]+", " ", tab_title or "")
    for pattern, code in _LANGUAGE_HINT_RES:
        if pattern.search(folded):
            return code
    return None


def _run_copy_lane(
    copy_tabs: tuple[DocTab, ...], *, site_page: LivePage | None, has_url: bool
) -> tuple[CopyTabResult, ...]:
    """Кожна копі-вкладка документа проти ЖИВОЇ сторінки (лейн K, task-92
    §2.1) — той самий `site_page`, що вже прочитаний для лейну I
    (`check_frame_site`), нуль додаткових HTTP-запитів (task-92 §1.9).
    Роль (`primary`/`secondary`) вирішує ПОКРИТТЯ (task-92 §2.5,
    LAXTON: дві копі-вкладки на документ, правильну видає лише
    порівняння з сайтом, не назва)."""
    if not copy_tabs:
        return ()

    # `role` here only distinguishes "shown expanded" from "collapsed
    # behind a <details>" in the UI (task-92 §2.7) — with no comparison
    # possible at all (no URL / site unreachable), there is no "worse
    # alternative" to hide, so the FIRST tab still gets `"primary"` (most
    # documents have exactly one copy tab; blindly marking every tab
    # `"secondary"` would collapse a lone blog's only Copy tab by default).
    if not has_url:
        return tuple(
            CopyTabResult(
                tab_title=t.title, genre=genre_of(copy_tab_blocks(t)),
                role="primary" if i == 0 else "secondary",
                state="no_url", reason="This framework has no URL to compare against.",
                language_hint=None,
            )
            for i, t in enumerate(copy_tabs)
        )

    assert site_page is not None  # `has_url=True` -> caller already fetched it
    if not site_page.ok:
        return tuple(
            CopyTabResult(
                tab_title=t.title, genre=genre_of(copy_tab_blocks(t)),
                role="primary" if i == 0 else "secondary",
                state="not_checked", reason=site_page.error or "unreachable", language_hint=None,
            )
            for i, t in enumerate(copy_tabs)
        )

    site_lang = (site_page.lang or "").split("-")[0].lower() or None
    outcomes: list[CopyTabResult] = []
    for tab in copy_tabs:
        blocks = copy_tab_blocks(tab)
        if not blocks:
            continue  # вкладка знайдена за іменем, але порожня — нема з чим звіряти
        hint = _language_hint(tab.title)
        cross_language = bool(hint and site_lang and hint != site_lang)
        result = build_copy_tab_result(
            tab_title=tab.title, doc_blocks=blocks, site_blocks=site_page.blocks,
            cross_language=cross_language,
        )
        if cross_language and result.stats.coverage < _LANGUAGE_MISMATCH_COVERAGE_FLOOR:
            outcomes.append(CopyTabResult(
                tab_title=tab.title, genre=result.genre, role="secondary",
                state="not_applicable", reason="No published page in this language.",
                language_hint=hint,
            ))
        else:
            outcomes.append(replace(result, role="secondary", language_hint=hint))

    if not outcomes:
        return ()

    checked = [o for o in outcomes if o.state == "checked"]
    primary_title = (
        max(checked, key=lambda o: o.stats.coverage).tab_title if checked else outcomes[0].tab_title
    )
    return tuple(
        replace(o, role="primary") if o.tab_title == primary_title else o for o in outcomes
    )


def _primary_copy_tab(
    all_copy_tabs: tuple[DocTab, ...], copy_diffs: tuple[CopyTabResult, ...]
) -> DocTab | None:
    """Яка копі-вкладка — "основна" для звірки посилань (task-95 §2.3):
    та сама роль, що лейн K уже вирішив за ПОКРИТТЯМ проти живої сторінки
    (`_run_copy_lane` вище, task-92 §2.5 — LAXTON: дві мовні вкладки на
    документ, правильну видає лише порівняння з сайтом, не назва). Коли
    `copy_diffs` порожній (нема URL / `check_published=False`) —
    вирішувати нема з чого, перша вкладка документа."""
    if not all_copy_tabs:
        return None
    primary_title = next((o.tab_title for o in copy_diffs if o.role == "primary"), None)
    if primary_title is not None:
        for tab in all_copy_tabs:
            if tab.title == primary_title:
                return tab
    return all_copy_tabs[0]


@dataclass(frozen=True)
class PackageResult:
    strategy_findings: list[Finding]
    frameworks: list[FrameworkOutcome]
    # `sync-strategy-row-no-frame` — агрегатний фіндинг, побудований після
    # того, як усі фреймворки оброблені (task-90 §2.4): один-до-одного
    # порівняння цього не бачить, потрібна повна картина `matched_key`.
    aggregate_findings: list[Finding]


def run_framework(
    source: FrameworkSource,
    *,
    docs_client: DocsClient,
    strategy: ContentDoc,
    profile: Profile,
    throttle: HostThrottle,
    stats: FetchStats,
    check_site: bool = True,
    check_published: bool = True,
    min_copy_chars: int = MIN_COPY_CHARS,
    known_row: ContentItem | None = None,
) -> FrameworkOutcome:
    """Один фреймворк, повністю ізольовано. Не кидає винятків, які вийшли б
    за межі своєї обробки логіки (Docs API/QA-рушій) — транспортні помилки з
    `docs_client.get_document` уже фолдяться в `None` там-таки (task-90
    §4.1); тут ловиться решта (наприклад несподівана форма даних)."""
    tabs = docs_client.get_document(source.document_id)
    if tabs is None:
        return FrameworkOutcome(
            source=source, state="not_accessible",
            reason="Document not accessible — share it with the agency account.",
        )

    resolved = read_framework_document(tabs, source=source.document_name or source.document_id)
    if resolved.frame_doc is None and resolved.copy_doc is None:
        return FrameworkOutcome(
            source=source, state="no_recognized_tabs",
            reason="No Framework or Content tab recognized in this document.",
            frame_tab_title=resolved.frame_tab_title, copy_tab_title=resolved.copy_tab_title,
        )

    ref = source.document_name or source.document_id
    findings: list[Finding] = []
    frame_strategy_info: CrossCheckInfo | None = None
    frame_site_info: CrossCheckInfo | None = None
    copy_site_info: CrossCheckInfo | None = None
    matched_key: str | None = None
    site_page: LivePage | None = None

    if resolved.unnamed_single_tab:
        findings.append(Finding(
            "framework-tab-unnamed", "info", ref,
            "This document has a single, unnamed tab ('Tab 1') — read as Framework.",
        ))
    elif resolved.frame_matched_by_content:
        findings.append(Finding(
            "framework-tab-unnamed", "info", ref,
            "This document's Framework tab has no recognized name (e.g. 'Tab 1') — "
            "matched by its table fields instead.",
        ))
    elif resolved.frame_doc is None and resolved.copy_tab_title is not None:
        findings.append(Finding(
            "framework-no-frame-tab", "info", ref,
            "This document has a Content tab but no Framework tab — "
            "no strategy/site comparison is possible.",
        ))

    frame_item: ContentItem | None = None
    if resolved.frame_doc is not None:
        frame_item = resolved.frame_doc.items[0]
        findings.extend(run_checks(resolved.frame_doc, profile))

        fs_result = check_frame_strategy(resolved.frame_doc, strategy, known_row=known_row)
        findings.extend(fs_result.findings)
        frame_strategy_info = fs_result.info
        matched_key = fs_result.matched_key

    copy_body_len = len(resolved.copy_doc.items[0].body or "") if resolved.copy_doc else 0
    copy_too_short = resolved.copy_doc is not None and copy_body_len < min_copy_chars
    if resolved.copy_tab_title is not None and copy_too_short:
        findings.append(Finding(
            "copy-too-short", "info", f"{ref} › {resolved.copy_tab_title}",
            f"Content tab has only {copy_body_len} characters — likely not written "
            f"yet, not checked for readability/voice/slop.",
        ))
    elif resolved.copy_doc is not None:
        findings.extend(run_checks(resolved.copy_doc, profile))

    copy_diffs: tuple[CopyTabResult, ...] = ()
    has_url = resolved.frame_doc is not None and frame_item is not None and bool(frame_item.url)
    if check_site and has_url:
        assert resolved.frame_doc is not None and frame_item is not None and frame_item.url
        frame_doc = resolved.frame_doc  # локальна прив'язка — вужче звуження типу для mypy
        site_page = fetch_page(frame_item.url, throttle=throttle, stats=stats)
        site_findings, frame_site_info = check_frame_site(frame_doc, site_page)
        findings.extend(site_findings)

        copy_doc = resolved.copy_doc
        copy_publishable = copy_doc is not None and not copy_too_short and resolved.copy_sections
        if check_published and copy_publishable and copy_doc is not None:
            copy_findings, copy_site_info = check_copy_site(
                resolved.copy_sections, copy_ref=copy_doc.items[0].ref, site=site_page
            )
            findings.extend(copy_findings)

        if check_published:
            copy_diffs = _run_copy_lane(resolved.all_copy_tabs, site_page=site_page, has_url=True)
    elif check_site and check_published and resolved.all_copy_tabs:
        # Блог-фреймворки (task-92 §1.10) не мають URL НІДЕ — ні в
        # таблиці Framework, ні в рядку стратегії — звірка з сайтом
        # неможлива в принципі. Чесний `no_url` на кожній копі-вкладці,
        # не порожня таблиця (task-92 DoD #7), а не мовчазне "нема лейну".
        copy_diffs = _run_copy_lane(resolved.all_copy_tabs, site_page=None, has_url=False)

    # Ланцюг посилань Framework -> Copy -> Site (task-95 §2.3) — незалежно
    # від `check_published`: "основна" копі-вкладка переїжджає з `copy_diffs`,
    # коли він порахований (роль за покриттям, task-92 §2.5), інакше перша
    # вкладка документа. `frame_item`/`site_page` — ті самі об'єкти, що
    # вище, нуль додаткових HTTP-запитів чи повторного читання Docs.
    primary_copy_tab = _primary_copy_tab(resolved.all_copy_tabs, copy_diffs)
    copy_links = copy_tab_links(primary_copy_tab) if primary_copy_tab is not None else ()
    copy_genre = (
        genre_of(copy_tab_blocks(primary_copy_tab)) if primary_copy_tab is not None else None
    )
    link_audit = audit_links(
        frame_item,
        copy_links=copy_links,
        has_copy_tab=primary_copy_tab is not None,
        copy_genre=copy_genre,
        site_page=site_page,
        has_url=has_url,
    )

    return FrameworkOutcome(
        source=source, state="checked",
        frame_tab_title=resolved.frame_tab_title, copy_tab_title=resolved.copy_tab_title,
        findings=findings,
        frame_strategy_info=frame_strategy_info, frame_site_info=frame_site_info,
        copy_site_info=copy_site_info, matched_strategy_key=matched_key,
        frame_item=frame_item, site_page=site_page, copy_diffs=copy_diffs,
        link_audit=link_audit,
    )


def run_package(
    strategy: ContentDoc,
    sources: list[FrameworkSource],
    *,
    docs_client: DocsClient,
    profile: Profile | None = None,
    check_site: bool = True,
    check_published: bool = True,
    min_copy_chars: int = MIN_COPY_CHARS,
    max_workers: int = 4,
    framework_runner: Callable[..., FrameworkOutcome] = run_framework,
) -> PackageResult:
    """QA стратегії + всі фреймворки пакета. `framework_runner` — параметр
    ін'єкції для тестів (замінити `run_framework` фейком без реальних Docs/
    HTTP-викликів), продакшн-виклики його не передають."""
    profile = profile or Profile()
    strategy_findings = run_checks(strategy, profile)

    throttle = HostThrottle()
    stats = FetchStats()
    strategy_by_ref = {item.ref: item for item in strategy.items}

    def _run_one(source: FrameworkSource) -> FrameworkOutcome:
        known_row = strategy_by_ref.get(source.strategy_ref) if source.strategy_ref else None
        try:
            return framework_runner(
                source, docs_client=docs_client, strategy=strategy, profile=profile,
                throttle=throttle, stats=stats, check_site=check_site,
                check_published=check_published, min_copy_chars=min_copy_chars,
                known_row=known_row,
            )
        except Exception as exc:  # noqa: BLE001 - isolation (task-90 §2.5): a
            # bug or transient failure anywhere in one framework's processing
            # must never take the whole package run down with it.
            return FrameworkOutcome(
                source=source, state="not_accessible",
                reason=f"Unexpected error while processing this framework: {type(exc).__name__}",
            )

    outcomes: list[FrameworkOutcome] = []
    if sources:
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            future_to_source = {pool.submit(_run_one, s): s for s in sources}
            for future in as_completed(future_to_source):
                outcomes.append(future.result())
        # Стабільний порядок для UI/тестів — порядок вхідного списку, не
        # порядок завершення потоків (недетермінований).
        order = {id(s): i for i, s in enumerate(sources)}
        outcomes.sort(key=lambda o: order[id(o.source)])

    matched_keys = {o.matched_strategy_key for o in outcomes if o.matched_strategy_key}
    aggregate_findings = find_strategy_rows_without_framework(strategy, matched_keys)

    return PackageResult(
        strategy_findings=strategy_findings, frameworks=outcomes,
        aggregate_findings=aggregate_findings,
    )
