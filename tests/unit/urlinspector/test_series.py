"""`app.urlinspector.series.build_series` — the two invariants measured on
the real portfolio (task-61 §0.3/§0.4): last-snapshot-wins per period, and
never averaging across period lengths. Pure function, no session."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from app.schemas.enums import Quality
from app.urlinspector.series import PeriodRow, build_series

WEEK_START = date(2026, 7, 20)
WEEK_END = date(2026, 7, 26)
MONTH_START = date(2026, 7, 1)
MONTH_END = date(2026, 7, 30)  # 30 days -> "month" bucket


def _row(start, end, created_at, quality, value):
    return PeriodRow(
        period_start=start, period_end=end, created_at=created_at,
        quality=quality, value=value,
    )


class TestLastSnapshotWins:
    def test_latest_created_at_wins_when_period_recollected(self):
        # Real measured case (task-61 §0.3): same week, clicks went 396 -> 470
        # across two re-collections two days apart.
        older = _row(
            WEEK_START, WEEK_END, datetime(2026, 7, 27, 10, 12), Quality.complete, Decimal(396)
        )
        newer = _row(
            WEEK_START, WEEK_END, datetime(2026, 7, 29, 9, 3), Quality.complete, Decimal(470)
        )
        series = build_series(
            metric="clicks", unit="count", url_rows=[older, newer],
            period_existence=[older, newer], granularity="week",
        )
        assert len(series.points) == 1
        assert series.points[0].value == Decimal(470)

    def test_order_of_input_rows_does_not_matter(self):
        older = _row(WEEK_START, WEEK_END, datetime(2026, 7, 27), Quality.complete, Decimal(1))
        newer = _row(WEEK_START, WEEK_END, datetime(2026, 7, 29), Quality.complete, Decimal(2))
        forward = build_series(
            metric="clicks", unit="count", url_rows=[older, newer],
            period_existence=[newer], granularity="week",
        )
        backward = build_series(
            metric="clicks", unit="count", url_rows=[newer, older],
            period_existence=[newer], granularity="week",
        )
        assert forward.points[0].value == backward.points[0].value == Decimal(2)


class TestGranularityBuckets:
    def test_only_matching_length_periods_are_returned(self):
        week_row = _row(
            WEEK_START, WEEK_END, datetime(2026, 7, 27), Quality.complete, Decimal(10)
        )
        month_row = _row(
            MONTH_START, MONTH_END, datetime(2026, 7, 31), Quality.complete, Decimal(500)
        )
        series = build_series(
            metric="clicks", unit="count", url_rows=[week_row, month_row],
            period_existence=[week_row, month_row], granularity="week",
        )
        assert len(series.points) == 1
        assert series.points[0].period_days == 7
        assert series.other_length_periods == 1

    def test_no_period_ever_averaged_across_lengths(self):
        # A week (10) and a month (500) covering overlapping dates must never
        # be blended into one series — this was the exact failure mode that
        # would draw a fake spike (task-61 §0.4).
        week_row = _row(
            WEEK_START, WEEK_END, datetime(2026, 7, 27), Quality.complete, Decimal(10)
        )
        month_row = _row(
            MONTH_START, MONTH_END, datetime(2026, 7, 31), Quality.complete, Decimal(500)
        )
        month_series = build_series(
            metric="clicks", unit="count", url_rows=[week_row, month_row],
            period_existence=[week_row, month_row], granularity="month",
        )
        assert len(month_series.points) == 1
        assert month_series.points[0].value == Decimal(500)


class TestHonestAbsence:
    def test_truncated_period_absence_is_none_not_zero(self):
        # This URL has no row this week, but the period itself was truncated
        # (cut to top-N) — absence must stay `None`, never a guessed `0`.
        existence_only = _row(WEEK_START, WEEK_END, datetime(2026, 7, 27), Quality.truncated, None)
        series = build_series(
            metric="clicks", unit="count", url_rows=[],
            period_existence=[existence_only], granularity="week",
        )
        assert len(series.points) == 1
        point = series.points[0]
        assert point.value is None
        assert point.absence_reason == "truncated"

    def test_complete_period_absence_is_an_honest_zero(self):
        # The provider returned every nonzero row for a `complete` period and
        # this URL wasn't one of them -> a real 0, not an unknown.
        existence_only = _row(WEEK_START, WEEK_END, datetime(2026, 7, 27), Quality.complete, None)
        series = build_series(
            metric="clicks", unit="count", url_rows=[],
            period_existence=[existence_only], granularity="week",
        )
        assert len(series.points) == 1
        point = series.points[0]
        assert point.value == Decimal(0)
        assert point.absence_reason is None

    def test_period_this_client_never_collected_is_absent_from_chart(self):
        # No existence row for this period at all (client's own dataset never
        # touched it) -> the period simply doesn't appear, not a fabricated point.
        series = build_series(
            metric="clicks", unit="count", url_rows=[], period_existence=[], granularity="week"
        )
        assert series.points == []


class TestOrdering:
    def test_points_are_sorted_by_period_start(self):
        later = _row(
            WEEK_START + timedelta(days=7), WEEK_END + timedelta(days=7),
            datetime(2026, 8, 3), Quality.complete, Decimal(1),
        )
        earlier = _row(WEEK_START, WEEK_END, datetime(2026, 7, 27), Quality.complete, Decimal(2))
        series = build_series(
            metric="clicks", unit="count", url_rows=[later, earlier],
            period_existence=[later, earlier], granularity="week",
        )
        assert [p.period_start for p in series.points] == sorted(
            p.period_start for p in series.points
        )
