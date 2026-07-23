"""Technical SEO Audit endpoints (task-25) — read side, run/recheck side, export.

Everything here is machine data: the tests seed `tech_categories` /
`tech_findings` / `tech_recheck` dataset rows exactly as `TechAuditAdapter`
writes them and assert the API joins them the way the UI needs.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.db.repositories.analytics_datasets import (
    AnalyticsDatasetRepository,
    AnalyticsDatasetRowInput,
)
from app.db.repositories.clients import ClientRepository
from app.db.repositories.jobs import JobRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.integrations.tech_audit import finding_key
from app.schemas.enums import ComparisonMode, PeriodKind, Provider, Quality, SourceMode

RUN_DIR = "tech_audit/client/run-1"
URL_404 = "https://tech.example.com/missing"
URL_ORPHAN = "https://tech.example.com/lonely"


def _client(db, *, slug: str = "acme-tech"):
    return ClientRepository(db).create(
        name="Acme Tech",
        slug=slug,
        domain="tech.example.com",
        timezone="America/New_York",
    )


def _snapshot(db, created, *, provenance: dict, observed: datetime, warnings=None):
    job = JobRepository(db).create_run(
        client_ids=[created.id],
        providers=[Provider.tech_audit],
        period_kind=PeriodKind.month,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 7, 23),
        mode="tech_audit",
    )[0]
    return SnapshotRepository(db).create_immutable(
        job_id=job.id,
        client_id=created.id,
        provider=Provider.tech_audit,
        source_mode=SourceMode.auto,
        observed_at=observed,
        quality=Quality.complete,
        warnings=warnings or [],
        provenance={"analytics_datasets": provenance},
    )


def _insert(db, created, snapshot, dataset_key: str, rows: list[AnalyticsDatasetRowInput]):
    AnalyticsDatasetRepository(db).bulk_insert(
        snapshot_id=snapshot.id,
        client_id=created.id,
        provider=Provider.tech_audit,
        period_start=date(2026, 7, 23),
        period_end=date(2026, 7, 23),
        rows=rows,
    )


def _seed_run(db, *, run_dir: str = RUN_DIR):
    created = _client(db)
    snapshot = _snapshot(
        db,
        created,
        observed=datetime(2026, 7, 23, 12, tzinfo=UTC),
        warnings=["sitemap points at another host"],
        provenance={
            "attempted": True,
            "run_dir": run_dir,
            "health_score": 88,
            "cms": "WordPress",
            "pages_crawled": 42,
            "audit_date": "2026-07-23",
            "totals": {"urls_crawled": 42, "errors": 1},
        },
    )
    _insert(
        db,
        created,
        snapshot,
        "tech_categories",
        [
            AnalyticsDatasetRowInput(
                dataset_key="tech_categories",
                dimension_key="category",
                dimension_value="http_errors",
                metrics={
                    "name": "HTTP errors (4XX/5XX)",
                    "severity": "Error",
                    "priority": "High",
                    "url_count": 1,
                    "finding_count": 1,
                    "description": "",
                    "how_to_fix": "Fix or 301 the URL.",
                },
                quality=Quality.complete,
                rank=1,
            ),
            AnalyticsDatasetRowInput(
                dataset_key="tech_categories",
                dimension_key="category",
                dimension_value="orphan",
                metrics={
                    "name": "Orphan pages",
                    "severity": "Notice",
                    "priority": "Low",
                    "url_count": 1,
                    "finding_count": 1,
                    "how_to_fix": "Add contextual internal links.",
                },
                quality=Quality.complete,
                rank=2,
            ),
        ],
    )
    _insert(
        db,
        created,
        snapshot,
        "tech_findings",
        [
            AnalyticsDatasetRowInput(
                dataset_key="tech_findings",
                dimension_key="finding",
                dimension_value=finding_key("page_4xx", URL_404),
                metrics={
                    "slug": "page_4xx",
                    "category": "http_errors",
                    "category_name": "HTTP errors (4XX/5XX)",
                    "name": "Page returns 4XX",
                    "severity": "Error",
                    "url": URL_404,
                    "measured": {"status": 404},
                    "how_to_fix": "Fix or 301 the URL.",
                    "live_status": "404",
                },
                quality=Quality.complete,
                rank=1,
            ),
            AnalyticsDatasetRowInput(
                dataset_key="tech_findings",
                dimension_key="finding",
                dimension_value=finding_key("orphan_page", URL_ORPHAN),
                metrics={
                    "slug": "orphan_page",
                    "category": "orphan",
                    "category_name": "Orphan pages",
                    "name": "Orphan page",
                    "severity": "Notice",
                    "url": URL_ORPHAN,
                    "measured": {"inbound_count": 0},
                },
                quality=Quality.complete,
                rank=2,
            ),
        ],
    )
    db.commit()
    return created


def test_latest_reports_no_run_for_a_fresh_client(client, db):
    created = _client(db, slug="fresh-tech")
    db.commit()

    body = client.get(f"/api/v1/clients/{created.id}/tech-audit/latest").json()

    assert body["has_run"] is False
    assert body["categories"] == []
    assert body["health_score"] is None  # never a fabricated 0


def test_latest_returns_categories_and_crawl_summary(client, db):
    created = _seed_run(db)

    body = client.get(f"/api/v1/clients/{created.id}/tech-audit/latest").json()

    assert body["has_run"] is True
    assert body["health_score"] == 88
    assert body["cms"] == "WordPress"
    assert body["pages_crawled"] == 42
    assert body["totals"]["urls_crawled"] == 42
    assert body["warnings"] == ["sitemap points at another host"]
    categories = {c["category"]: c for c in body["categories"]}
    assert categories["http_errors"]["severity"] == "Error"
    assert categories["http_errors"]["recheckable"] is True
    # Site-graph scoped: a single-page recheck can never judge it honestly.
    assert categories["orphan"]["recheckable"] is False


def test_findings_filter_paginate_and_join_latest_recheck(client, db):
    created = _seed_run(db)
    # A later, separate recheck run touching only the 4XX finding.
    recheck_snapshot = _snapshot(
        db,
        created,
        observed=datetime(2026, 7, 24, 9, tzinfo=UTC),
        provenance={"attempted": True, "run_dir": RUN_DIR, "rechecked": 1},
    )
    _insert(
        db,
        created,
        recheck_snapshot,
        "tech_recheck",
        [
            AnalyticsDatasetRowInput(
                dataset_key="tech_recheck",
                dimension_key="finding",
                dimension_value=finding_key("page_4xx", URL_404),
                metrics={
                    "slug": "page_4xx",
                    "url": URL_404,
                    "status": "fixed",
                    "detail": {"live": 200},
                    "checked_at": "2026-07-24T09:00:00+00:00",
                },
                quality=Quality.complete,
                rank=1,
            )
        ],
    )
    db.commit()

    all_rows = client.get(f"/api/v1/clients/{created.id}/tech-audit/findings").json()
    assert all_rows["total"] == 2

    scoped = client.get(
        f"/api/v1/clients/{created.id}/tech-audit/findings",
        params={"category": "http_errors"},
    ).json()
    assert scoped["total"] == 1
    row = scoped["rows"][0]
    assert row["url"] == URL_404
    assert row["measured"]["status"] == 404
    # The recheck lives in a *different* snapshot and still resolves onto its finding.
    assert row["recheck_status"] == "fixed"
    assert row["recheck_at"].startswith("2026-07-24")

    # The untouched finding keeps a null verdict — never "fixed" by association.
    orphan = client.get(
        f"/api/v1/clients/{created.id}/tech-audit/findings", params={"category": "orphan"}
    ).json()["rows"][0]
    assert orphan["recheck_status"] is None
    assert orphan["recheckable"] is False


def test_findings_search_matches_url(client, db):
    created = _seed_run(db)
    body = client.get(
        f"/api/v1/clients/{created.id}/tech-audit/findings", params={"q": "lonely"}
    ).json()
    assert body["total"] == 1
    assert body["rows"][0]["slug"] == "orphan_page"


def test_start_run_queues_a_tech_audit_job(client, db):
    created = _client(db, slug="run-tech")
    db.commit()

    response = client.post(f"/api/v1/clients/{created.id}/tech-audit/runs")

    assert response.status_code == 202
    body = response.json()
    assert body["events_url"] == f"/api/v1/sync-jobs/{body['job_id']}/events"
    job = JobRepository(db).get(body["job_id"])
    assert job is not None
    assert job.mode == "tech_audit"
    assert job.requested_providers_json == ["tech_audit"]


def test_start_run_404_for_unknown_client(client):
    response = client.post("/api/v1/clients/does-not-exist/tech-audit/runs")
    assert response.status_code == 404


def test_recheck_refuses_graph_scoped_findings(client, db):
    created = _seed_run(db)

    response = client.post(
        f"/api/v1/clients/{created.id}/tech-audit/findings/recheck",
        json={"findings": [{"slug": "orphan_page", "url": URL_ORPHAN}]},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "needs_full_rerun"


def test_recheck_queues_a_targeted_job_carrying_the_run_dir(client, db):
    created = _seed_run(db)

    response = client.post(
        f"/api/v1/clients/{created.id}/tech-audit/findings/recheck",
        json={
            "findings": [
                {"slug": "page_4xx", "url": URL_404},
                {"slug": "orphan_page", "url": URL_ORPHAN},  # dropped, graph-scoped
            ]
        },
    )

    assert response.status_code == 202
    job = JobRepository(db).get(response.json()["job_id"])
    assert job is not None
    assert job.mode == "tech_audit_recheck"
    assert job.params_json["run_dir"] == RUN_DIR
    assert job.params_json["findings"] == [{"slug": "page_4xx", "url": URL_404}]


def test_recheck_404_when_the_client_never_ran_an_audit(client, db):
    created = _client(db, slug="norun-tech")
    db.commit()

    response = client.post(
        f"/api/v1/clients/{created.id}/tech-audit/findings/recheck",
        json={"findings": [{"slug": "page_4xx", "url": URL_404}]},
    )

    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "no_audit_run"


def test_export_404_without_a_run(client, db):
    created = _client(db, slug="noexport-tech")
    db.commit()
    assert client.get(f"/api/v1/clients/{created.id}/tech-audit/export.xlsx").status_code == 404


def test_export_refuses_a_run_dir_escaping_the_artifacts_root(client, db):
    """Defence in depth: the stored path is ours, but it is still never trusted."""
    created = _seed_run(db, run_dir="../../../etc")

    response = client.get(f"/api/v1/clients/{created.id}/tech-audit/export.xlsx")

    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "no_audit_run"
