"""Три звірки пакета (task-90 §2.4/§2.5/§3): Frame ↔ Strategy (лейн H),
Frame ↔ живий сайт (лейн I), Copy ↔ живий сайт (лейн J).

Кожна звірка — окрема функція з власним `CrossCheckInfo` (тристан
`checked`/`not_checked`/`not_applicable`, той самий принцип, що
`sheet_states` у task-87): недоступний файл/сайт ніколи не малюється як
«порушень немає» (PLAN §12). Лейн H працює **на один фреймворк за раз** —
агрегатний `sync-strategy-row-no-frame` (рядок стратегії без жодного
фреймворка) вимагає бачити всі фреймворки пакета одразу, тож
`find_strategy_rows_without_framework()` викликається один раз наприкінці,
з `contentqa/package.py` (task-90 §4.6), із зібраними ключами всіх
попередніх викликів `check_frame_strategy`.

Join між Frame і рядком стратегії — за `inspector_key(url)`
(`app.urlinspector.urlkey`), не сирим URL: відсутній/наявний кінцевий слеш
губить до 96% зіставлень на цьому портфелі (той самий факт, що вже ламав
GSC↔GA4 матчинг і чому `inspector_key` взагалі існує). Резервний ключ —
нормалізована назва сторінки, лише коли URL відсутній хоч на одному боці.

⚠️ **Свідомо поза обсягом цього інкременту:** `site-internal-link-missing` —
`contentqa/site.py` (task-90 §4.4) не витягує `<a href>`-цілі, лише
title/h1/H2-H4/секції; перевірка внутрішніх лінків Frame проти сайту
вимагає окремого розширення `LivePage`, не форсується сюди під тиском обсягу.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

from app.contentqa.findings import Finding, excerpt
from app.contentqa.model import ContentDoc, ContentItem
from app.contentqa.sections import ContentSection
from app.contentqa.site import LivePage
from app.monitor.page import is_indexable
from app.urlinspector.urlkey import inspector_key

# Поріг подібності секції Copy↔сайт (task-90 §3 лейн J). Початкове значення —
# план вимагає відкалібрувати його на ≥3 живих опублікованих сторінках
# (DoD 4a/7) і **записати** обране число в Completion report; тут — стартова
# точка, не остаточне рішення.
_COPY_SIMILARITY_THRESHOLD = 0.90

CrossCheckState = Literal["checked", "not_checked", "not_applicable"]


@dataclass(frozen=True)
class CrossCheckInfo:
    name: str  # "frame_strategy" | "frame_site" | "copy_site"
    state: CrossCheckState
    reason: str | None = None


@dataclass(frozen=True)
class FrameStrategyResult:
    findings: list[Finding]
    info: CrossCheckInfo
    matched_key: str | None  # ключ рядка стратегії, якщо зіставлення відбулось


def _norm_name(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _item_key(item: ContentItem) -> str | None:
    """URL — основний ключ (`inspector_key`, не сирий рядок); назва сторінки
    — резерв, лише коли URL немає взагалі."""
    if item.url:
        return f"url:{inspector_key(item.url)}"
    if item.page_name:
        return f"name:{_norm_name(item.page_name)}"
    return None


def _drift(a: str | None, b: str | None, *, case_sensitive: bool = True) -> bool:
    """`True`, лише коли ОБИДВА боки мають непорожнє значення і воно
    розійшлось — відсутність на будь-якому боці ніколи не «дрейф»
    (PLAN §12: `None`/`""` ≠ порушення)."""
    if not a or not b:
        return False
    left, right = a.strip(), b.strip()
    if not case_sensitive:
        left, right = left.lower(), right.lower()
    return left != right


def check_frame_strategy(
    frame: ContentDoc, strategy: ContentDoc, *, known_row: ContentItem | None = None
) -> FrameStrategyResult:
    """Лейн H: title/description/h1/primary-keyword/intent Frame-документа
    проти рядка стратегії.

    `known_row` — рядок, уже зіставлений точно (task-90 §1.8): коли
    фреймворк знайдений за посиланням/чипом зі стратегії, `contentqa/
    package.py` знає ТОЧНИЙ рядок (`FrameworkLinkCandidate.sheet`/`.row`),
    і жодного вгадування за URL/назвою не потрібно — воно й не спрацювало б
    надійно. **Живо заміряно** на `SHB_SEO_STRATEGY_082626`: аркуш
    `Q3-Q4 2026 Strategy` не має розпізнаної колонки `URL` (лише `New URL`,
    не в `_ALIASES`), тож рядок дає лише ключ за назвою сторінки — а Frame
    завжди має власний `url` із таблиці "Page:" і НІКОЛИ не має `page_name`
    (§2.3, `readers/gdoc.py` не мапить floating-заголовок "Page:" на
    `page_name`), тому URL-пріоритетний `_item_key` ніколи не збігається з
    name-ключем рядка, попри те що зв'язок точно є (сам факт, що чип на
    нього посилається). Без `known_row` це дає хибний
    `sync-frame-not-in-strategy` там, де насправді все зіставлено.
    URL/name-джойн (нижче) лишається для фреймворків, знайдених резервним
    шляхом — пошуком по Диску за іменем (§1.8), де точного рядка немає."""
    if not frame.items:
        return FrameStrategyResult(
            findings=[],
            info=CrossCheckInfo("frame_strategy", "not_applicable", "Frame has no items"),
            matched_key=None,
        )
    item = frame.items[0]
    ref = item.ref

    if known_row is not None:
        return FrameStrategyResult(
            findings=_compare_frame_to_row(item, known_row, ref),
            info=CrossCheckInfo("frame_strategy", "checked"),
            matched_key=_item_key(known_row),
        )

    by_key: dict[str, ContentItem] = {}
    for row in strategy.items:
        key = _item_key(row)
        if key is not None:
            by_key.setdefault(key, row)  # перший рядок виграє при дублях ключа

    item_key = _item_key(item)
    if item_key is None:
        return FrameStrategyResult(
            findings=[Finding(
                "sync-url-unresolved", "warn", ref,
                "Framework has neither a URL nor a page name — cannot be matched "
                "to a strategy row.",
            )],
            info=CrossCheckInfo(
                "frame_strategy", "not_checked", "Framework has no URL or page name"
            ),
            matched_key=None,
        )

    strategy_row = by_key.get(item_key)
    if strategy_row is None:
        return FrameStrategyResult(
            findings=[Finding(
                "sync-frame-not-in-strategy", "warn", ref,
                f"No strategy row matches this framework's URL/page name "
                f"({item.url or item.page_name}).",
            )],
            info=CrossCheckInfo("frame_strategy", "checked"),
            matched_key=None,
        )

    return FrameStrategyResult(
        findings=_compare_frame_to_row(item, strategy_row, ref),
        info=CrossCheckInfo("frame_strategy", "checked"), matched_key=item_key,
    )


def _compare_frame_to_row(item: ContentItem, strategy_row: ContentItem, ref: str) -> list[Finding]:
    findings: list[Finding] = []
    if _drift(item.title, strategy_row.title):
        findings.append(Finding(
            "sync-title-drift", "error", ref,
            f"Frame title ({item.title!r}) differs from the strategy title "
            f"({strategy_row.title!r}).",
            value=item.title,
        ))
    if _drift(item.description, strategy_row.description):
        findings.append(Finding(
            "sync-desc-drift", "error", ref,
            "Frame description differs from the strategy description.", value=item.description,
        ))
    if _drift(item.h1, strategy_row.h1):
        findings.append(Finding(
            "sync-h1-drift", "error", ref,
            f"Frame H1 ({item.h1!r}) differs from the strategy H1 "
            f"({strategy_row.h1!r}).",
            value=item.h1,
        ))
    frame_primary = item.keywords[0] if item.keywords else None
    row_primary = strategy_row.keywords[0] if strategy_row.keywords else None
    if _drift(frame_primary, row_primary, case_sensitive=False):
        findings.append(Finding(
            "sync-keyword-drift", "error", ref,
            f"Frame primary keyword ({frame_primary!r}) differs from the strategy's "
            f"({row_primary!r}).", value=frame_primary,
        ))
    if _drift(item.intent, strategy_row.intent, case_sensitive=False):
        findings.append(Finding(
            "sync-intent-drift", "warn", ref,
            f"Frame intent ({item.intent!r}) differs from the strategy intent "
            f"({strategy_row.intent!r}).",
            value=item.intent,
        ))
    return findings


def find_strategy_rows_without_framework(
    strategy: ContentDoc, matched_keys: set[str]
) -> list[Finding]:
    """`sync-strategy-row-no-frame` (info): рядок стратегії, якого не
    зіставив жоден фреймворк пакета. Викликається package.py один раз,
    після того як `check_frame_strategy` пройшов по всіх фреймворках —
    один-до-одного порівняння цього не бачить (task-90 §2.4)."""
    findings: list[Finding] = []
    seen: set[str] = set()
    for row in strategy.items:
        key = _item_key(row)
        if key is None or key in matched_keys or key in seen:
            continue
        seen.add(key)
        findings.append(Finding(
            "sync-strategy-row-no-frame", "info", row.ref,
            f"No framework document links back to this strategy row "
            f"({row.url or row.page_name or 'unnamed'}).",
        ))
    return findings


# ── Лейн I: Frame ↔ живий сайт ────────────────────────────────────────────────


def _norm_heading(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def check_frame_site(frame: ContentDoc, site: LivePage) -> tuple[list[Finding], CrossCheckInfo]:
    if not frame.items:
        return [], CrossCheckInfo("frame_site", "not_applicable", "Frame has no items")
    item = frame.items[0]
    ref = item.ref

    if site.error == "blocked":
        return (
            [Finding(
                "site-blocked", "info", ref, f"The live page ({site.url}) blocked the fetch."
            )],
            CrossCheckInfo("frame_site", "not_checked", "blocked"),
        )
    if not site.ok or site.signals is None:
        return (
            [Finding(
                "site-unreachable", "info", ref,
                f"The live page ({site.url}) could not be fetched "
                f"({site.error or 'unknown error'}).",
            )],
            CrossCheckInfo("frame_site", "not_checked", site.error or "unreachable"),
        )

    findings: list[Finding] = []
    signals = site.signals

    if _drift(item.title, signals.title):
        findings.append(Finding(
            "site-title-drift", "error", ref,
            f"Frame title ({item.title!r}) differs from the live page's "
            f"<title> ({signals.title!r}).",
            value=item.title,
        ))
    if _drift(item.description, signals.description):
        findings.append(Finding(
            "site-desc-drift", "error", ref,
            "Frame description differs from the live page's meta description.",
            value=item.description,
        ))
    if _drift(item.h1, signals.h1):
        findings.append(Finding(
            "site-h1-drift", "error", ref,
            f"Frame H1 ({item.h1!r}) differs from the live page's H1 ({signals.h1!r}).",
            value=item.h1,
        ))

    frame_headings = tuple((lvl, txt) for lvl, txt in item.headings if lvl != "H1")
    if frame_headings:
        # Живий сайт може повторювати заголовок (карусель/слайдер, task-90
        # §4.4) — членство перевіряється по множині нормалізованого тексту,
        # не по кількості повторів.
        site_texts = {_norm_heading(txt) for _, txt in site.headings if txt}
        for level, text in frame_headings:
            if _norm_heading(text) not in site_texts:
                findings.append(Finding(
                    "site-heading-missing", "error", ref,
                    f"{level} {text!r} from the Frame is not on the live page.", value=text,
                ))

        frame_texts = {_norm_heading(t) for _, t in frame_headings}
        seen_extra: set[str] = set()
        for level, text in site.headings:
            norm = _norm_heading(text)
            if norm and norm not in frame_texts and norm not in seen_extra:
                seen_extra.add(norm)
                findings.append(Finding(
                    "site-heading-extra", "info", ref,
                    f"{level} {text!r} is on the live page but not in the Frame.", value=text,
                ))

        matched_frame = [
            (lvl, txt) for lvl, txt in frame_headings if _norm_heading(txt) in site_texts
        ]
        if len(matched_frame) >= 2:
            site_order = [
                _norm_heading(txt) for _, txt in site.headings if _norm_heading(txt) in frame_texts
            ]
            site_first_index: dict[str, int] = {}
            for i, norm in enumerate(site_order):
                site_first_index.setdefault(norm, i)
            frame_norms = [_norm_heading(txt) for _, txt in matched_frame]
            site_positions = [site_first_index[n] for n in frame_norms if n in site_first_index]
            if site_positions != sorted(site_positions):
                findings.append(Finding(
                    "site-heading-order", "warn", ref,
                    "The Frame's headings are all present on the live page, "
                    "but in a different order.",
                ))

    x_robots = site.x_robots_tag
    indexable = is_indexable(
        meta_robots=signals.meta_robots, x_robots_tag=x_robots, status=site.status
    )
    if indexable is False:
        findings.append(Finding(
            "site-noindex", "error", ref,
            "The live page is noindex, but the strategy treats it as a live, indexable page.",
        ))

    if signals.canonical and item.url:
        if inspector_key(signals.canonical) != inspector_key(item.url):
            findings.append(Finding(
                "site-canonical-mismatch", "warn", ref,
                f"The live page's canonical ({signals.canonical!r}) points elsewhere "
                f"than the Frame's URL ({item.url!r}).",
            ))

    return findings, CrossCheckInfo("frame_site", "checked")


# ── Лейн J: Copy ↔ живий сайт ─────────────────────────────────────────────────


def _dedupe_by_heading(sections: Iterable[ContentSection]) -> list[ContentSection]:
    seen: set[str] = set()
    out: list[ContentSection] = []
    for s in sections:
        norm = _norm_heading(s.heading)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(s)
    return out


def check_copy_site(
    copy_sections: tuple[ContentSection, ...], *, copy_ref: str, site: LivePage
) -> tuple[list[Finding], CrossCheckInfo]:
    if not copy_sections:
        return [], CrossCheckInfo("copy_site", "not_applicable", "Copy has no sections")

    if site.error == "blocked":
        return (
            [Finding(
                "site-blocked", "info", copy_ref,
                f"The live page ({site.url}) blocked the fetch.",
            )],
            CrossCheckInfo("copy_site", "not_checked", "blocked"),
        )
    if not site.ok:
        return (
            [Finding(
                "site-unreachable", "info", copy_ref,
                f"The live page ({site.url}) could not be fetched "
                f"({site.error or 'unknown error'}).",
            )],
            CrossCheckInfo("copy_site", "not_checked", site.error or "unreachable"),
        )

    site_by_heading: dict[str, ContentSection] = {
        _norm_heading(s.heading): s for s in _dedupe_by_heading(site.sections)
    }

    findings: list[Finding] = []
    any_matched = False
    for copy_section in copy_sections:
        norm = _norm_heading(copy_section.heading)
        site_section = site_by_heading.get(norm)
        if site_section is None:
            findings.append(Finding(
                "published-section-missing", "error", copy_ref,
                f"Section {copy_section.heading!r} from Copy is not on the live page.",
                value=copy_section.heading,
            ))
            continue
        any_matched = True
        ratio = SequenceMatcher(None, copy_section.text, site_section.text).ratio()
        if ratio < _COPY_SIMILARITY_THRESHOLD:
            findings.append(Finding(
                "published-copy-drift", "warn", copy_ref,
                f"Section {copy_section.heading!r}: published text differs from Copy "
                f"({ratio:.0%} similar). Copy: {excerpt(copy_section.text)!r} — "
                f"live: {excerpt(site_section.text)!r}.",
            ))

    if copy_sections and not any_matched:
        findings.append(Finding(
            "published-not-published", "warn", copy_ref,
            "None of Copy's headings were found on the live page — "
            "it may still be on the old copy.",
        ))
        return findings, CrossCheckInfo("copy_site", "checked")

    copy_headings = {_norm_heading(s.heading) for s in copy_sections}
    for norm, site_section in site_by_heading.items():
        if norm and norm not in copy_headings:
            findings.append(Finding(
                "published-copy-extra", "info", copy_ref,
                f"Section {site_section.heading!r} is on the live page but not in Copy.",
                value=site_section.heading,
            ))

    return findings, CrossCheckInfo("copy_site", "checked")
