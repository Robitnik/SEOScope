"""Крок 3 конвеєра: виключення, причина, бал (task-100 §2.3).

Порядок жорсткий і має значення:

1. **Не перевіряли — не судимо.** URL із `is_indexed IS NULL` не кандидат
   і не «в порядку»; він просто ще не інспектований. Плутанина саме тут
   коштувала б найдорожче: непросканований портфель виглядав би як 2311
   кандидатів замість 274 реальних (заміряно, план §1.5).
2. **Виключення — до балу.** Сторінка, яку Google свідомо не індексує,
   не змагається за денну квоту. Живо це 36 URL (12%): 15 `noindex`,
   14 редиректів, 3 `robots.txt`, 3 чужих каноніки, 1 `404`.
3. **Причина — перша підхожа зверху вниз**, з доказами як даними.

Найважливіше калібрування: `crawled_not_indexed` стоїть НИЖЧЕ за
`discovered_not_indexed`, хоча інтуїція підказує навпаки. `Discovered`
означає, що Google ще не дійшов краулити — там відправка найкорисніша.
`Crawled - currently not indexed` означає, що він уже подивився і **не
взяв**: проблема в самій сторінці, і повторна відправка без правок нічого
не змінить. Таких 78 — третина беклогу, тож ця одна строчка вирішує, чи
третина ручної роботи буде змарнована.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from app.db.models import IndexingUrl
from app.indexing.context import ClientContext, PageFacts

# --- словники -------------------------------------------------------------

EXCLUDED_NOINDEX = "noindex"
EXCLUDED_ROBOTS = "robots_blocked"
EXCLUDED_NON_CANONICAL = "non_canonical"
EXCLUDED_REDIRECT = "redirect"
EXCLUDED_HTTP_ERROR = "http_error"
EXCLUDED_MANUAL = "manual"

BASE_SCORES: dict[str, int] = {
    "dropped_out": 100,
    "never_indexed": 90,
    "discovered_not_indexed": 85,
    "new_page": 75,
    "stale_crawl": 60,
    # Свідомо нижче за `discovered_not_indexed` — див. докстрінг модуля.
    "crawled_not_indexed": 55,
    "traffic_lost": 40,
    "no_traffic": 30,
    "periodic_refresh": 10,
}

# Дія оператора: для цих причин сама відправка нічого не дасть, доки не
# поправлено сторінку.
REVIEW_FIRST = {"crawled_not_indexed", "traffic_lost", "no_traffic"}

NEW_PAGE_DAYS = 30
NO_TRAFFIC_DAYS = 90
TRAFFIC_DROP_RATIO = Decimal("0.5")


def _anchor(today: date, now: datetime | None) -> datetime:
    """Завжди timezone-aware.

    `first_seen_at`/`last_crawl_time` приходять з БД як UTC-aware
    (`UTCDateTime`), і naive-опорна точка тут дала б `TypeError` рівно на
    тому рядку, який рахує вік сторінки — тобто на кожному живому URL.
    """
    return now or datetime.combine(today, datetime.min.time(), tzinfo=UTC)


@dataclass
class Verdict:
    excluded: bool = False
    excluded_reason: str | None = None
    reason_code: str | None = None
    reason: dict[str, Any] | None = None
    score: Decimal | None = None


def action_for(reason_code: str | None) -> str | None:
    if reason_code is None:
        return None
    return "review_then_submit" if reason_code in REVIEW_FIRST else "submit"


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


def _canonical_mismatch(row: IndexingUrl) -> bool:
    google = _norm(row.google_canonical).rstrip("/")
    if not google:
        return False
    own = _norm(row.url).rstrip("/")
    user = _norm(row.user_canonical).rstrip("/")
    # Порівнюємо і з самою адресою, і з заявленим каноніком: збіг хоч із
    # чимось одним означає, що Google обрав саме цю сторінку.
    return google != own and (not user or google != user)


def detect_exclusion(row: IndexingUrl, facts: PageFacts) -> tuple[bool, str | None]:
    """Ручне виключення переважає все — його поставила людина."""
    if row.excluded and row.excluded_reason == EXCLUDED_MANUAL:
        return True, EXCLUDED_MANUAL

    state = _norm(row.coverage_state)
    indexing_state = _norm(row.indexing_state)
    robots_state = _norm(row.robots_txt_state)
    fetch_state = _norm(row.page_fetch_state)

    # Матч підрядком, а не рівністю: у `Excluded by ‘noindex’ tag` лапки
    # типографські (U+2018/U+2019), і ASCII-порівняння промахнулося б.
    if "noindex" in state or "noindex" in indexing_state or facts.indexable is False:
        return True, EXCLUDED_NOINDEX
    if "disallow" in robots_state or "blocked by robots" in state:
        return True, EXCLUDED_ROBOTS
    if "redirect" in state:
        return True, EXCLUDED_REDIRECT
    if "not found" in state or "404" in state:
        return True, EXCLUDED_HTTP_ERROR
    if fetch_state and fetch_state not in ("successful", "page_fetch_state_unspecified"):
        return True, EXCLUDED_HTTP_ERROR
    if facts.http_status is not None and not (200 <= facts.http_status < 300):
        return True, EXCLUDED_HTTP_ERROR
    if "duplicate" in state or "alternate page" in state or _canonical_mismatch(row):
        return True, EXCLUDED_NON_CANONICAL
    return False, None


def _traffic_reason(
    facts: PageFacts, *, indexed_for_days: int | None
) -> tuple[str, dict[str, Any]] | None:
    recent = facts.clicks_recent
    previous = facts.clicks_previous
    if previous is not None and previous > 0 and recent is not None:
        if Decimal(recent) <= Decimal(previous) * TRAFFIC_DROP_RATIO:
            return "traffic_lost", {
                "clicks_previous": previous,
                "clicks_recent": recent,
                "impressions_previous": facts.impressions_previous,
                "impressions_recent": facts.impressions_recent,
            }
    # `impressions_recent is None` = рядка немає взагалі. Це і є «жодного
    # показу», на відміну від синтетичного нуля, який ми відкинули ще в
    # `context`.
    if (
        facts.impressions_recent is None
        and indexed_for_days is not None
        and indexed_for_days >= NO_TRAFFIC_DAYS
    ):
        return "no_traffic", {"window_days": NO_TRAFFIC_DAYS, "impressions_recent": None}
    return None


def evaluate(
    row: IndexingUrl,
    facts: PageFacts,
    *,
    today: date,
    now: datetime | None = None,
) -> Verdict:
    moment = _anchor(today, now)

    # 1. Ще не інспектували — жодного вердикту.
    if row.inspected_at is None or row.is_indexed is None:
        return Verdict()

    # 2. Виключення.
    excluded, excluded_reason = detect_exclusion(row, facts)
    if excluded:
        return Verdict(excluded=True, excluded_reason=excluded_reason)

    state = _norm(row.coverage_state)

    # 3. Причина.
    if row.is_indexed is False:
        if row.left_index_at is not None:
            return _verdict(
                "dropped_out",
                {"coverage_state": row.coverage_state, "left_index_at": _iso(row.left_index_at)},
            )
        if "unknown to google" in state:
            return _verdict("never_indexed", {"coverage_state": row.coverage_state})
        if "discovered" in state:
            return _verdict("discovered_not_indexed", {"coverage_state": row.coverage_state})
        if "crawled" in state:
            return _verdict(
                "crawled_not_indexed",
                {
                    "coverage_state": row.coverage_state,
                    "last_crawl_time": _iso(row.last_crawl_time),
                },
            )
        first_seen = row.first_seen_at
        if first_seen is not None and (moment - first_seen).days <= NEW_PAGE_DAYS:
            return _verdict(
                "new_page",
                {
                    "coverage_state": row.coverage_state,
                    "first_seen_at": _iso(first_seen),
                    "sitemap_lastmod": row.sitemap_lastmod.isoformat()
                    if row.sitemap_lastmod
                    else None,
                },
            )
        return _verdict("never_indexed", {"coverage_state": row.coverage_state})

    # 4. У індексі — але, можливо, застарілий краул чи втрачений трафік.
    if (
        facts.last_changed_at is not None
        and row.last_crawl_time is not None
        and facts.last_changed_at > row.last_crawl_time
    ):
        return _verdict(
            "stale_crawl",
            {
                "changed_at": _iso(facts.last_changed_at),
                "last_crawl_time": _iso(row.last_crawl_time),
            },
        )

    indexed_for_days = (
        (moment - row.first_indexed_seen_at).days if row.first_indexed_seen_at else None
    )
    traffic = _traffic_reason(facts, indexed_for_days=indexed_for_days)
    if traffic is not None:
        code, evidence = traffic
        return _verdict(code, evidence)

    return Verdict()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _verdict(code: str, evidence: dict[str, Any]) -> Verdict:
    return Verdict(
        reason_code=code,
        reason=evidence,
        score=Decimal(BASE_SCORES[code]),
    )


def apply_modifiers(
    verdict: Verdict,
    row: IndexingUrl,
    facts: PageFacts,
    *,
    today: date,
    now: datetime | None = None,
) -> None:
    """± до бази, не множники — щоб бал лишався читабельним в UI.

    Модифікатори свідомо слабкі (одиниці): вони розсовують рядки всередині
    однієї причини, але ніколи не перекидають URL через межу класу — інакше
    «просіла» могла б обігнати «Google її не знає», і порядок роботи
    перестав би відповідати поясненню, яке бачить оператор.
    """
    if verdict.score is None:
        return
    score = verdict.score
    depth = row.url_key.count("/")
    # Глибша сторінка — трохи нижчий пріоритет: коренева важливіша.
    score -= Decimal(min(max(depth - 3, 0), 5))
    if facts.title:
        score += Decimal(2)
    if facts.description:
        score += Decimal(1)
    if row.first_seen_at is not None:
        waiting_days = (_anchor(today, now) - row.first_seen_at).days
        score += Decimal(min(waiting_days // 30, 5))
    verdict.score = score


def score_client_urls(
    rows: list[IndexingUrl],
    context: ClientContext,
    *,
    today: date,
    now: datetime | None = None,
) -> dict[str, int]:
    """Проставити вердикт кожному рядку. Повертає розклад по причинах."""
    counts: dict[str, int] = {}
    for row in rows:
        if row.gone_from_sitemap_at is not None:
            continue
        facts = context.get(row.url_key)
        manual = row.excluded and row.excluded_reason == EXCLUDED_MANUAL
        verdict = evaluate(row, facts, today=today, now=now)
        apply_modifiers(verdict, row, facts, today=today, now=now)

        if manual:
            # Ручне виключення не перезаписується автоматикою.
            row.reason_code = None
            row.reason_json = {}
            row.priority_score = None
            counts[EXCLUDED_MANUAL] = counts.get(EXCLUDED_MANUAL, 0) + 1
            continue

        row.excluded = verdict.excluded
        row.excluded_reason = verdict.excluded_reason
        row.reason_code = verdict.reason_code
        row.reason_json = verdict.reason or {}
        row.priority_score = verdict.score
        key = verdict.excluded_reason or verdict.reason_code or "ok"
        counts[key] = counts.get(key, 0) + 1
    return counts
