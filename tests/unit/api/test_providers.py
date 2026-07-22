from __future__ import annotations

from datetime import UTC, date, datetime

from app.db.repositories.cells import CellRepository
from app.db.repositories.clients import ClientRepository
from app.db.repositories.jobs import JobRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.schemas.enums import CellStatus, ComparisonMode, PeriodKind, Provider, Quality, SourceMode


def test_providers_status_not_configured_for_new_client(client, db):
    ClientRepository(db).create(
        name="NewCo", slug="newco", domain="newco.com", timezone="America/New_York"
    )
    db.commit()

    response = client.get("/api/v1/providers/status")
    assert response.status_code == 200
    body = response.json()
    assert len(body["cells"]) == len(Provider)
    assert all(c["configured"] is False for c in body["cells"])
    assert all(c["state"] == "not_configured" for c in body["cells"])
    # Never a secret in an ops-facing status response.
    assert "api_key" not in str(body)


def test_providers_status_reports_failed_cell(client, db):
    clients = ClientRepository(db)
    c = clients.create(name="Acme", slug="acme", domain="acme.com", timezone="America/New_York")
    clients.upsert_integration(c.id, Provider.ahrefs, enabled=True, source_mode=SourceMode.api)
    jobs = JobRepository(db).create_run(
        client_ids=[c.id],
        providers=[Provider.ahrefs],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 12),
    )
    cell = CellRepository(db).list_for_job(jobs[0].id)[0]
    CellRepository(db).set_status(cell.id, CellStatus.failed, error_code="timeout")
    db.commit()

    response = client.get("/api/v1/providers/status")
    ahrefs_row = next(
        r
        for r in response.json()["cells"]
        if r["provider"] == Provider.ahrefs.value and r["client_id"] == c.id
    )
    assert ahrefs_row["configured"] is True
    assert ahrefs_row["state"] == "failed"
    assert ahrefs_row["reason_code"] == "timeout"


def test_providers_status_reports_configured_with_snapshot(client, db):
    clients = ClientRepository(db)
    c = clients.create(name="Acme", slug="acme2", domain="acme2.com", timezone="America/New_York")
    clients.upsert_integration(c.id, Provider.gsc, enabled=True, source_mode=SourceMode.api)
    jobs = JobRepository(db).create_run(
        client_ids=[c.id],
        providers=[Provider.gsc],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 12),
    )
    SnapshotRepository(db).create_immutable(
        job_id=jobs[0].id,
        client_id=c.id,
        provider=Provider.gsc,
        source_mode=SourceMode.api,
        observed_at=datetime.now(UTC),
        quality=Quality.complete,
    )
    db.commit()

    response = client.get("/api/v1/providers/status")
    row = next(
        r
        for r in response.json()["cells"]
        if r["provider"] == Provider.gsc.value and r["client_id"] == c.id
    )
    assert row["state"] == "complete"
