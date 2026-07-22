"""`None` round-trips as `None`, never coerced to `0` (CLAUDE.md invariant)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.db.base import utcnow
from app.db.models import Client, MetricValue, ProviderSnapshot, SyncJob
from app.schemas.enums import (
    ComparisonMode,
    MetricUnit,
    PeriodKind,
    PeriodRole,
    Provider,
    Quality,
    SourceMode,
)


def test_none_value_numeric_is_not_zero(session):
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

    snap = ProviderSnapshot(
        job_id=job.id,
        client_id=client.id,
        provider=Provider.gsc,
        source_mode=SourceMode.api,
        observed_at=utcnow(),
        quality=Quality.unavailable,
    )
    session.add(snap)
    session.commit()

    metric = MetricValue(
        snapshot_id=snap.id,
        client_id=client.id,
        provider=Provider.gsc,
        metric_key="clicks",
        period_role=PeriodRole.current,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 7),
        value_numeric=None,
        unit=MetricUnit.count,
        quality=Quality.unavailable,
    )
    session.add(metric)
    session.commit()
    session.expire_all()

    reloaded = session.get(MetricValue, metric.id)
    assert reloaded.value_numeric is None
    assert reloaded.value_numeric != 0


def test_decimal_precision_preserved(session):
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

    snap = ProviderSnapshot(
        job_id=job.id,
        client_id=client.id,
        provider=Provider.gsc,
        source_mode=SourceMode.api,
        observed_at=utcnow(),
        quality=Quality.complete,
    )
    session.add(snap)
    session.commit()

    metric = MetricValue(
        snapshot_id=snap.id,
        client_id=client.id,
        provider=Provider.gsc,
        metric_key="avg_position",
        period_role=PeriodRole.current,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 7),
        value_numeric=Decimal("12.3456"),
        unit=MetricUnit.position,
        quality=Quality.complete,
    )
    session.add(metric)
    session.commit()
    session.expire_all()

    reloaded = session.get(MetricValue, metric.id)
    assert reloaded.value_numeric == Decimal("12.3456")
