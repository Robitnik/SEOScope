"""GSC dimensional analytics endpoints (`/analytics/pages`, `/analytics/queries`,
task-16, PLAN §21.4): period filter, pagination, and the `truncated` flag never
silently passing a cut list off as complete (PLAN §12)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.db.models import DimensionKind
from app.db.repositories.clients import ClientRepository
from app.db.repositories.dimensions import DimensionRepository, DimensionRowInput
from app.db.repositories.jobs import JobRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.metrics import resolve_periods
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


def _seed(db, *, slug: str = "acme", rows=None, truncated=False):
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
    snapshot = SnapshotRepository(db).create_immutable(
        job_id=job.id,
        client_id=client.id,
        provider=Provider.gsc,
        source_mode=SourceMode.browser,
        observed_at=datetime.now(UTC),
        quality=Quality.truncated if truncated else Quality.complete,
    )
    periods = resolve_periods(
        PeriodKind.week, ANCHOR, tz=client.timezone, mode=ComparisonMode.closed
    )
    if rows is not None:
        DimensionRepository(db).bulk_insert(
            snapshot_id=snapshot.id,
            client_id=client.id,
            dimension=DimensionKind.page,
            period_start=periods.current.start,
            period_end=periods.current.end,
            rows=rows,
        )
    db.commit()
    return client.id, periods


def test_page_analytics_returns_rows_sorted_by_clicks(client, db):
    client_id, _periods = _seed(db, rows=[_row("/a", clicks=5), _row("/b", clicks=50, rank=2)])

    response = client.get(f"/api/v1/clients/{client_id}/analytics/pages?anchor=2026-01-12")

    assert response.status_code == 200
    body = response.json()
    assert [r["dimension_value"] for r in body["rows"]] == ["/b", "/a"]
    assert body["total_rows_available"] == 2
    assert body["truncated"] is False


def test_query_analytics_scoped_separately_from_pages(client, db):
    clients = ClientRepository(db)
    c = clients.create(name="Acme", slug="acme", domain="acme.com", timezone="America/New_York")
    job = JobRepository(db).create_run(
        client_ids=[c.id],
        providers=[Provider.gsc],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 12),
    )[0]
    snapshot = SnapshotRepository(db).create_immutable(
        job_id=job.id,
        client_id=c.id,
        provider=Provider.gsc,
        source_mode=SourceMode.browser,
        observed_at=datetime.now(UTC),
        quality=Quality.complete,
    )
    periods = resolve_periods(PeriodKind.week, ANCHOR, tz=c.timezone, mode=ComparisonMode.closed)
    DimensionRepository(db).bulk_insert(
        snapshot_id=snapshot.id,
        client_id=c.id,
        dimension=DimensionKind.query,
        period_start=periods.current.start,
        period_end=periods.current.end,
        rows=[_row("shoes")],
    )
    db.commit()

    pages_resp = client.get(f"/api/v1/clients/{c.id}/analytics/pages?anchor=2026-01-12")
    queries_resp = client.get(f"/api/v1/clients/{c.id}/analytics/queries?anchor=2026-01-12")

    assert pages_resp.json()["rows"] == []
    assert [r["dimension_value"] for r in queries_resp.json()["rows"]] == ["shoes"]


def test_truncated_period_is_flagged_and_never_passed_as_full_list(client, db):
    rows = [_row(f"/p{i}", quality=Quality.truncated) for i in range(3)]
    rows.append(
        DimensionRowInput(
            dimension_value="інше",
            clicks=1,
            impressions=1,
            ctr=None,
            position=None,
            quality=Quality.truncated,
            rank=None,
        )
    )
    client_id, _ = _seed(db, rows=rows, truncated=True)

    response = client.get(
        f"/api/v1/clients/{client_id}/analytics/pages?anchor=2026-01-12&limit=2"
    )

    body = response.json()
    assert body["truncated"] is True
    assert body["total_rows_available"] == 4  # full set size, independent of this page's `limit`
    assert len(body["rows"]) == 2  # this page honors `limit`, distinct from `total_rows_available`


def test_no_matching_stored_period_returns_empty_not_error(client, db):
    client_id, _ = _seed(db, rows=None)

    response = client.get(f"/api/v1/clients/{client_id}/analytics/pages?anchor=2026-01-12")

    assert response.status_code == 200
    body = response.json()
    assert body["rows"] == []
    assert body["total_rows_available"] == 0
    assert body["period_start"] is None


def test_unknown_client_is_404(client, db):
    response = client.get("/api/v1/clients/does-not-exist/analytics/pages")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "client_not_found"


def test_pagination_offset(client, db):
    rows = [_row(f"/p{i}", clicks=100 - i, rank=i + 1) for i in range(5)]
    client_id, _ = _seed(db, rows=rows)

    response = client.get(
        f"/api/v1/clients/{client_id}/analytics/pages?anchor=2026-01-12&limit=2&offset=2"
    )

    body = response.json()
    assert [r["dimension_value"] for r in body["rows"]] == ["/p2", "/p3"]
    assert body["limit"] == 2
    assert body["offset"] == 2
