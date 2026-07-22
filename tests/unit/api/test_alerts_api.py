"""Alerts endpoint (`GET /clients/{id}/alerts`, task-17, PLAN §21.5): resolves
the requested period pair, reads both sides of `gsc_dimension_metrics` and
runs `app.metrics.alerts.detect_alerts` over them."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.db.models import DimensionKind
from app.db.repositories.clients import ClientRepository
from app.db.repositories.dimensions import DimensionRepository, DimensionRowInput
from app.db.repositories.jobs import JobRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.metrics.periods import resolve_periods
from app.schemas.enums import ComparisonMode, PeriodKind, Provider, Quality, SourceMode

ANCHOR = datetime(2026, 1, 12, 12, tzinfo=UTC)


def _row(value: str, *, clicks=10, impressions=100, quality=Quality.complete, rank=1):
    return DimensionRowInput(
        dimension_value=value,
        clicks=clicks,
        impressions=impressions,
        ctr=None,
        position=None,
        quality=quality,
        rank=rank,
    )


def _seed(
    db,
    *,
    slug: str = "acme",
    dimension: str = DimensionKind.page,
    current_rows=None,
    previous_rows=None,
):
    clients = ClientRepository(db)
    client = clients.create(
        name="Acme", slug=slug, domain=f"{slug}.com", timezone="America/New_York"
    )
    job = JobRepository(db).create_run(
        client_ids=[client.id],
        providers=[Provider.gsc],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 12),
    )[0]
    periods = resolve_periods(
        PeriodKind.week, ANCHOR, tz=client.timezone, mode=ComparisonMode.closed
    )
    dims = DimensionRepository(db)
    for rows, side in ((current_rows, periods.current), (previous_rows, periods.previous)):
        if rows is None:
            continue
        snapshot = SnapshotRepository(db).create_immutable(
            job_id=job.id,
            client_id=client.id,
            provider=Provider.gsc,
            source_mode=SourceMode.browser,
            observed_at=datetime.now(UTC),
            quality=Quality.complete,
        )
        dims.bulk_insert(
            snapshot_id=snapshot.id,
            client_id=client.id,
            dimension=dimension,
            period_start=side.start,
            period_end=side.end,
            rows=rows,
        )
    db.commit()
    return client.id, periods


def test_alerts_returns_declining_and_rising_sorted_by_absolute_impact(client, db):
    client_id, _periods = _seed(
        db,
        current_rows=[
            _row("/big", clicks=4000, impressions=10_000, rank=1),
            _row("/small", clicks=10, impressions=100, rank=2),
        ],
        previous_rows=[
            _row("/big", clicks=5000, impressions=10_000, rank=1),
            _row("/small", clicks=20, impressions=100, rank=2),
        ],
    )

    response = client.get(f"/api/v1/clients/{client_id}/alerts?anchor=2026-01-12")

    assert response.status_code == 200
    body = response.json()
    assert [e["dimension_value"] for e in body["declining"]] == ["/big", "/small"]
    assert body["skipped_reason"] is None
    assert body["period_current_start"] and body["period_previous_start"]


def test_new_page_reported_separately_from_growth(client, db):
    client_id, _periods = _seed(
        db,
        current_rows=[_row("/fresh", clicks=80, impressions=500, rank=1)],
        previous_rows=[_row("/other", clicks=100, impressions=1000, rank=1)],
    )

    response = client.get(f"/api/v1/clients/{client_id}/alerts?anchor=2026-01-12")

    body = response.json()
    assert body["rising"] == []
    assert body["declining"] == []
    assert [e["dimension_value"] for e in body["new"]] == ["/fresh"]
    assert body["new"][0]["clicks_previous"] is None


def test_micro_volume_move_is_not_reported(client, db):
    client_id, _periods = _seed(
        db,
        current_rows=[_row("/a", clicks=2, impressions=5, rank=1)],
        previous_rows=[_row("/a", clicks=1, impressions=5, rank=1)],
    )

    response = client.get(f"/api/v1/clients/{client_id}/alerts?anchor=2026-01-12")

    body = response.json()
    assert body["rising"] == []
    assert body["declining"] == []
    assert body["new"] == []


def test_truncated_period_yields_no_alerts_and_explicit_reason(client, db):
    client_id, _periods = _seed(
        db,
        current_rows=[_row("/a", clicks=100, impressions=1000, quality=Quality.truncated, rank=1)],
        previous_rows=[_row("/a", clicks=200, impressions=1000, rank=1)],
    )

    response = client.get(f"/api/v1/clients/{client_id}/alerts?anchor=2026-01-12")

    body = response.json()
    assert body["declining"] == []
    assert body["rising"] == []
    assert body["new"] == []
    assert body["skipped_reason"] == "current_period_truncated"


def test_no_stored_data_returns_missing_period_data_reason(client, db):
    client_id, _periods = _seed(db, current_rows=None, previous_rows=None)

    response = client.get(f"/api/v1/clients/{client_id}/alerts?anchor=2026-01-12")

    assert response.status_code == 200
    body = response.json()
    assert body["skipped_reason"] == "missing_period_data"
    # Exact calendar dates are still returned even with no data (PLAN §21.5).
    assert body["period_current_start"] is not None
    assert body["period_previous_start"] is not None


def test_query_dimension_is_scoped_separately_from_pages(client, db):
    client_id, _periods = _seed(
        db,
        dimension=DimensionKind.query,
        current_rows=[_row("shoes", clicks=80, impressions=1000, rank=1)],
        previous_rows=[_row("shoes", clicks=100, impressions=1000, rank=1)],
    )

    pages_resp = client.get(f"/api/v1/clients/{client_id}/alerts?anchor=2026-01-12&dimension=page")
    queries_resp = client.get(
        f"/api/v1/clients/{client_id}/alerts?anchor=2026-01-12&dimension=query"
    )

    assert pages_resp.json()["skipped_reason"] == "missing_period_data"
    assert queries_resp.json()["declining"][0]["dimension_value"] == "shoes"


def test_unknown_client_is_404(client, db):
    response = client.get("/api/v1/clients/does-not-exist/alerts")

    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "client_not_found"
