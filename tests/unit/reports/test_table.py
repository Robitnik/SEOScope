from __future__ import annotations

from datetime import date

import pytest

from app.reports.models import PeriodMetrics
from app.reports.periods import ReportPeriod
from app.reports.table import build_table, safe_pct


@pytest.mark.parametrize(
    "current,previous,expected",
    [
        (110, 100, pytest.approx(0.10)),
        (90, 100, pytest.approx(-0.10)),
        (None, 100, None),
        (110, None, None),
        (110, 0, None),
        (0, 0, None),
    ],
)
def test_safe_pct(current, previous, expected):
    assert safe_pct(current, previous) == expected


def test_monthly_table_computes_the_four_fixed_change_columns():
    periods = [
        ReportPeriod("This Month", date(2026, 5, 1), date(2026, 5, 31)),
        ReportPeriod("Last Month", date(2026, 4, 1), date(2026, 4, 30)),
        ReportPeriod("Month YOY", date(2025, 5, 1), date(2025, 5, 31)),
        ReportPeriod("This Quarter", date(2026, 3, 1), date(2026, 5, 31)),
        ReportPeriod("Last Quarter", date(2025, 12, 1), date(2026, 2, 28)),
        ReportPeriod("Quarterly YOY", date(2025, 3, 1), date(2025, 5, 31)),
    ]
    metrics = {
        "This Month": PeriodMetrics(users=110, clicks=None),
        "Last Month": PeriodMetrics(users=100, clicks=None),
        "Month YOY": PeriodMetrics(users=50, clicks=None),
        "This Quarter": PeriodMetrics(users=330, clicks=None),
        "Last Quarter": PeriodMetrics(users=300, clicks=None),
        "Quarterly YOY": PeriodMetrics(users=150, clicks=None),
    }
    table = build_table("monthly", periods, metrics)

    assert table.change_labels == ["% M/M", "% Q/Q", "% M YOY", "% Q YOY"]
    users_row = next(r for r in table.rows if r.key == "users")
    assert users_row.values == [110, 100, 50, 330, 300, 150]
    assert users_row.changes == [
        pytest.approx(0.10),  # M/M: 110 vs 100
        pytest.approx(0.10),  # Q/Q: 330 vs 300
        pytest.approx(1.2),  # M YOY: 110 vs 50
        pytest.approx(1.2),  # Q YOY: 330 vs 150
    ]

    clicks_row = next(r for r in table.rows if r.key == "clicks")
    assert clicks_row.values == [None] * 6
    assert clicks_row.changes == [None] * 4


def test_non_monthly_table_uses_n_minus_one_adjacent_changes():
    periods = [
        ReportPeriod("July 2026", date(2026, 7, 1), date(2026, 7, 31)),
        ReportPeriod("July 2025", date(2025, 7, 1), date(2025, 7, 31)),
        ReportPeriod("July 2024", date(2024, 7, 1), date(2024, 7, 31)),
    ]
    metrics = {
        "July 2026": PeriodMetrics(users=300),
        "July 2025": PeriodMetrics(users=200),
        "July 2024": PeriodMetrics(users=100),
    }
    table = build_table("comparator", periods, metrics)

    assert table.change_labels == ["July 2026 vs July 2025", "July 2025 vs July 2024"]
    users_row = next(r for r in table.rows if r.key == "users")
    assert users_row.changes == [pytest.approx(0.5), pytest.approx(1.0)]


def test_missing_period_metrics_render_as_all_none_not_zero():
    periods = [ReportPeriod("July 2026", date(2026, 7, 1), date(2026, 7, 31))]
    table = build_table("custom_months", periods, {})
    users_row = next(r for r in table.rows if r.key == "users")
    assert users_row.values == [None]


def test_sources_are_carried_through_untouched():
    periods = [ReportPeriod("July 2026", date(2026, 7, 1), date(2026, 7, 31))]
    table = build_table(
        "custom_months",
        periods,
        {"July 2026": PeriodMetrics(users=1)},
        sources={"July 2026": "EFFYS__july_2026.pdf"},
    )
    assert table.sources == {"July 2026": "EFFYS__july_2026.pdf"}
