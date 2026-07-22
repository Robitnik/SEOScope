"""Tests for `GscAdapter.collect_dimensions` (task-16, PLAN §21.4).

`seo_audit.dashboard.gsc.collect_gsc_dimensions` is monkeypatched throughout — these
tests cover only this module's own mapping logic (rank/quality/dimension tagging), not
the AISeoTools wrapper itself (covered by AISeoTools' own `tests/dashboard/test_gsc.py`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from seo_audit.dashboard.gsc import DashboardDimensionResult, DimensionPeriodResult, DimensionValue
from seo_audit.dashboard.models import DateRange as DashDateRange
from seo_audit.dashboard.models import PeriodRole as DashPeriodRole
from seo_audit.dashboard.models import Provider as DashProvider
from seo_audit.dashboard.models import Quality as DashQuality

from app.integrations import gsc as adapter_module
from app.schemas.adapter import CollectionContext, DateRange
from app.schemas.enums import ComparisonMode, PeriodKind, PeriodRole, Provider, Quality, SourceMode

CURRENT = DateRange(start=date(2026, 6, 1), end=date(2026, 6, 30))
PREVIOUS = DateRange(start=date(2026, 5, 1), end=date(2026, 5, 31))
DASH_CURRENT = DashDateRange(start=CURRENT.start, end=CURRENT.end)
DASH_PREVIOUS = DashDateRange(start=PREVIOUS.start, end=PREVIOUS.end)


def _context(**overrides) -> CollectionContext:
    defaults = dict(
        client_id="client-1",
        provider=Provider.gsc,
        source_mode=SourceMode.browser,
        period_kind=PeriodKind.month,
        comparison_mode=ComparisonMode.closed,
        current=CURRENT,
        previous=PREVIOUS,
        timezone="America/New_York",
        integration_config={"property": "sc-domain:example.com", "profile": "gsc"},
    )
    defaults.update(overrides)
    return CollectionContext(**defaults)


def _period(role, date_range, rows, *, truncated=False, total=None) -> DimensionPeriodResult:
    return DimensionPeriodResult(
        period_role=role,
        period_start=date_range.start,
        period_end=date_range.end,
        rows=rows,
        truncated=truncated,
        total_rows_available=total if total is not None else len(rows),
    )


def _dim_result(
    *, pages=None, queries=None, quality=DashQuality.complete, error_code=None, warnings=None
) -> DashboardDimensionResult:
    return DashboardDimensionResult(
        provider=DashProvider.gsc,
        quality=quality,
        observed_at=datetime.now(UTC),
        data_through=date(2026, 6, 28),
        pages=pages or [],
        queries=queries or [],
        warnings=warnings or [],
        error_code=error_code,
        provenance={"source_mode": "browser", "transport": "gsc_dimensions", "profile": "gsc"},
    )


def _patch(monkeypatch, result):
    from seo_audit.dashboard import gsc as real_wrapper

    monkeypatch.setattr(real_wrapper, "collect_gsc_dimensions", lambda *a, **k: result)


# ── no `property` configured ─────────────────────────────────────────────────
def test_collect_dimensions_without_property_is_not_configured():
    adapter = adapter_module.GscAdapter()
    context = _context(integration_config={})

    result = adapter.collect_dimensions(context, lambda e: None)

    assert result.quality == Quality.unavailable
    assert result.error_code == "not_configured"
    assert result.rows == []


# ── happy path: pages + queries mapped, tagged by dimension ─────────────────
def test_collect_dimensions_maps_pages_and_queries(monkeypatch):
    page_row = DimensionValue(
        dimension_value="/a",
        clicks=10,
        impressions=100,
        ctr=Decimal("0.1"),
        position=Decimal("4.0"),
    )
    query_row = DimensionValue(
        dimension_value="shoes",
        clicks=5,
        impressions=50,
        ctr=Decimal("0.1"),
        position=Decimal("3.0"),
    )
    result = _dim_result(
        pages=[_period(DashPeriodRole.current, DASH_CURRENT, [page_row])],
        queries=[_period(DashPeriodRole.current, DASH_CURRENT, [query_row])],
    )
    _patch(monkeypatch, result)
    adapter = adapter_module.GscAdapter()

    out = adapter.collect_dimensions(_context(), lambda e: None)

    by_dim = {(r.dimension, r.dimension_value): r for r in out.rows}
    assert by_dim[("page", "/a")].clicks == 10
    assert by_dim[("page", "/a")].rank == 1
    assert by_dim[("page", "/a")].quality == Quality.complete
    assert by_dim[("query", "shoes")].period_role == PeriodRole.current
    assert out.quality == Quality.complete


# ── truncated period: every row (incl. remainder) flagged, remainder rank None ──
def test_collect_dimensions_marks_truncated_period_rows(monkeypatch):
    kept = DimensionValue(
        dimension_value="/a",
        clicks=100,
        impressions=1000,
        ctr=Decimal("0.1"),
        position=Decimal("2.0"),
    )
    other = DimensionValue(
        dimension_value="інше",
        clicks=5,
        impressions=50,
        ctr=Decimal("0.1"),
        position=Decimal("9.0"),
    )
    result = _dim_result(
        pages=[
            _period(DashPeriodRole.current, DASH_CURRENT, [kept, other], truncated=True, total=2000)
        ],
    )
    _patch(monkeypatch, result)
    adapter = adapter_module.GscAdapter()

    out = adapter.collect_dimensions(_context(), lambda e: None)

    assert all(r.quality == Quality.truncated for r in out.rows)
    other_row = next(r for r in out.rows if r.dimension_value == "інше")
    assert other_row.rank is None
    kept_row = next(r for r in out.rows if r.dimension_value == "/a")
    assert kept_row.rank == 1


# ── not-truncated period keeps rows `complete`, ranked in order ──────────────
def test_collect_dimensions_untruncated_period_ranks_rows_in_order(monkeypatch):
    rows = [
        DimensionValue(dimension_value=f"/p{i}", clicks=10 - i, impressions=100 - i)
        for i in range(3)
    ]
    result = _dim_result(pages=[_period(DashPeriodRole.current, DASH_CURRENT, rows)])
    _patch(monkeypatch, result)
    adapter = adapter_module.GscAdapter()

    out = adapter.collect_dimensions(_context(), lambda e: None)

    assert [r.rank for r in out.rows] == [1, 2, 3]
    assert all(r.quality == Quality.complete for r in out.rows)


# ── adapter-level guard ──────────────────────────────────────────────────────
def test_collect_dimensions_unexpected_exception_never_raises(monkeypatch):
    from seo_audit.dashboard import gsc as real_wrapper

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(real_wrapper, "collect_gsc_dimensions", _boom)
    adapter = adapter_module.GscAdapter()

    out = adapter.collect_dimensions(_context(), lambda e: None)

    assert out.quality == Quality.unavailable
    assert out.error_code == "adapter_error"
    assert out.rows == []
