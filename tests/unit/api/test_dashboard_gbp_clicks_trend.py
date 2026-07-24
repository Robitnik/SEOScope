"""Card GBP clicks trend: the week-over-week arrow and daily sparkline that
sit above the Reporting Ninja section, mirroring the Ahrefs/health pairing
above the Ahrefs section (test_dashboard_health_trend.py).

`ClientCardDTO.gbp_clicks_trend` reads straight off the `gbp_daily` dataset
rows the Reporting Ninja adapter already persists per day (no dedicated
history table, unlike composite health) — these tests lock in that contract.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.db.repositories.analytics_datasets import (
    AnalyticsDatasetRepository,
    AnalyticsDatasetRowInput,
)
from app.db.repositories.clients import ClientRepository
from app.db.repositories.jobs import JobRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.schemas.enums import ComparisonMode, PeriodKind, Provider, Quality, SourceMode


def _seed_client(db, *, slug: str = "clicks-trend") -> str:
    clients = ClientRepository(db)
    client = clients.create(
        name="Clicks Co", slug=slug, domain=f"{slug}.com", timezone="America/New_York"
    )
    clients.upsert_integration(
        client.id, Provider.reporting_ninja, enabled=True, source_mode=SourceMode.api
    )
    return client.id


def _seed_gbp_day(db, client_id: str, day: date, website_clicks: int) -> None:
    job = JobRepository(db).create_run(
        client_ids=[client_id],
        providers=[Provider.reporting_ninja],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=day,
        mode="live",
    )[0]
    snapshot = SnapshotRepository(db).create_immutable(
        job_id=job.id,
        client_id=client_id,
        provider=Provider.reporting_ninja,
        source_mode=SourceMode.api,
        observed_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
        quality=Quality.complete,
        warnings=[],
        provenance={},
    )
    AnalyticsDatasetRepository(db).bulk_insert(
        snapshot_id=snapshot.id,
        client_id=client_id,
        provider=Provider.reporting_ninja,
        period_start=day,
        period_end=day,
        rows=[
            AnalyticsDatasetRowInput(
                dataset_key="gbp_daily",
                dimension_key="day",
                dimension_value=day.isoformat(),
                metrics={"website_clicks": website_clicks},
                quality=Quality.complete,
                rank=None,
            )
        ],
    )


def _trend_of(payload, client_id: str):
    return next(c for c in payload["clients"] if c["client_id"] == client_id)["gbp_clicks_trend"]


def test_history_and_delta_are_exposed_on_the_card(client, db):
    client_id = _seed_client(db)
    today = date.today()
    _seed_gbp_day(db, client_id, today - timedelta(days=8), 50)
    _seed_gbp_day(db, client_id, today, 60)
    db.commit()

    trend = _trend_of(client.get("/api/v1/dashboard").json(), client_id)

    days = [point["day"] for point in trend["history"]]
    assert days == sorted(days), "series must be oldest-first for the sparkline"
    assert (today - timedelta(days=8)).isoformat() in days
    assert trend["value"] == "60"
    # Compared against the nearest point at or before D-7, i.e. the 50.
    assert trend["value_previous"] == "50"
    assert trend["delta_pct"] == "20.0"


def test_no_comparison_basis_yields_none_not_zero_percent(client, db):
    """A client with only today's point has an unknown trend, not a flat one."""
    client_id = _seed_client(db)
    _seed_gbp_day(db, client_id, date.today(), 42)
    db.commit()

    trend = _trend_of(client.get("/api/v1/dashboard").json(), client_id)

    assert trend["delta_pct"] is None
    assert trend["value_previous"] is None
    assert trend["value"] == "42"


def test_a_client_with_no_gbp_data_never_shows_zero(client, db):
    client_id = _seed_client(db)
    db.commit()

    trend = _trend_of(client.get("/api/v1/dashboard").json(), client_id)

    assert trend["value"] is None
    assert trend["history"] == []
