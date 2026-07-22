"""`provider_snapshots`/`metric_values` reject updates after commit (PLAN §6)."""

from __future__ import annotations

from datetime import date

import pytest

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


@pytest.fixture
def snapshot(session) -> ProviderSnapshot:
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
        provider=Provider.ahrefs,
        source_mode=SourceMode.api,
        observed_at=utcnow(),
        quality=Quality.complete,
    )
    session.add(snap)
    session.commit()
    return snap


def test_snapshot_update_rejected(session, snapshot):
    snapshot.quality = Quality.partial
    with pytest.raises(ValueError, match="immutable"):
        session.commit()


def test_metric_value_update_rejected(session, snapshot):
    metric = MetricValue(
        snapshot_id=snapshot.id,
        client_id=snapshot.client_id,
        provider=Provider.ahrefs,
        metric_key="health_score",
        period_role=PeriodRole.snapshot,
        value_numeric=None,
        unit=MetricUnit.score,
        quality=Quality.complete,
    )
    session.add(metric)
    session.commit()

    metric.value_numeric = None  # any write at all must be rejected, even a no-op value
    metric.value_text = "changed"
    with pytest.raises(ValueError, match="immutable"):
        session.commit()
