"""DimensionRepository (`gsc_dimension_metrics`, task-16): top-N/truncated
semantics, immutability, unique constraint, `None` != `0`, retention."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.base import utcnow
from app.db.models import Client, DimensionKind, SyncJob
from app.db.repositories.dimensions import (
    MIN_DIMENSION_RETENTION_DAYS,
    DimensionRepository,
    DimensionRowInput,
)
from app.db.repositories.snapshots import SnapshotRepository
from app.schemas.enums import ComparisonMode, PeriodKind, Provider, Quality, SourceMode

CURRENT_START = date(2026, 1, 5)
CURRENT_END = date(2026, 1, 11)


def _make_snapshot(session, *, client=None):
    client = client or Client(name="Acme", slug="acme", domain="acme.com")
    session.add(client)
    session.commit()
    job = SyncJob(
        client_id=client.id,
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 12),
    )
    session.add(job)
    session.commit()
    snapshot = SnapshotRepository(session).create_immutable(
        job_id=job.id,
        client_id=client.id,
        provider=Provider.gsc,
        source_mode=SourceMode.browser,
        observed_at=utcnow(),
        quality=Quality.complete,
    )
    return client, snapshot


def _row(
    value: str, *, clicks=10, impressions=100, ctr="0.1", position="4.0", rank=1
) -> DimensionRowInput:
    return DimensionRowInput(
        dimension_value=value,
        clicks=clicks,
        impressions=impressions,
        ctr=Decimal(ctr) if ctr is not None else None,
        position=Decimal(position) if position is not None else None,
        quality=Quality.complete,
        rank=rank,
    )


def test_bulk_insert_and_for_period_round_trip(session):
    client, snapshot = _make_snapshot(session)
    repo = DimensionRepository(session)
    repo.bulk_insert(
        snapshot_id=snapshot.id,
        client_id=client.id,
        dimension=DimensionKind.page,
        period_start=CURRENT_START,
        period_end=CURRENT_END,
        rows=[_row("/a", clicks=50), _row("/b", clicks=10, rank=2)],
    )
    session.commit()

    rows, total = repo.for_period(client.id, DimensionKind.page, CURRENT_START, CURRENT_END)
    assert total == 2
    assert [r.dimension_value for r in rows] == ["/a", "/b"]  # sorted by clicks desc


def test_top_n_cut_plus_remainder_row_is_flagged_truncated(session):
    client, snapshot = _make_snapshot(session)
    repo = DimensionRepository(session)
    kept = [_row(f"/page-{i}", clicks=100 - i, impressions=1000 - i, rank=i + 1) for i in range(3)]
    remainder = _row("інше", clicks=5, impressions=50, ctr="0.1", position="9.0", rank=None)
    rows = [
        DimensionRowInput(
            dimension_value=r.dimension_value,
            clicks=r.clicks,
            impressions=r.impressions,
            ctr=r.ctr,
            position=r.position,
            quality=Quality.truncated,  # the whole cut period is flagged, not just the remainder
            rank=r.rank,
        )
        for r in (*kept, remainder)
    ]
    repo.bulk_insert(
        snapshot_id=snapshot.id,
        client_id=client.id,
        dimension=DimensionKind.page,
        period_start=CURRENT_START,
        period_end=CURRENT_END,
        rows=rows,
    )
    session.commit()

    stored, total = repo.for_period(client.id, DimensionKind.page, CURRENT_START, CURRENT_END)
    assert total == 4  # 3 kept + 1 remainder row — exactly N + 1, never silently dropped
    assert all(r.quality == Quality.truncated for r in stored)
    assert stored[-1].dimension_value == "інше"
    assert stored[-1].rank is None


def test_none_is_never_coerced_to_zero(session):
    client, snapshot = _make_snapshot(session)
    repo = DimensionRepository(session)
    repo.bulk_insert(
        snapshot_id=snapshot.id,
        client_id=client.id,
        dimension=DimensionKind.query,
        period_start=CURRENT_START,
        period_end=CURRENT_END,
        rows=[_row("shoes", clicks=None, impressions=None, ctr=None, position=None)],
    )
    session.commit()

    rows, _ = repo.for_period(client.id, DimensionKind.query, CURRENT_START, CURRENT_END)
    assert rows[0].clicks is None
    assert rows[0].impressions is None
    assert rows[0].ctr is None
    assert rows[0].position is None


def test_ctr_and_position_keep_decimal_precision(session):
    client, snapshot = _make_snapshot(session)
    repo = DimensionRepository(session)
    repo.bulk_insert(
        snapshot_id=snapshot.id,
        client_id=client.id,
        dimension=DimensionKind.page,
        period_start=CURRENT_START,
        period_end=CURRENT_END,
        rows=[_row("/a", ctr="0.03421", position="4.678")],
    )
    session.commit()

    rows, _ = repo.for_period(client.id, DimensionKind.page, CURRENT_START, CURRENT_END)
    assert rows[0].ctr == Decimal("0.03421")
    assert rows[0].position == Decimal("4.678")


def test_unique_constraint_rejects_duplicate_dimension_value_in_one_snapshot(session):
    client, snapshot = _make_snapshot(session)
    repo = DimensionRepository(session)
    repo.bulk_insert(
        snapshot_id=snapshot.id,
        client_id=client.id,
        dimension=DimensionKind.page,
        period_start=CURRENT_START,
        period_end=CURRENT_END,
        rows=[_row("/a")],
    )
    session.commit()

    with pytest.raises(IntegrityError):
        repo.bulk_insert(
            snapshot_id=snapshot.id,
            client_id=client.id,
            dimension=DimensionKind.page,
            period_start=CURRENT_START,
            period_end=CURRENT_END,
            rows=[_row("/a")],
        )
        session.commit()
    session.rollback()


def test_rows_are_immutable_after_commit(session):
    client, snapshot = _make_snapshot(session)
    repo = DimensionRepository(session)
    rows = repo.bulk_insert(
        snapshot_id=snapshot.id,
        client_id=client.id,
        dimension=DimensionKind.page,
        period_start=CURRENT_START,
        period_end=CURRENT_END,
        rows=[_row("/a")],
    )
    session.commit()

    rows[0].clicks = 999
    with pytest.raises(ValueError, match="immutable"):
        session.commit()
    session.rollback()


def test_for_period_returns_empty_when_no_exact_period_match(session):
    client, snapshot = _make_snapshot(session)
    repo = DimensionRepository(session)
    repo.bulk_insert(
        snapshot_id=snapshot.id,
        client_id=client.id,
        dimension=DimensionKind.page,
        period_start=CURRENT_START,
        period_end=CURRENT_END,
        rows=[_row("/a")],
    )
    session.commit()

    rows, total = repo.for_period(
        client.id, DimensionKind.page, CURRENT_START, CURRENT_END + timedelta(days=1)
    )
    assert rows == []
    assert total == 0


def test_top_pages_and_top_queries_scope_by_dimension(session):
    client, snapshot = _make_snapshot(session)
    repo = DimensionRepository(session)
    repo.bulk_insert(
        snapshot_id=snapshot.id,
        client_id=client.id,
        dimension=DimensionKind.page,
        period_start=CURRENT_START,
        period_end=CURRENT_END,
        rows=[_row("/a")],
    )
    repo.bulk_insert(
        snapshot_id=snapshot.id,
        client_id=client.id,
        dimension=DimensionKind.query,
        period_start=CURRENT_START,
        period_end=CURRENT_END,
        rows=[_row("shoes")],
    )
    session.commit()

    pages, _ = repo.top_pages(client.id, CURRENT_START, CURRENT_END)
    queries, _ = repo.top_queries(client.id, CURRENT_START, CURRENT_END)
    assert [r.dimension_value for r in pages] == ["/a"]
    assert [r.dimension_value for r in queries] == ["shoes"]


def test_for_two_periods_resolves_each_side_independently(session):
    # Each period gets its own `ProviderSnapshot` — the unique constraint is
    # `(snapshot_id, dimension, dimension_value)`, with no `period_role`, so
    # one snapshot holds exactly one period's worth of a given URL/query
    # (unlike `metric_values`, which packs current+previous into one snapshot).
    client, current_snapshot = _make_snapshot(session)
    _, previous_snapshot = _make_snapshot(session, client=client)
    repo = DimensionRepository(session)
    previous_start = CURRENT_START - timedelta(days=7)
    previous_end = CURRENT_START - timedelta(days=1)
    repo.bulk_insert(
        snapshot_id=current_snapshot.id,
        client_id=client.id,
        dimension=DimensionKind.page,
        period_start=CURRENT_START,
        period_end=CURRENT_END,
        rows=[_row("/a", clicks=20)],
    )
    repo.bulk_insert(
        snapshot_id=previous_snapshot.id,
        client_id=client.id,
        dimension=DimensionKind.page,
        period_start=previous_start,
        period_end=previous_end,
        rows=[_row("/a", clicks=10)],
    )
    session.commit()

    current_rows, previous_rows = repo.for_two_periods(
        client.id,
        DimensionKind.page,
        current=(CURRENT_START, CURRENT_END),
        previous=(previous_start, previous_end),
    )
    assert current_rows[0].clicks == 20
    assert previous_rows[0].clicks == 10


def test_purge_older_than_deletes_only_stale_periods(session):
    client, snapshot = _make_snapshot(session)
    repo = DimensionRepository(session)
    old_end = date.today() - timedelta(days=30)
    old_start = old_end - timedelta(days=6)
    repo.bulk_insert(
        snapshot_id=snapshot.id,
        client_id=client.id,
        dimension=DimensionKind.page,
        period_start=old_start,
        period_end=old_end,
        rows=[_row("/old")],
    )
    recent_end = date.today()
    recent_start = recent_end - timedelta(days=6)
    repo.bulk_insert(
        snapshot_id=snapshot.id,
        client_id=client.id,
        dimension=DimensionKind.page,
        period_start=recent_start,
        period_end=recent_end,
        rows=[_row("/new")],
    )
    session.commit()

    deleted = repo.purge_older_than(MIN_DIMENSION_RETENTION_DAYS)
    session.commit()

    assert deleted == 1
    remaining, _ = repo.for_period(client.id, DimensionKind.page, recent_start, recent_end)
    assert [r.dimension_value for r in remaining] == ["/new"]


def test_purge_older_than_refuses_below_safe_minimum(session):
    with pytest.raises(ValueError):
        DimensionRepository(session).purge_older_than(MIN_DIMENSION_RETENTION_DAYS - 1)
