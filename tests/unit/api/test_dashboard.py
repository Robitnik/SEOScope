from __future__ import annotations

from datetime import UTC, date, datetime
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


def _seed_client_with_gsc_snapshot(db, *, slug: str = "acme") -> str:
    clients = ClientRepository(db)
    client = clients.create(
        name="Acme", slug=slug, domain=f"{slug}.com", timezone="America/New_York"
    )
    clients.upsert_integration(client.id, Provider.gsc, enabled=True, source_mode=SourceMode.api)

    jobs = JobRepository(db).create_run(
        client_ids=[client.id],
        providers=[Provider.gsc],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 12),
    )
    job = jobs[0]

    snapshot = SnapshotRepository(db).create_immutable(
        job_id=job.id,
        client_id=client.id,
        provider=Provider.gsc,
        source_mode=SourceMode.api,
        observed_at=datetime.now(UTC),
        quality=Quality.complete,
        data_through=date(2026, 1, 11),
    )
    MetricRepository(db).bulk_insert(
        snapshot_id=snapshot.id,
        client_id=client.id,
        provider=Provider.gsc,
        values=[
            MetricValueResult(
                metric_key="gsc_clicks",
                period_role=PeriodRole.current,
                period_start=date(2026, 1, 5),
                period_end=date(2026, 1, 11),
                value_numeric=Decimal(100),
                unit="count",
                quality=Quality.complete,
            ),
            MetricValueResult(
                metric_key="gsc_clicks",
                period_role=PeriodRole.previous,
                period_start=date(2025, 12, 29),
                period_end=date(2026, 1, 4),
                value_numeric=Decimal(80),
                unit="count",
                quality=Quality.complete,
            ),
        ],
    )
    db.commit()
    return client.id


def test_dashboard_new_client_is_not_configured_never_zero(client, db):
    ClientRepository(db).create(
        name="NewCo", slug="newco", domain="newco.com", timezone="America/New_York"
    )
    db.commit()

    response = client.get("/api/v1/dashboard")
    assert response.status_code == 200
    body = response.json()
    assert len(body["clients"]) == 1
    card = body["clients"][0]
    providers = {p["provider"]: p for p in card["providers"]}
    assert providers[Provider.gsc.value]["state"] == "not_configured"
    assert providers[Provider.gsc.value]["metrics"] == []
    # not_configured must never be silently a quality/zero value.
    assert providers[Provider.gsc.value]["quality"] is None


def test_dashboard_preflight_not_configured_is_not_reported_as_outage(client, db):
    clients = ClientRepository(db)
    created = clients.create(
        name="Needs Setup",
        slug="needs-setup",
        domain="needs-setup.com",
        timezone="America/New_York",
    )
    clients.upsert_integration(
        created.id,
        Provider.semrush,
        enabled=True,
        source_mode=SourceMode.auto,
        config={"domain": created.domain},
    )
    job = JobRepository(db).create_run(
        client_ids=[created.id],
        providers=[Provider.semrush],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 12),
    )[0]
    cell = CellRepository(db).list_for_job(job.id)[0]
    CellRepository(db).set_status(
        cell.id,
        CellStatus.not_configured,
        error_code="not_configured",
    )
    db.commit()

    response = client.get("/api/v1/dashboard")

    assert response.status_code == 200
    card = next(item for item in response.json()["clients"] if item["client_id"] == created.id)
    semrush = next(item for item in card["providers"] if item["provider"] == "semrush")
    assert semrush["state"] == "not_configured"
    assert semrush["metrics"] == []


def test_dashboard_client_with_gsc_snapshot_has_delta_and_ranges(client, db):
    client_id = _seed_client_with_gsc_snapshot(db)

    # Match the anchor used to hand-pick the seeded metric period ranges
    # above (`_last_full_week_closed(2026-01-12)` == 2026-01-05..01-11).
    response = client.get("/api/v1/dashboard", params={"anchor": "2026-01-12"})
    assert response.status_code == 200
    card = next(c for c in response.json()["clients"] if c["client_id"] == client_id)
    gsc = next(p for p in card["providers"] if p["provider"] == Provider.gsc.value)
    assert gsc["state"] == "complete"

    clicks = next(m for m in gsc["metrics"] if m["metric_key"] == "gsc_clicks")
    assert clicks["current_value"] == "100"
    assert clicks["previous_value"] == "80"
    assert clicks["delta_abs"] == "20"
    assert clicks["is_improvement"] is True
    assert clicks["current_range"] == {"start": "2026-01-05", "end": "2026-01-11"}

    # A metric never seeded (no data yet) must be None, never 0.
    impressions = next(m for m in gsc["metrics"] if m["metric_key"] == "gsc_impressions")
    assert impressions["current_value"] is None
    assert impressions["delta_reason"] == "current_missing"


def test_dashboard_ignores_future_and_wrong_period_gsc_snapshots(client, db):
    client_id = _seed_client_with_gsc_snapshot(db)
    job_id = CellRepository(db).latest_for_client_provider(client_id, Provider.gsc).job_id
    snapshots = SnapshotRepository(db)
    metrics = MetricRepository(db)

    for observed_at, period_start, period_end, value, source_mode in (
        (datetime.now(UTC), date(2025, 12, 1), date(2025, 12, 31), 999, SourceMode.api),
        (
            datetime.now(UTC),
            date(2026, 1, 5),
            date(2026, 1, 11),
            666,
            SourceMode.browser,
        ),
        (
            datetime(2099, 1, 1, tzinfo=UTC),
            date(2026, 1, 5),
            date(2026, 1, 11),
            777,
            SourceMode.api,
        ),
    ):
        snapshot = snapshots.create_immutable(
            job_id=job_id,
            client_id=client_id,
            provider=Provider.gsc,
            source_mode=source_mode,
            observed_at=observed_at,
            quality=Quality.complete,
        )
        metrics.bulk_insert(
            snapshot_id=snapshot.id,
            client_id=client_id,
            provider=Provider.gsc,
            values=[
                MetricValueResult(
                    metric_key="gsc_clicks",
                    period_role=PeriodRole.current,
                    period_start=period_start,
                    period_end=period_end,
                    value_numeric=Decimal(value),
                    unit="count",
                    quality=Quality.complete,
                )
            ],
        )
    db.commit()

    response = client.get("/api/v1/dashboard", params={"anchor": "2026-01-12"})
    card = next(c for c in response.json()["clients"] if c["client_id"] == client_id)
    gsc = next(p for p in card["providers"] if p["provider"] == Provider.gsc.value)
    clicks = next(m for m in gsc["metrics"] if m["metric_key"] == "gsc_clicks")

    assert clicks["current_value"] == "100"
    assert clicks["current_range"] == {"start": "2026-01-05", "end": "2026-01-11"}


def test_dashboard_provider_filter(client, db):
    _seed_client_with_gsc_snapshot(db)

    response = client.get("/api/v1/dashboard", params={"provider": Provider.gsc.value})
    card = response.json()["clients"][0]
    assert [p["provider"] for p in card["providers"]] == [Provider.gsc.value]


def test_dashboard_search_filter(client, db):
    _seed_client_with_gsc_snapshot(db, slug="acme")
    ClientRepository(db).create(
        name="Beta", slug="beta", domain="beta.com", timezone="America/New_York"
    )
    db.commit()

    response = client.get("/api/v1/dashboard", params={"q": "acme"})
    names = [c["name"] for c in response.json()["clients"]]
    assert names == ["Acme"]


def test_client_metrics_history_endpoint(client, db):
    client_id = _seed_client_with_gsc_snapshot(db)
    response = client.get(f"/api/v1/clients/{client_id}/metrics", params={"history": 5})
    assert response.status_code == 200
    body = response.json()
    series = next(s for s in body["series"] if s["metric_key"] == "gsc_clicks")
    assert len(series["points"]) == 1
    assert series["points"][0]["value_numeric"] == "100"


def test_client_metrics_history_filters_cadence_and_deduplicates_ranges(client, db):
    client_id = _seed_client_with_gsc_snapshot(db)
    job = JobRepository(db).create_run(
        client_ids=[client_id],
        providers=[Provider.gsc],
        period_kind=PeriodKind.month,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 2, 2),
    )[0]
    for observed_at, start, end, value in (
        (datetime(2026, 1, 13, tzinfo=UTC), date(2026, 1, 5), date(2026, 1, 11), 110),
        (datetime(2026, 2, 2, tzinfo=UTC), date(2026, 1, 1), date(2026, 1, 31), 500),
    ):
        snapshot = SnapshotRepository(db).create_immutable(
            job_id=job.id,
            client_id=client_id,
            provider=Provider.gsc,
            source_mode=SourceMode.api,
            observed_at=observed_at,
            quality=Quality.complete,
            data_through=end,
        )
        MetricRepository(db).bulk_insert(
            snapshot_id=snapshot.id,
            client_id=client_id,
            provider=Provider.gsc,
            values=[
                MetricValueResult(
                    metric_key="gsc_clicks",
                    period_role=PeriodRole.current,
                    period_start=start,
                    period_end=end,
                    value_numeric=Decimal(value),
                    unit="count",
                    quality=Quality.complete,
                )
            ],
        )
    db.commit()

    weekly = client.get(
        f"/api/v1/clients/{client_id}/metrics", params={"period": "week", "history": 5}
    ).json()
    monthly = client.get(
        f"/api/v1/clients/{client_id}/metrics", params={"period": "month", "history": 5}
    ).json()
    weekly_clicks = next(s for s in weekly["series"] if s["metric_key"] == "gsc_clicks")
    monthly_clicks = next(s for s in monthly["series"] if s["metric_key"] == "gsc_clicks")

    assert len(weekly_clicks["points"]) == 1
    assert weekly_clicks["points"][0]["value_numeric"] == "110"
    assert len(monthly_clicks["points"]) == 1
    assert monthly_clicks["points"][0]["value_numeric"] == "500"


def test_client_metrics_history_missing_client_is_404(client):
    response = client.get("/api/v1/clients/does-not-exist/metrics")
    assert response.status_code == 404
