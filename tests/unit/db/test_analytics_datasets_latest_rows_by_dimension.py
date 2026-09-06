"""`AnalyticsDatasetRepository.latest_rows_by_dimension` (task-68 §2): dedup
moved from Python (a full unbounded scan + `setdefault`) into SQL via
`ROW_NUMBER() OVER (PARTITION BY dimension_value ORDER BY created_at DESC)`,
plus an optional `since`/`until` window (mirroring `daily_series`, task-28).
Live-verified byte-for-byte identical to the old Python dedup across all 15
production clients before this replaced it; these tests pin the same
contract on a synthetic dataset so a future change can't silently drift.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.db.base import utcnow
from app.db.models import Client, ProviderSnapshot, SyncJob
from app.db.repositories.analytics_datasets import (
    AnalyticsDatasetRepository,
    AnalyticsDatasetRowInput,
)
from app.schemas.enums import ComparisonMode, PeriodKind, Provider, Quality, SourceMode


def _client(session, *, slug="acme") -> Client:
    client = Client(name=slug, slug=slug, domain=f"{slug}.com")
    session.add(client)
    session.commit()
    return client


def _snapshot(session, client: Client, *, provider=Provider.gsc) -> ProviderSnapshot:
    job = SyncJob(
        client_id=client.id,
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 7, 1),
    )
    session.add(job)
    session.commit()
    snap = ProviderSnapshot(
        job_id=job.id,
        client_id=client.id,
        provider=provider,
        source_mode=SourceMode.api,
        observed_at=utcnow(),
        quality=Quality.complete,
    )
    session.add(snap)
    session.commit()
    return snap


def _insert_row(
    session,
    client: Client,
    snapshot: ProviderSnapshot,
    *,
    provider: Provider,
    dataset_key: str,
    dimension_value: str,
    metrics: dict,
    created_at=None,
):
    rows = AnalyticsDatasetRepository(session).bulk_insert(
        snapshot_id=snapshot.id,
        client_id=client.id,
        provider=provider,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        rows=[
            AnalyticsDatasetRowInput(
                dataset_key=dataset_key,
                dimension_key="date",
                dimension_value=dimension_value,
                metrics=metrics,
                quality=Quality.complete,
                rank=1,
            )
        ],
    )
    if created_at is not None:
        for row in rows:
            row.created_at = created_at
    session.commit()
    return rows[0]


def test_newer_row_wins_for_the_same_dimension_value(session):
    """The exact rule task-25's per-finding recheck depends on: many small
    runs write rows for the same `dimension_value` over time, and the
    newest `created_at` must win — never the newest snapshot as a whole,
    since a recheck touches only some dimension values, not all of them.
    """
    client = _client(session)
    older = _snapshot(session, client)
    newer = _snapshot(session, client)

    _insert_row(
        session, client, older,
        provider=Provider.gsc, dataset_key="gsc_daily", dimension_value="2026-07-10",
        metrics={"clicks": Decimal(100)}, created_at=utcnow() - timedelta(hours=2),
    )
    _insert_row(
        session, client, newer,
        provider=Provider.gsc, dataset_key="gsc_daily", dimension_value="2026-07-10",
        metrics={"clicks": Decimal(999)}, created_at=utcnow() - timedelta(hours=1),
    )

    result = AnalyticsDatasetRepository(session).latest_rows_by_dimension(
        client.id, Provider.gsc, "gsc_daily"
    )

    assert result["2026-07-10"].metrics_json["clicks"] == "999"


def test_recheck_style_partial_write_leaves_untouched_dimensions_alone(session):
    """task-25: a recheck job writes ONLY the dimension values it touched.
    A later, narrower run must not make an untouched dimension's row
    disappear or revert — each `dimension_value` is deduped independently.
    """
    client = _client(session)
    first_run = _snapshot(session, client)
    recheck_run = _snapshot(session, client)

    _insert_row(
        session, client, first_run,
        provider=Provider.gsc, dataset_key="gsc_daily", dimension_value="2026-07-10",
        metrics={"clicks": Decimal(10)}, created_at=utcnow() - timedelta(hours=2),
    )
    _insert_row(
        session, client, first_run,
        provider=Provider.gsc, dataset_key="gsc_daily", dimension_value="2026-07-11",
        metrics={"clicks": Decimal(20)}, created_at=utcnow() - timedelta(hours=2),
    )
    # Recheck touches ONLY 2026-07-11.
    _insert_row(
        session, client, recheck_run,
        provider=Provider.gsc, dataset_key="gsc_daily", dimension_value="2026-07-11",
        metrics={"clicks": Decimal(999)}, created_at=utcnow() - timedelta(hours=1),
    )

    result = AnalyticsDatasetRepository(session).latest_rows_by_dimension(
        client.id, Provider.gsc, "gsc_daily"
    )

    assert result["2026-07-10"].metrics_json["clicks"] == "10"  # untouched, unchanged
    assert result["2026-07-11"].metrics_json["clicks"] == "999"  # rechecked, updated


def test_window_excludes_dimension_values_outside_the_requested_range(session):
    client = _client(session)
    snapshot = _snapshot(session, client)
    for day in ("2026-06-01", "2026-07-15", "2026-08-01"):
        _insert_row(
            session, client, snapshot,
            provider=Provider.gsc, dataset_key="gsc_daily", dimension_value=day,
            metrics={"clicks": Decimal(1)},
        )

    result = AnalyticsDatasetRepository(session).latest_rows_by_dimension(
        client.id, Provider.gsc, "gsc_daily",
        since=date(2026, 7, 1), until=date(2026, 7, 31),
    )

    assert set(result) == {"2026-07-15"}


def test_windowed_result_matches_unwindowed_result_filtered_in_python(session):
    """Pins the exact equivalence this task's change relies on for being a
    pure performance narrowing, not a behavior change: passing `since`/
    `until` must return exactly the subset an unbounded call would have
    yielded after filtering client-side to the same range.
    """
    client = _client(session)
    older = _snapshot(session, client)
    newer = _snapshot(session, client)
    days = ["2026-06-20", "2026-07-05", "2026-07-05", "2026-07-20", "2026-08-10"]
    for i, day in enumerate(days):
        _insert_row(
            session, client, older if i % 2 == 0 else newer,
            provider=Provider.gsc, dataset_key="gsc_daily", dimension_value=day,
            metrics={"clicks": Decimal(i)},
            created_at=utcnow() - timedelta(hours=len(days) - i),
        )

    repo = AnalyticsDatasetRepository(session)
    since, until = date(2026, 7, 1), date(2026, 7, 31)

    windowed = repo.latest_rows_by_dimension(
        client.id, Provider.gsc, "gsc_daily", since=since, until=until
    )
    unwindowed = repo.latest_rows_by_dimension(client.id, Provider.gsc, "gsc_daily")
    expected = {
        k: v for k, v in unwindowed.items() if since.isoformat() <= k <= until.isoformat()
    }

    assert set(windowed) == set(expected)
    for key in expected:
        assert windowed[key].id == expected[key].id
        assert windowed[key].metrics_json == expected[key].metrics_json


def test_scoped_to_client_provider_and_dataset_key(session):
    """Regression guard: the SQL rewrite must keep filtering by all three —
    a subquery rewrite is exactly the kind of change that can accidentally
    widen a WHERE clause.
    """
    client_a = _client(session, slug="acme")
    client_b = _client(session, slug="beta")
    snap_a = _snapshot(session, client_a, provider=Provider.gsc)
    snap_b = _snapshot(session, client_b, provider=Provider.gsc)

    _insert_row(
        session, client_a, snap_a,
        provider=Provider.gsc, dataset_key="gsc_daily", dimension_value="2026-07-10",
        metrics={"clicks": Decimal(1)},
    )
    _insert_row(
        session, client_b, snap_b,
        provider=Provider.gsc, dataset_key="gsc_daily", dimension_value="2026-07-10",
        metrics={"clicks": Decimal(2)},
    )
    # Same client, different dataset_key — must not leak in either.
    _insert_row(
        session, client_a, snap_a,
        provider=Provider.gsc, dataset_key="gsc_weekly", dimension_value="2026-07-10",
        metrics={"clicks": Decimal(3)},
    )

    result = AnalyticsDatasetRepository(session).latest_rows_by_dimension(
        client_a.id, Provider.gsc, "gsc_daily"
    )

    assert list(result) == ["2026-07-10"]
    assert result["2026-07-10"].metrics_json["clicks"] == "1"


def test_empty_dataset_returns_empty_dict(session):
    client = _client(session)
    result = AnalyticsDatasetRepository(session).latest_rows_by_dimension(
        client.id, Provider.gsc, "gsc_daily"
    )
    assert result == {}
