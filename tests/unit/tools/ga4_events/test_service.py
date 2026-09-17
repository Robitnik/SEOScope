"""Юніти GA4 Events by Month (task-98).

Два тести тут охороняють числа, які вже були помилковими на живих даних:
`test_shares_are_measured_against_each_segments_own_totals` (відсотки Organic
1.16%, а не 1.22% — різниця CONTAINS проти EXACT) і
`test_users_total_is_not_the_column_sum` (сума колонки Users ×3.9 більша за
property-тотал).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.integrations.google_api.errors import GoogleApiError
from app.tools.ga4_events import service

from .fakes import FakeGa4Session, ga4_response

_EVENT_METRICS = list(service.EVENT_METRICS)  # eventCount, totalUsers, eventCountPerUser, keyEvents
_TOTALS_METRICS = list(service.TOTALS_METRICS)  # eventCount, totalUsers, keyEvents


def _events(rows: list[tuple[str, str, str, str, str]]) -> dict:
    """(назва, eventCount, totalUsers, eventCountPerUser, keyEvents)."""
    return ga4_response(
        [([name], [count, users, per_user, key]) for name, count, users, per_user, key in rows],
        dimensions=["eventName"],
        metrics=_EVENT_METRICS,
    )


def _totals(count: str, users: str, key: str = "0") -> dict:
    return ga4_response([([], [count, users, key])], dimensions=[], metrics=_TOTALS_METRICS)


def _seed(
    fake: FakeGa4Session,
    month: str,
    segment: str,
    rows: list[tuple[str, str, str, str, str]],
    totals: tuple[str, str, str],
) -> None:
    fake.route(month, ("eventName",), _events(rows), segment=segment)
    fake.route(month, (), _totals(*totals), segment=segment)


def _run(fake: FakeGa4Session, months: list[str], *, now: datetime | None = None):
    return service.run_events_report(
        property_id="properties/1",
        months=months,
        session_factory=lambda: fake,
        now=now or datetime(2026, 9, 17, tzinfo=UTC),
    )


def test_every_event_carries_all_three_comparisons() -> None:
    fake = FakeGa4Session()
    _seed(
        fake, "2026-07", "all",
        [("Book Now", "704", "437", "1.6109", "704")], ("27772", "3953", "849"),
    )
    _seed(
        fake, "2026-07", "direct",
        [("Book Now", "74", "57", "1.2982", "74")], ("12088", "1610", "105"),
    )
    _seed(
        fake, "2026-07", "organic",
        [("Book Now", "70", "54", "1.2962", "70")], ("6042", "700", "111"),
    )

    month = _run(fake, ["2026-07"]).months[0]
    row = month.rows[0]

    assert sorted(row.segments) == ["all", "direct", "organic"]
    assert row.segments["all"].event_count == 704
    assert row.segments["direct"].event_count == 74
    assert row.segments["organic"].event_count == 70
    assert row.is_key_event is True


def test_shares_are_measured_against_each_segments_own_totals() -> None:
    """Живе вимірювання (SDS, липень 2026) — рядок у рядок як в аркуші
    оператора. Organic: 1.16%/7.71% від тоталів Organic-порізу; узяти
    загальні тотали або EXACT-фільтр дало б 1.22%/8.11%."""
    fake = FakeGa4Session()
    _seed(
        fake, "2026-07", "all",
        [("Book Now", "704", "437", "1.6109", "704")], ("27772", "3953", "849"),
    )
    _seed(
        fake, "2026-07", "direct",
        [("Book Now", "74", "57", "1.2982", "74")], ("12088", "1610", "105"),
    )
    _seed(
        fake, "2026-07", "organic",
        [("Book Now", "70", "54", "1.2962", "70")], ("6042", "700", "111"),
    )

    row = _run(fake, ["2026-07"]).months[0].rows[0]

    assert str(row.segments["all"].event_share_pct) == "2.53"
    assert str(row.segments["all"].user_share_pct) == "11.05"
    assert str(row.segments["direct"].event_share_pct) == "0.61"
    assert str(row.segments["direct"].user_share_pct) == "3.54"
    assert str(row.segments["organic"].event_share_pct) == "1.16"
    assert str(row.segments["organic"].user_share_pct) == "7.71"


def test_per_user_comes_from_ga4_not_from_our_own_division() -> None:
    fake = FakeGa4Session()
    _seed(fake, "2026-07", "all", [("Phone", "82", "61", "1.3442", "82")], ("27772", "3953", "849"))

    row = _run(fake, ["2026-07"]).months[0].rows[0]

    assert str(row.segments["all"].per_user) == "1.34"


def test_users_total_is_not_the_column_sum() -> None:
    """SAPHNY 2026-08: сума Users по івентах 227 237 проти property-тоталу
    58 556. Тотал мусить приходити окремим запитом."""
    fake = FakeGa4Session()
    _seed(
        fake,
        "2026-08",
        "all",
        [
            ("page_view", "100", "80", "1.25", "0"),
            ("session_start", "90", "78", "1.15", "0"),
            ("Phone", "10", "9", "1.11", "10"),
        ],
        ("200", "82", "10"),
    )

    month = _run(fake, ["2026-08"]).months[0]

    assert sum(row.segments["all"].users or 0 for row in month.rows) == 167
    assert month.totals["all"].users == 82
    assert month.totals["all"].event_count == 200


def test_channel_filter_uses_contains_not_exact() -> None:
    """Форма фільтра — частина контракту: `EXACT "Organic Search"` губить
    Organic Social/Video у знаменнику й тихо змінює всі відсотки."""
    fake = FakeGa4Session()
    _seed(fake, "2026-07", "all", [("Phone", "10", "9", "1.1", "10")], ("100", "50", "10"))
    _run(fake, ["2026-07"])

    filters = [call["body"].get("dimensionFilter") for call in fake.calls]
    applied = [f["filter"] for f in filters if f is not None]

    assert any(f is None for f in filters), "the All Users comparison is unfiltered"
    assert {f["stringFilter"]["value"] for f in applied} == {"Direct", "Organic"}
    assert {f["stringFilter"]["matchType"] for f in applied} == {"CONTAINS"}
    assert {f["fieldName"] for f in applied} == {"sessionDefaultChannelGroup"}


def test_event_missing_from_a_comparison_has_no_row_for_it_not_a_zero() -> None:
    fake = FakeGa4Session()
    _seed(fake, "2026-07", "all", [("Newsletter", "5", "5", "1.0", "5")], ("100", "50", "5"))
    _seed(fake, "2026-07", "direct", [], ("40", "20", "0"))

    row = _run(fake, ["2026-07"]).months[0].rows[0]

    assert "all" in row.segments
    assert "direct" not in row.segments  # GA4 не повернув рядка — це не 0


def test_rows_are_sorted_by_all_users_event_count() -> None:
    fake = FakeGa4Session()
    _seed(
        fake,
        "2026-07",
        "all",
        [("Phone", "120", "90", "1.33", "120"), ("page_view", "9712", "6084", "1.6", "0")],
        ("10000", "6100", "120"),
    )

    month = _run(fake, ["2026-07"]).months[0]

    assert [row.event_name for row in month.rows] == ["page_view", "Phone"]


def test_a_failed_comparison_fails_its_month_and_spares_the_neighbours() -> None:
    fake = FakeGa4Session()
    fake.route(
        "2026-08",
        ("eventName",),
        GoogleApiError("ga4 runReport failed: http_500"),
        segment="all",
    )
    _seed(fake, "2026-07", "all", [("Phone", "10", "9", "1.1", "10")], ("100", "50", "10"))

    result = _run(fake, ["2026-07", "2026-08"])

    assert result.state == "partial"
    assert result.months[0].month == "2026-08"
    assert result.months[0].state == "failed"
    assert result.months[0].error == "ga4 runReport failed: http_500"
    assert result.months[1].state == "ok"
    assert result.months[1].rows[0].event_name == "Phone"


def test_month_without_rows_is_no_data_not_zero() -> None:
    fake = FakeGa4Session()
    month = _run(fake, ["2026-08"]).months[0]

    assert month.state == "no_data"
    assert month.rows == []


def test_current_and_just_closed_months_are_partial() -> None:
    now = datetime(2026, 9, 17, tzinfo=UTC)
    assert service.is_partial_month("2026-09", now=now) is True
    assert service.is_partial_month("2026-08", now=now) is False
    # Місяць, що закрився менш ніж 48 год тому, — ще не остаточний.
    assert service.is_partial_month("2026-08", now=datetime(2026, 9, 1, 12, tzinfo=UTC)) is True


def test_months_are_validated_deduped_and_capped() -> None:
    assert service.normalize_months(["2026-07", "2026-08", "2026-07"]) == [
        "2026-08",
        "2026-07",
    ]
    with pytest.raises(ValueError):
        service.normalize_months(["2026-13"])
    with pytest.raises(ValueError):
        service.normalize_months([])
    too_many = (
        [f"2020-{index:02d}" for index in range(1, 13)]
        + [f"2021-{index:02d}" for index in range(1, 13)]
        + ["2022-01"]
    )
    assert len(too_many) == service.MAX_MONTHS + 1
    with pytest.raises(ValueError):
        service.normalize_months(too_many)


def test_six_requests_per_month_three_comparisons_times_rows_and_totals() -> None:
    """Форма запитів — частина контракту: по-місячно й по-порізно, бо холодний
    24-місячний запит заміряно у 46 с проти 60-секундного таймауту (план §1.5)."""
    fake = FakeGa4Session()
    for month in ("2026-06", "2026-07"):
        _seed(fake, month, "all", [("Phone", "10", "9", "1.1", "10")], ("100", "50", "10"))

    _run(fake, ["2026-06", "2026-07"])

    ranges = {
        (call["body"]["dateRanges"][0]["startDate"], call["body"]["dateRanges"][0]["endDate"])
        for call in fake.calls
    }
    assert ranges == {("2026-06-01", "2026-06-30"), ("2026-07-01", "2026-07-31")}
    assert len(fake.calls) == 2 * 3 * 2  # 2 місяці × 3 порізи × (рядки + тотали)


def test_available_months_come_from_one_yearmonth_request() -> None:
    fake = FakeGa4Session()
    fake.route(
        "2023-09",
        ("yearMonth",),
        ga4_response(
            [(["202608"], ["1"]), (["202412"], ["2"]), (["202607"], ["3"])],
            dimensions=["yearMonth"],
            metrics=["eventCount"],
        ),
    )
    months = service.available_months(
        property_id="properties/1",
        session_factory=lambda: fake,
        now=datetime(2026, 9, 17, tzinfo=UTC),
    )

    assert months == ["2026-08", "2026-07", "2024-12"]
    assert len(fake.calls) == 1
