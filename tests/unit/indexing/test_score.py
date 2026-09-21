"""Оцінка кандидатів (task-100 §2.3).

Найважливіші тут не «щасливі» випадки, а три, що вже ламались або могли:
типографські лапки в `noindex`, тризначність `is_indexed` і порядок
`crawled` нижче за `discovered`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.db.models import IndexingUrl
from app.indexing.context import PageFacts
from app.indexing.score import BASE_SCORES, action_for, evaluate

TODAY = date(2026, 9, 21)
NOW = datetime(2026, 9, 21, tzinfo=UTC)


def make(**kwargs) -> IndexingUrl:
    row = IndexingUrl(client_id="c", url="https://x.test/page", url_key="x.test/page")
    row.inspected_at = NOW
    row.first_seen_at = NOW - timedelta(days=200)
    row.is_indexed = False
    for key, value in kwargs.items():
        setattr(row, key, value)
    return row


def verdict_for(**kwargs):
    return evaluate(make(**kwargs), PageFacts(), today=TODAY, now=NOW)


def test_not_inspected_yet_is_not_a_candidate() -> None:
    """`None` — це «ще не питали», і воно НЕ означає «не в індексі».

    Якби це читалось як `False`, непросканований портфель дав би 2311
    кандидатів замість 274 реальних (заміряно, план §1.5).
    """
    verdict = verdict_for(inspected_at=None, is_indexed=None)
    assert verdict.reason_code is None
    assert verdict.excluded is False
    assert verdict.score is None


@pytest.mark.parametrize(
    ("coverage_state", "expected"),
    [
        ("URL is unknown to Google", "never_indexed"),
        ("Discovered - currently not indexed", "discovered_not_indexed"),
        ("Crawled - currently not indexed", "crawled_not_indexed"),
    ],
)
def test_candidate_reasons(coverage_state: str, expected: str) -> None:
    assert verdict_for(coverage_state=coverage_state).reason_code == expected


def test_crawled_scores_below_discovered() -> None:
    """Google уже подивився і не взяв — відправка без правок марна.

    Це третина беклогу (78 із 274 живо), тож переставити ці два місцями
    означало б змарнувати третину ручної роботи.
    """
    assert BASE_SCORES["crawled_not_indexed"] < BASE_SCORES["discovered_not_indexed"]
    assert action_for("crawled_not_indexed") == "review_then_submit"
    assert action_for("discovered_not_indexed") == "submit"


def test_noindex_with_typographic_quotes_is_excluded() -> None:
    """Google віддає `Excluded by ‘noindex’ tag` із U+2018/U+2019.

    Порівняння з ASCII-апострофом промахнулось би, і сторінка з `noindex`
    щодня з'їдала б слот денної квоти.
    """
    verdict = verdict_for(coverage_state="Excluded by ‘noindex’ tag")
    assert verdict.excluded is True
    assert verdict.excluded_reason == "noindex"
    assert verdict.reason_code is None


@pytest.mark.parametrize(
    ("coverage_state", "reason"),
    [
        ("Page with redirect", "redirect"),
        ("Not found (404)", "http_error"),
        ("Blocked by robots.txt", "robots_blocked"),
        ("Duplicate, Google chose different canonical than user", "non_canonical"),
        ("Alternate page with proper canonical tag", "non_canonical"),
    ],
)
def test_exclusion_classes(coverage_state: str, reason: str) -> None:
    verdict = verdict_for(coverage_state=coverage_state)
    assert (verdict.excluded, verdict.excluded_reason) == (True, reason)


def test_redirect_is_not_reported_as_http_error() -> None:
    """Редирект — не поламана сторінка, і оператора не можна слати
    лагодити те, що не зламане (14 URL живо)."""
    assert verdict_for(coverage_state="Page with redirect").excluded_reason == "redirect"


def test_noindex_from_page_inventory_without_google_saying_so() -> None:
    row = make(coverage_state="Crawled - currently not indexed")
    verdict = evaluate(row, PageFacts(indexable=False), today=TODAY, now=NOW)
    assert (verdict.excluded, verdict.excluded_reason) == (True, "noindex")


def test_dropped_out_beats_everything() -> None:
    row = make(coverage_state="URL is unknown to Google", left_index_at=NOW - timedelta(days=2))
    verdict = evaluate(row, PageFacts(), today=TODAY, now=NOW)
    assert verdict.reason_code == "dropped_out"
    assert verdict.score == BASE_SCORES["dropped_out"]


def test_indexed_page_is_left_alone() -> None:
    verdict = verdict_for(is_indexed=True, coverage_state="Submitted and indexed")
    assert verdict.reason_code is None
    assert verdict.excluded is False


def test_stale_crawl_needs_a_change_after_the_crawl() -> None:
    row = make(is_indexed=True, coverage_state="Submitted and indexed")
    row.last_crawl_time = NOW - timedelta(days=10)
    fresh = evaluate(
        row, PageFacts(last_changed_at=NOW - timedelta(days=2)), today=TODAY, now=NOW
    )
    assert fresh.reason_code == "stale_crawl"
    older = evaluate(
        row, PageFacts(last_changed_at=NOW - timedelta(days=30)), today=TODAY, now=NOW
    )
    assert older.reason_code is None


def test_traffic_lost_uses_absence_not_zero() -> None:
    """`impressions is None` = рядка немає (GSC не показував сторінку).

    Синтетичні нулі GSC відкидає вже `context`, тож сюди `0` як «виміряний
    нуль» не приходить — і `no_traffic` не має спрацьовувати на сторінці,
    яку ми просто не питали.
    """
    row = make(is_indexed=True, coverage_state="Submitted and indexed")
    row.first_indexed_seen_at = NOW - timedelta(days=200)
    dropped = evaluate(
        row, PageFacts(clicks_previous=40, clicks_recent=3), today=TODAY, now=NOW
    )
    assert dropped.reason_code == "traffic_lost"
    assert dropped.reason["clicks_previous"] == 40

    silent = evaluate(row, PageFacts(), today=TODAY, now=NOW)
    assert silent.reason_code == "no_traffic"


def test_reason_carries_evidence_not_prose() -> None:
    """У рядку лежать дані, а не готове речення: формулювання можна
    виправити, не переписуючи історію."""
    verdict = verdict_for(coverage_state="Discovered - currently not indexed")
    assert verdict.reason == {"coverage_state": "Discovered - currently not indexed"}
