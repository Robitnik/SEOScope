from __future__ import annotations

from datetime import date

from app.reports.models import PeriodMetrics
from app.reports.periods import ReportPeriod
from app.reports.verify import cross_check

_PERIOD = [ReportPeriod("This Month", date(2026, 7, 1), date(2026, 7, 31))]


def test_identical_tables_are_a_full_match():
    metrics = {"This Month": PeriodMetrics(users=6251, clicks=2528)}
    result = cross_check(_PERIOD, metrics, metrics)
    assert result.status == "match"
    # The other 6 metrics are `None` on both sides here — `no_data`, not
    # `match` (task-48 §2.3) — but neither counts as a disagreement.
    assert all(c.verdict in ("match", "no_data") for c in result.cells)
    assert result.differs == []


def test_small_drift_within_tolerance_does_not_count_as_mismatch():
    pdf = {"This Month": PeriodMetrics(users=1000)}
    api = {"This Month": PeriodMetrics(users=1004)}  # 0.4% drift
    result = cross_check(_PERIOD, pdf, api, tolerance_pct=0.5)
    assert result.status == "within_tolerance"
    assert result.mismatches == []
    assert result.differs == []


def test_large_drift_is_a_mismatch_with_a_readable_description():
    pdf = {"This Month": PeriodMetrics(organic_users=3061)}
    api = {"This Month": PeriodMetrics(organic_users=2914)}
    result = cross_check(_PERIOD, pdf, api, tolerance_pct=0.5)
    assert result.status == "mismatch"
    [bad] = result.mismatches
    assert bad.metric_key == "organic_users"
    assert "3061" in bad.describe()
    assert "2914" in bad.describe()
    assert result.differs == [bad]


# --- task-48 §2.3: the full verdict matrix -----------------------------------


def test_one_sided_none_is_missing_api_not_a_generic_mismatch():
    # PDF has a real number, the API has nothing for this cell: a genuine
    # gap, named by which side is missing (task-48 §2.3), not lumped into
    # the same bucket as a numeric disagreement.
    pdf = {"This Month": PeriodMetrics(clicks=500)}
    api = {"This Month": PeriodMetrics(clicks=None)}
    result = cross_check(_PERIOD, pdf, api)
    assert result.status == "mismatch"  # still moves the overall status — it IS a gap
    [cell] = [c for c in result.cells if c.metric_key == "clicks"]
    assert cell.verdict == "missing_api"
    assert cell in result.differs
    assert cell not in result.mismatches  # `.mismatches` stays narrowly "mismatch"


def test_one_sided_none_the_other_way_is_missing_pdf():
    pdf = {"This Month": PeriodMetrics(clicks=None)}
    api = {"This Month": PeriodMetrics(clicks=500)}
    result = cross_check(_PERIOD, pdf, api)
    [cell] = [c for c in result.cells if c.metric_key == "clicks"]
    assert cell.verdict == "missing_pdf"
    assert cell in result.differs


def test_both_none_is_no_data_not_a_false_match_or_mismatch():
    # task-48 §1.3: a period neither source has anything for (e.g. a client
    # with no GSC integration, or — the live case — a GA4 property that did
    # not exist yet in a YOY period) is neither "they agree" nor "they
    # disagree": there was nothing to compare.
    pdf = {"This Month": PeriodMetrics(clicks=None)}
    api = {"This Month": PeriodMetrics(clicks=None)}
    result = cross_check(_PERIOD, pdf, api)
    [cell] = [c for c in result.cells if c.metric_key == "clicks"]
    assert cell.verdict == "no_data"
    assert cell not in result.differs
    assert cell not in result.mismatches
    assert cell in result.no_data_cells
    # `no_data` never taints the overall status — nothing here disagrees.
    assert result.status == "match"


def test_missing_period_in_either_table_compares_as_all_no_data():
    result = cross_check(_PERIOD, {}, {})
    assert result.status == "match"
    assert all(c.verdict == "no_data" for c in result.cells)
    assert all(c.pdf_value is None and c.api_value is None for c in result.cells)


def test_no_data_cells_never_escalate_status_past_within_tolerance():
    # A run with one within-tolerance drift AND one no-data period must
    # report `within_tolerance`, not be dragged up to `mismatch` by the
    # no-data cell nor down to `match` by ignoring the real drift.
    periods = [
        ReportPeriod("This Month", date(2026, 7, 1), date(2026, 7, 31)),
        ReportPeriod("Month YOY", date(2025, 7, 1), date(2025, 7, 31)),
    ]
    pdf = {
        "This Month": PeriodMetrics(users=1000),
        "Month YOY": PeriodMetrics(),
    }
    api = {
        "This Month": PeriodMetrics(users=1004),
        "Month YOY": PeriodMetrics(),
    }
    result = cross_check(periods, pdf, api, tolerance_pct=0.5)
    assert result.status == "within_tolerance"


def test_summary_counts_match_the_task48_headline_shape():
    # The exact live shape from task-48 §1 (scaled down): most cells match,
    # a handful are genuinely no-data, none actually differ. `users`/`clicks`
    # are `None` on both sides (no data); every other metric agrees.
    periods = [ReportPeriod("Month YOY", date(2025, 7, 1), date(2025, 7, 31))]
    common = {
        "organic_users": 100, "direct_users": 50, "conversions": 20,
        "organic_conversions": 10, "direct_conversions": 5, "impressions": 1000,
    }
    pdf = {"Month YOY": PeriodMetrics(users=None, clicks=None, **common)}
    api = {"Month YOY": PeriodMetrics(users=None, clicks=None, **common)}
    result = cross_check(periods, pdf, api)
    match_count = len(result.cells) - len(result.no_data_cells) - len(result.differs)
    assert (match_count, len(result.no_data_cells), len(result.differs)) == (6, 2, 0)
