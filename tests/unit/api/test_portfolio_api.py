"""Portfolio Trends endpoint (`GET /analytics/portfolio-series`, task-28):
one line per client for a single daily metric, bucketed and never turning
absence into a fabricated `0`."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.db.models import SyncJob
from app.db.repositories.analytics_datasets import (
    AnalyticsDatasetRepository,
    AnalyticsDatasetRowInput,
)
from app.db.repositories.clients import ClientRepository
from app.db.repositories.jobs import JobRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.schemas.enums import ComparisonMode, PeriodKind, Provider, Quality, SourceMode


def _seed_client_with_days(db, *, slug="acme", days: dict[str, int] | None = None, configure=True):
    clients = ClientRepository(db)
    client = clients.create(name=slug.title(), slug=slug, domain=f"{slug}.com")
    if configure:
        clients.upsert_integration(client.id, Provider.gsc, source_mode=SourceMode.api)
    if days:
        job = JobRepository(db).create_run(
            client_ids=[client.id],
            providers=[Provider.gsc],
            period_kind=PeriodKind.week,
            comparison_mode=ComparisonMode.closed,
            anchor_date=date(2026, 7, 27),
        )[0]
        snapshot = SnapshotRepository(db).create_immutable(
            job_id=job.id,
            client_id=client.id,
            provider=Provider.gsc,
            source_mode=SourceMode.api,
            observed_at=datetime.now(UTC),
            quality=Quality.complete,
        )
        AnalyticsDatasetRepository(db).bulk_insert(
            snapshot_id=snapshot.id,
            client_id=client.id,
            provider=Provider.gsc,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 27),
            rows=[
                AnalyticsDatasetRowInput(
                    dataset_key="gsc_daily",
                    dimension_key="date",
                    dimension_value=day,
                    metrics={"clicks": Decimal(clicks), "impressions": Decimal(clicks * 10)},
                    quality=Quality.complete,
                    rank=1,
                )
                for day, clicks in days.items()
            ],
        )
    db.commit()
    return client.id


def test_unknown_metric_is_404(client, db):
    response = client.get("/api/v1/analytics/portfolio-series?metric=not_a_metric")

    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "metric_not_found"


def test_unknown_client_id_is_404(client, db):
    response = client.get(
        "/api/v1/analytics/portfolio-series?metric=gsc_clicks&client_ids=does-not-exist"
    )

    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "client_not_found"


def test_end_before_start_is_422(client, db):
    response = client.get(
        "/api/v1/analytics/portfolio-series"
        "?metric=gsc_clicks&start=2026-07-10&end=2026-07-01"
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "invalid_range"


def test_start_without_end_is_422(client, db):
    response = client.get(
        "/api/v1/analytics/portfolio-series?metric=gsc_clicks&start=2026-07-01"
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "invalid_range"


def test_window_wider_than_max_is_422(client, db):
    response = client.get(
        "/api/v1/analytics/portfolio-series"
        "?metric=gsc_clicks&start=2020-01-01&end=2026-01-01"
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "range_too_wide"


def test_client_without_integration_is_excluded_not_zero(client, db):
    client_id = _seed_client_with_days(db, configure=False, days=None)

    response = client.get(
        "/api/v1/analytics/portfolio-series"
        f"?metric=gsc_clicks&client_ids={client_id}&start=2026-07-01&end=2026-07-27"
    )

    body = response.json()
    assert body["series"] == []
    assert len(body["excluded"]) == 1
    assert body["excluded"][0]["client_id"] == client_id
    assert body["excluded"][0]["state"] == "not_configured"


def test_client_with_integration_but_no_collected_days_is_no_data(client, db):
    client_id = _seed_client_with_days(db, configure=True, days=None)

    response = client.get(
        "/api/v1/analytics/portfolio-series"
        f"?metric=gsc_clicks&client_ids={client_id}&start=2026-07-01&end=2026-07-27"
    )

    body = response.json()
    assert body["series"] == []
    assert body["excluded"][0]["state"] == "no_data"


def test_client_with_partial_week_reports_points_and_never_fabricates_zero(client, db):
    client_id = _seed_client_with_days(
        db,
        configure=True,
        days={
            "2026-07-20": 10,
            "2026-07-21": 20,
            # 22..26 not collected
        },
    )

    response = client.get(
        "/api/v1/analytics/portfolio-series"
        f"?metric=gsc_clicks&client_ids={client_id}&bucket=day"
        "&start=2026-07-20&end=2026-07-22"
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["series"]) == 1
    row = body["series"][0]
    assert row["client_id"] == client_id
    points = row["points"]
    assert points[0]["value"] == "10"
    assert points[0]["quality"] == "complete"
    assert points[1]["value"] == "20"
    assert points[2]["value"] is None  # never "0"
    assert points[2]["days_with_data"] == 0
    assert points[2]["quality"] == "unavailable"


def test_response_shape_matches_contract(client, db):
    client_id = _seed_client_with_days(
        db, configure=True, days={"2026-07-20": 5}
    )

    response = client.get(
        "/api/v1/analytics/portfolio-series"
        f"?metric=gsc_impressions&client_ids={client_id}&bucket=day"
        "&start=2026-07-20&end=2026-07-20"
    )

    body = response.json()
    assert body["metric"]["key"] == "gsc_impressions"
    assert body["metric"]["provider"] == "gsc"
    assert body["metric"]["dataset_key"] == "gsc_daily"
    assert body["metric"]["field"] == "impressions"
    assert body["bucket"] == "day"
    assert body["range"] == {"start": "2026-07-20", "end": "2026-07-20"}
    assert len(body["buckets"]) == 1
    assert body["buckets"][0]["days_expected"] == 1
    assert body["series"][0]["points"][0]["value"] == "50"  # 5 clicks * 10 = impressions


def test_endpoint_never_creates_a_sync_job(client, db):
    client_id = _seed_client_with_days(db, configure=True, days={"2026-07-20": 5})
    before = db.query(SyncJob).count()

    client.get(
        "/api/v1/analytics/portfolio-series"
        f"?metric=gsc_clicks&client_ids={client_id}&start=2026-07-01&end=2026-07-27"
    )

    after = db.query(SyncJob).count()
    assert after == before
