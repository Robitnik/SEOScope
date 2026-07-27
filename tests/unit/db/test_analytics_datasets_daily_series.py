"""`AnalyticsDatasetRepository.daily_series` (task-28): newest row per
(client, day) across ALL snapshots, clipped to an arbitrary window, for
many clients at once — the read the Portfolio Trends page needs and neither
`for_period` (exact period match) nor `latest_rows_by_dimension`
(single-client) can serve."""

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


def _insert_day(
    session, client: Client, snapshot: ProviderSnapshot, day: str, clicks: int, *, created_at=None
):
    rows = AnalyticsDatasetRepository(session).bulk_insert(
        snapshot_id=snapshot.id,
        client_id=client.id,
        provider=Provider.gsc,
        period_start=date.fromisoformat(day),
        period_end=date.fromisoformat(day),
        rows=[
            AnalyticsDatasetRowInput(
                dataset_key="gsc_daily",
                dimension_key="date",
                dimension_value=day,
                metrics={"clicks": Decimal(clicks)},
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


def test_newer_snapshot_wins_for_the_same_day(session):
    client = _client(session)
    older_snapshot = _snapshot(session, client)
    newer_snapshot = _snapshot(session, client)

    _insert_day(
        session, client, older_snapshot, "2026-07-10", 100,
        created_at=utcnow() - timedelta(hours=2),
    )
    _insert_day(
        session, client, newer_snapshot, "2026-07-10", 999,
        created_at=utcnow() - timedelta(hours=1),
    )

    result = AnalyticsDatasetRepository(session).daily_series(
        [client.id], Provider.gsc, "gsc_daily", date(2026, 7, 1), date(2026, 7, 31)
    )

    row = result[client.id][date(2026, 7, 10)]
    assert row.metrics_json["clicks"] == "999"


def test_window_excludes_days_outside_the_requested_range(session):
    client = _client(session)
    snapshot = _snapshot(session, client)
    _insert_day(session, client, snapshot, "2026-06-30", 1)
    _insert_day(session, client, snapshot, "2026-07-15", 2)
    _insert_day(session, client, snapshot, "2026-08-01", 3)

    result = AnalyticsDatasetRepository(session).daily_series(
        [client.id], Provider.gsc, "gsc_daily", date(2026, 7, 1), date(2026, 7, 31)
    )

    assert set(result[client.id]) == {date(2026, 7, 15)}


def test_multiple_clients_do_not_mix(session):
    client_a = _client(session, slug="acme")
    client_b = _client(session, slug="zenith")
    snap_a = _snapshot(session, client_a)
    snap_b = _snapshot(session, client_b)
    _insert_day(session, client_a, snap_a, "2026-07-10", 10)
    _insert_day(session, client_b, snap_b, "2026-07-10", 20)

    result = AnalyticsDatasetRepository(session).daily_series(
        [client_a.id, client_b.id], Provider.gsc, "gsc_daily", date(2026, 7, 1), date(2026, 7, 31)
    )

    assert result[client_a.id][date(2026, 7, 10)].metrics_json["clicks"] == "10"
    assert result[client_b.id][date(2026, 7, 10)].metrics_json["clicks"] == "20"


def test_empty_client_id_list_returns_empty_dict(session):
    result = AnalyticsDatasetRepository(session).daily_series(
        [], Provider.gsc, "gsc_daily", date(2026, 7, 1), date(2026, 7, 31)
    )

    assert result == {}


def test_non_iso_dimension_value_is_skipped_without_crashing(session):
    client = _client(session)
    snapshot = _snapshot(session, client)
    # "2026-06-99" sorts lexically between the window bounds below (so it is
    # NOT filtered out by the SQL string-range clause), but has an invalid
    # day-of-month — it must be dropped by `date.fromisoformat`'s try/except,
    # not raise, and not smuggle a bogus value into the returned series.
    AnalyticsDatasetRepository(session).bulk_insert(
        snapshot_id=snapshot.id,
        client_id=client.id,
        provider=Provider.gsc,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 1),
        rows=[
            AnalyticsDatasetRowInput(
                dataset_key="gsc_daily",
                dimension_key="date",
                dimension_value="2026-06-99",
                metrics={"clicks": Decimal(5)},
                quality=Quality.complete,
                rank=1,
            )
        ],
    )
    session.commit()

    result = AnalyticsDatasetRepository(session).daily_series(
        [client.id], Provider.gsc, "gsc_daily", date(2026, 1, 1), date(2026, 12, 31)
    )

    assert result.get(client.id, {}) == {}
