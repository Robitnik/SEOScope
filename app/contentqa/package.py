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

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Literal

from app.contentqa.crosscheck import (
    CrossCheckInfo,
    check_copy_site,
    check_frame_site,
    check_frame_strategy,
    find_strategy_rows_without_framework,
)
from app.contentqa.engine import run_checks
from app.contentqa.findings import Finding
from app.contentqa.model import ContentDoc, ContentItem
from app.contentqa.readers.gdoc import read_framework_document
from app.contentqa.rules import Profile
from app.contentqa.site import fetch_page
from app.integrations.google_api.docs_client import DocsClient
from app.monitor.fetcher import FetchStats
from app.tools.http_safety import HostThrottle

# Нижче цієї довжини вкладка `Content` вважається "копію ще не написали", не
# "копія без порушень" (task-90 §2.3 DoD 4a). Заміряний живий розподіл
# (task-90 §1.4a): 1 / 322 / 1446 / 2977 / 3614 / 5674 символів у документах
# без готової копії, проти 8038-11256 у документах зі справжньою блоговою
# прозою — між 5674 і 8038 лежить порожня прогалина без жодного живого
# заміру. 6000 узятий усередині цієї прогалини, ближче до "порожньої"
# групи (усі 6 заміряних "порожніх" значень лишаються нижче порогу).
# **Не остаточне число** — DoD 4a вимагає калібрування на ширшому живому
# зрізі, це обґрунтована стартова точка.
MIN_COPY_CHARS = 6000

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

    if resolved.unnamed_single_tab:
        findings.append(Finding(
            "framework-tab-unnamed", "info", ref,
            "This document has a single, unnamed tab ('Tab 1') — read as Framework.",
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

    if check_site and resolved.frame_doc is not None and frame_item is not None and frame_item.url:
        frame_doc = resolved.frame_doc  # локальна прив'язка — вужче звуження типу для mypy
        page = fetch_page(frame_item.url, throttle=throttle, stats=stats)
        site_findings, frame_site_info = check_frame_site(frame_doc, page)
        findings.extend(site_findings)

        copy_doc = resolved.copy_doc
        copy_publishable = copy_doc is not None and not copy_too_short and resolved.copy_sections
        if check_published and copy_publishable and copy_doc is not None:
            copy_findings, copy_site_info = check_copy_site(
                resolved.copy_sections, copy_ref=copy_doc.items[0].ref, site=page
            )
            findings.extend(copy_findings)

    return FrameworkOutcome(
        source=source, state="checked",
        frame_tab_title=resolved.frame_tab_title, copy_tab_title=resolved.copy_tab_title,
        findings=findings,
        frame_strategy_info=frame_strategy_info, frame_site_info=frame_site_info,
        copy_site_info=copy_site_info, matched_strategy_key=matched_key,
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
