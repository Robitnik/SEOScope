"""GSC dimensional analytics endpoints (`/analytics/pages`, `/analytics/queries`,
task-16, PLAN §21.4): period filter, pagination, and the `truncated` flag never
silently passing a cut list off as complete (PLAN §12)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.db.models import DimensionKind
from app.db.repositories.analytics_datasets import (
    AnalyticsDatasetRepository,
    AnalyticsDatasetRowInput,
)
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


def test_reporting_ninja_dataset_supports_sort_search_and_previous_role(client, db):
    clients = ClientRepository(db)
    created = clients.create(
        name="Acme GA4", slug="acme-ga4", domain="ga4.example.com", timezone="America/New_York"
    )
    job = JobRepository(db).create_run(
        client_ids=[created.id],
        providers=[Provider.reporting_ninja],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 12),
    )[0]
    periods = resolve_periods(
        PeriodKind.week, ANCHOR, tz=created.timezone, mode=ComparisonMode.closed
    )
    snapshot = SnapshotRepository(db).create_immutable(
        job_id=job.id,
        client_id=created.id,
        provider=Provider.reporting_ninja,
        source_mode=SourceMode.api,
        observed_at=datetime.now(UTC),
        quality=Quality.complete,
    )
    AnalyticsDatasetRepository(db).bulk_insert(
        snapshot_id=snapshot.id,
        client_id=created.id,
        provider=Provider.reporting_ninja,
        period_start=periods.previous.start,
        period_end=periods.previous.end,
        rows=[
            AnalyticsDatasetRowInput(
                dataset_key="ga4_channels",
                dimension_key="sessionDefaultChannelGroup",
                dimension_value="Organic Search",
                metrics={"sessions": Decimal(50), "users": Decimal(40)},
                quality=Quality.complete,
                rank=2,
            ),
            AnalyticsDatasetRowInput(
                dataset_key="ga4_channels",
                dimension_key="sessionDefaultChannelGroup",
                dimension_value="Direct",
                metrics={"sessions": Decimal(100), "users": Decimal(80)},
                quality=Quality.complete,
                rank=1,
            ),
        ],
    )
    db.commit()

    response = client.get(
        f"/api/v1/clients/{created.id}/analytics/reporting-ninja/ga4_channels",
        params={
            "anchor": "2026-01-12",
            "role": "previous",
            "sort_by": "sessions",
            "q": "organic",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["period_role"] == "previous"
    assert body["period_start"] == periods.previous.start.isoformat()
    assert body["rows"][0]["dimension_value"] == "Organic Search"
    assert body["rows"][0]["metrics"]["sessions"] == "50"


def test_gsc_daily_dataset_returns_exact_period_date_series(client, db):
    created = ClientRepository(db).create(
        name="Acme GSC Daily",
        slug="acme-gsc-daily",
        domain="daily.example.com",
        timezone="America/New_York",
    )
    job = JobRepository(db).create_run(
        client_ids=[created.id],
        providers=[Provider.gsc],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 12),
    )[0]
    periods = resolve_periods(
        PeriodKind.week, ANCHOR, tz=created.timezone, mode=ComparisonMode.closed
    )
    snapshot = SnapshotRepository(db).create_immutable(
        job_id=job.id,
        client_id=created.id,
        provider=Provider.gsc,
        source_mode=SourceMode.api,
        observed_at=datetime.now(UTC),
        quality=Quality.complete,
    )
    AnalyticsDatasetRepository(db).bulk_insert(
        snapshot_id=snapshot.id,
        client_id=created.id,
        provider=Provider.gsc,
        period_start=periods.current.start,
        period_end=periods.current.end,
        rows=[
            AnalyticsDatasetRowInput(
                dataset_key="gsc_daily",
                dimension_key="date",
                dimension_value="2026-01-06",
                metrics={"clicks": Decimal(20), "impressions": Decimal(200)},
                quality=Quality.complete,
                rank=2,
            ),
            AnalyticsDatasetRowInput(
                dataset_key="gsc_daily",
                dimension_key="date",
                dimension_value="2026-01-05",
                metrics={"clicks": Decimal(10), "impressions": Decimal(100)},
                quality=Quality.complete,
                rank=1,
            ),
        ],
    )
    db.commit()

    response = client.get(
        f"/api/v1/clients/{created.id}/analytics/gsc/daily",
        params={"anchor": "2026-01-12"},
    )

    assert response.status_code == 200
    body = response.json()
    assert [row["dimension_value"] for row in body["rows"]] == [
        "2026-01-05",
        "2026-01-06",
    ]
    assert body["rows"][1]["metrics"]["clicks"] == "20"
    assert body["total_rows_available"] == 2


def test_reporting_ninja_unknown_dataset_is_404(client, db):
    client_id, _ = _seed(db, rows=None)

    response = client.get(
        f"/api/v1/clients/{client_id}/analytics/reporting-ninja/not-a-dataset"
    )

    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "dataset_not_found"


def test_ahrefs_site_audit_and_affected_urls_return_latest_live_export(client, db):
    created = ClientRepository(db).create(
        name="Acme Ahrefs",
        slug="acme-ahrefs",
        domain="ahrefs.example.com",
        timezone="America/New_York",
    )
    job = JobRepository(db).create_run(
        client_ids=[created.id],
        providers=[Provider.ahrefs],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 12),
    )[0]
    snapshot = SnapshotRepository(db).create_immutable(
        job_id=job.id,
        client_id=created.id,
        provider=Provider.ahrefs,
        source_mode=SourceMode.browser,
        observed_at=datetime(2026, 1, 12, tzinfo=UTC),
        quality=Quality.complete,
        provenance={"project_id": "123"},
    )
    rows = [
        AnalyticsDatasetRowInput(
            dataset_key="ahrefs_issues",
            dimension_key="issue_slug",
            dimension_value="404_page",
            metrics={
                "name": "404 page",
                "severity": "Error",
                "priority": "high",
                "description": "Broken page",
                "how_to_fix": "Redirect it",
                "affected_urls": Decimal(2),
                "exported_rows": Decimal(1),
            },
            quality=Quality.complete,
            rank=1,
        ),
        AnalyticsDatasetRowInput(
            dataset_key="ahrefs_affected_urls",
            dimension_key="issue_url",
            dimension_value="404_page\x1f1\x1fhttps://ahrefs.example.com/missing",
            metrics={
                "url": "https://ahrefs.example.com/missing",
                "issue_slug": "404_page",
                "issue_name": "404 page",
                "severity": "Error",
                "details": {"HTTP code": "404", "Inlinks": "7"},
            },
            quality=Quality.complete,
            rank=1,
        ),
    ]
    for dataset_key in ("ahrefs_issues", "ahrefs_affected_urls"):
        AnalyticsDatasetRepository(db).bulk_insert(
            snapshot_id=snapshot.id,
            client_id=created.id,
            provider=Provider.ahrefs,
            period_start=date(2026, 1, 5),
            period_end=date(2026, 1, 11),
            rows=[row for row in rows if row.dataset_key == dataset_key],
        )
    db.commit()

    audit = client.get(f"/api/v1/clients/{created.id}/analytics/ahrefs/site-audit")
    affected = client.get(
        f"/api/v1/clients/{created.id}/analytics/ahrefs/affected-urls",
        params={"issue_slug": "404_page"},
    )

    assert audit.status_code == 200
    assert audit.json()["project_id"] == "123"
    assert audit.json()["issues"][0]["how_to_fix"] == "Redirect it"
    assert audit.json()["severity_counts"]["Error"] == 1
    assert affected.status_code == 200
    assert affected.json()["rows"][0]["details"] == {
        "HTTP code": "404",
        "Inlinks": "7",
    }
