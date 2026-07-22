from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.db.repositories.cells import CellRepository
from app.db.repositories.clients import ClientRepository
from app.db.repositories.jobs import JobRepository
from app.db.repositories.metrics import MetricRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.schemas.adapter import MetricValueResult
from app.schemas.enums import (
    CellStatus,
    ComparisonMode,
    PeriodKind,
    PeriodRole,
    Provider,
    Quality,
    SourceMode,
)

_ANCHOR = date(2026, 1, 12)


def _create_client(db, *, name: str, slug: str):
    return ClientRepository(db).create(
        name=name, slug=slug, domain=f"{slug}.com", timezone="America/New_York"
    )


def _seed_gsc_snapshot(
    db, client_id: str, *, job_id: str, current: int, previous: int, observed_at: datetime
) -> None:
    snapshot = SnapshotRepository(db).create_immutable(
        job_id=job_id,
        client_id=client_id,
        provider=Provider.gsc,
        source_mode=SourceMode.api,
        observed_at=observed_at,
        quality=Quality.complete,
        data_through=observed_at.date(),
    )
    MetricRepository(db).bulk_insert(
        snapshot_id=snapshot.id,
        client_id=client_id,
        provider=Provider.gsc,
        values=[
            MetricValueResult(
                metric_key="gsc_clicks",
                period_role=PeriodRole.current,
                period_start=date(2026, 1, 5),
                period_end=date(2026, 1, 11),
                value_numeric=Decimal(current),
                unit="count",
                quality=Quality.complete,
            ),
            MetricValueResult(
                metric_key="gsc_clicks",
                period_role=PeriodRole.previous,
                period_start=date(2025, 12, 29),
                period_end=date(2026, 1, 4),
                value_numeric=Decimal(previous),
                unit="count",
                quality=Quality.complete,
            ),
        ],
    )


def _full_coverage_client(
    db,
    *,
    name: str,
    slug: str,
    current: int = 110,
    previous: int = 100,
    observed_at: datetime | None = None,
) -> str:
    """Single `gsc` integration, `complete` -> coverage ratio 1.0."""
    client = _create_client(db, name=name, slug=slug)
    ClientRepository(db).upsert_integration(
        client.id, Provider.gsc, enabled=True, source_mode=SourceMode.api
    )
    job = JobRepository(db).create_run(
        client_ids=[client.id],
        providers=[Provider.gsc],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=_ANCHOR,
    )[0]
    _seed_gsc_snapshot(
        db,
        client.id,
        job_id=job.id,
        current=current,
        previous=previous,
        observed_at=observed_at or datetime.now(UTC),
    )
    db.commit()
    return client.id


def _partial_coverage_client(db, *, name: str, slug: str) -> str:
    """`gsc` complete + `semrush` failed -> configured=2, contributing=1 (ratio 0.5)."""
    clients = ClientRepository(db)
    client = _create_client(db, name=name, slug=slug)
    clients.upsert_integration(client.id, Provider.gsc, enabled=True, source_mode=SourceMode.api)
    clients.upsert_integration(
        client.id, Provider.semrush, enabled=True, source_mode=SourceMode.auto
    )
    job = JobRepository(db).create_run(
        client_ids=[client.id],
        providers=[Provider.gsc, Provider.semrush],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=_ANCHOR,
    )[0]
    semrush_cell = next(
        c for c in CellRepository(db).list_for_job(job.id) if c.provider == Provider.semrush
    )
    CellRepository(db).set_status(
        semrush_cell.id, CellStatus.failed, error_code="collection_failed"
    )
    _seed_gsc_snapshot(
        db, client.id, job_id=job.id, current=110, previous=100, observed_at=datetime.now(UTC)
    )
    db.commit()
    return client.id


def _no_coverage_client(db, *, name: str, slug: str) -> str:
    """No integrations at all -> `not_configured` everywhere, coverage 0."""
    client = _create_client(db, name=name, slug=slug)
    db.commit()
    return client.id


def test_default_sort_orders_by_coverage_and_puts_unconfigured_last(client, db):
    full_id = _full_coverage_client(db, name="Alpha", slug="alpha")
    partial_id = _partial_coverage_client(db, name="Gamma", slug="gamma")
    none_id = _no_coverage_client(db, name="Zeta", slug="zeta")

    response = client.get("/api/v1/dashboard")
    ids = [c["client_id"] for c in response.json()["clients"]]

    assert ids.index(full_id) < ids.index(partial_id) < ids.index(none_id)


def test_sort_name_is_alphabetical_regardless_of_coverage(client, db):
    _no_coverage_client(db, name="Charlie", slug="charlie")
    _full_coverage_client(db, name="Alpha", slug="alpha")
    _partial_coverage_client(db, name="Bravo", slug="bravo")

    response = client.get("/api/v1/dashboard", params={"sort": "name"})
    names = [c["name"] for c in response.json()["clients"]]

    assert names == ["Alpha", "Bravo", "Charlie"]


def test_sort_last_sync_orders_by_recency_never_synced_last(client, db):
    now = datetime.now(UTC)
    old_id = _full_coverage_client(db, name="Old", slug="old", observed_at=now - timedelta(days=7))
    new_id = _full_coverage_client(db, name="New", slug="new", observed_at=now)
    never_id = _no_coverage_client(db, name="Never", slug="never")

    response = client.get("/api/v1/dashboard", params={"sort": "last_sync"})
    ids = [c["client_id"] for c in response.json()["clients"]]

    assert ids == [new_id, old_id, never_id]


def test_sort_health_orders_by_health_value_none_last(client, db):
    high_id = _full_coverage_client(db, name="HighHealth", slug="high", current=150, previous=100)
    low_id = _full_coverage_client(db, name="LowHealth", slug="low", current=50, previous=100)
    none_id = _no_coverage_client(db, name="NoHealth", slug="nohealth")

    response = client.get("/api/v1/dashboard", params={"sort": "health"})
    ids = [c["client_id"] for c in response.json()["clients"]]

    assert ids == [high_id, low_id, none_id]


def test_sort_biggest_drop_orders_worst_metric_first(client, db):
    big_drop_id = _full_coverage_client(
        db, name="BigDrop", slug="bigdrop", current=10, previous=100
    )
    small_drop_id = _full_coverage_client(
        db, name="SmallDrop", slug="smalldrop", current=95, previous=100
    )
    no_signal_id = _no_coverage_client(db, name="NoSignal", slug="nosignal")

    response = client.get("/api/v1/dashboard", params={"sort": "biggest_drop"})
    ids = [c["client_id"] for c in response.json()["clients"]]

    assert ids == [big_drop_id, small_drop_id, no_signal_id]


def test_default_sort_is_deterministic_and_stable_on_ties(client, db):
    now = datetime.now(UTC)
    # Same coverage, same overall_status, same observed_at -> the only thing
    # left to break the tie is name, not insertion order.
    bravo_id = _full_coverage_client(db, name="Bravo", slug="bravo", observed_at=now)
    alpha_id = _full_coverage_client(db, name="Alpha", slug="alpha", observed_at=now)

    first = client.get("/api/v1/dashboard").json()["clients"]
    second = client.get("/api/v1/dashboard").json()["clients"]

    assert [c["client_id"] for c in first] == [alpha_id, bravo_id]
    assert [c["client_id"] for c in first] == [c["client_id"] for c in second]
