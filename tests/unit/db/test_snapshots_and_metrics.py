"""SnapshotRepository/MetricRepository: immutable insert + read-back semantics."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.db.base import utcnow
from app.db.models import Client, SyncJob
from app.db.repositories.metrics import MetricRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.schemas.adapter import MetricValueResult
from app.schemas.enums import (
    ComparisonMode,
    MetricUnit,
    PeriodKind,
    PeriodRole,
    Provider,
    Quality,
    SourceMode,
)


def _make_job(session) -> SyncJob:
    client = Client(name="Acme", slug="acme", domain="acme.com")
    session.add(client)
    session.commit()
    job = SyncJob(
        client_id=client.id,
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 1),
    )
    session.add(job)
    session.commit()
    return job


def test_latest_success_orders_by_observed_at(session):
    job = _make_job(session)
    repo = SnapshotRepository(session)

    older = repo.create_immutable(
        job_id=job.id,
        client_id=job.client_id,
        provider=Provider.ahrefs,
        source_mode=SourceMode.api,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        quality=Quality.complete,
    )
    newer = repo.create_immutable(
        job_id=job.id,
        client_id=job.client_id,
        provider=Provider.ahrefs,
        source_mode=SourceMode.api,
        observed_at=datetime(2026, 1, 8, tzinfo=UTC),
        quality=Quality.complete,
    )

    assert repo.latest_success(job.client_id, Provider.ahrefs).id == newer.id
    assert older.id != newer.id


def test_latest_success_excludes_unavailable(session):
    job = _make_job(session)
    repo = SnapshotRepository(session)
    repo.create_immutable(
        job_id=job.id,
        client_id=job.client_id,
        provider=Provider.ahrefs,
        source_mode=SourceMode.api,
        observed_at=utcnow(),
        quality=Quality.unavailable,
    )

    assert repo.latest_success(job.client_id, Provider.ahrefs) is None


def test_latest_success_can_require_reporting_ninja_api_source(session):
    job = _make_job(session)
    repo = SnapshotRepository(session)
    imported = repo.create_immutable(
        job_id=job.id,
        client_id=job.client_id,
        provider=Provider.gsc,
        source_mode=SourceMode.browser,
        observed_at=datetime(2026, 1, 8, tzinfo=UTC),
        quality=Quality.complete,
    )
    live = repo.create_immutable(
        job_id=job.id,
        client_id=job.client_id,
        provider=Provider.gsc,
        source_mode=SourceMode.api,
        observed_at=datetime(2026, 1, 7, tzinfo=UTC),
        quality=Quality.complete,
    )

    assert repo.latest_success(job.client_id, Provider.gsc).id == imported.id
    assert repo.latest_success(
        job.client_id, Provider.gsc, source_mode=SourceMode.api
    ).id == live.id


def test_bulk_insert_metrics_preserves_none_and_decimal(session):
    job = _make_job(session)
    snapshot = SnapshotRepository(session).create_immutable(
        job_id=job.id,
        client_id=job.client_id,
        provider=Provider.gsc,
        source_mode=SourceMode.api,
        observed_at=utcnow(),
        quality=Quality.complete,
    )

    values = [
        MetricValueResult(
            metric_key="clicks",
            period_role=PeriodRole.current,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 7),
            value_numeric=Decimal("123"),
            unit=MetricUnit.count,
            quality=Quality.complete,
        ),
        MetricValueResult(
            metric_key="ctr",
            period_role=PeriodRole.current,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 7),
            value_numeric=None,
            unit=MetricUnit.percent,
            quality=Quality.unavailable,
        ),
    ]

    rows = MetricRepository(session).bulk_insert(
        snapshot_id=snapshot.id, client_id=job.client_id, provider=Provider.gsc, values=values
    )

    by_key = {r.metric_key: r for r in rows}
    assert by_key["clicks"].value_numeric == Decimal("123")
    assert by_key["ctr"].value_numeric is None


def test_history_limits_and_orders(session):
    job = _make_job(session)
    snap_repo = SnapshotRepository(session)
    metric_repo = MetricRepository(session)

    for i in range(3):
        snapshot = snap_repo.create_immutable(
            job_id=job.id,
            client_id=job.client_id,
            provider=Provider.ahrefs,
            source_mode=SourceMode.api,
            observed_at=utcnow() + timedelta(seconds=i),
            quality=Quality.complete,
        )
        metric_repo.bulk_insert(
            snapshot_id=snapshot.id,
            client_id=job.client_id,
            provider=Provider.ahrefs,
            values=[
                MetricValueResult(
                    metric_key="health_score",
                    period_role=PeriodRole.snapshot,
                    value_numeric=Decimal(str(70 + i)),
                    unit=MetricUnit.score,
                    quality=Quality.complete,
                )
            ],
        )

    history = metric_repo.history(job.client_id, "health_score", n=2)
    assert len(history) == 2
