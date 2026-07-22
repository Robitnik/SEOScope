"""task-21: a dead session must surface as `auth_required`, never
`unavailable` — and, when there is still a last-successful snapshot, that
data must stay visible with only the badge flipped (PLAN §12).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.db.repositories.cells import CellRepository
from app.db.repositories.clients import ClientRepository
from app.db.repositories.jobs import JobRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.schemas.enums import CellStatus, ComparisonMode, PeriodKind, Provider, Quality, SourceMode


def _seed_cell(db, *, slug: str, provider: Provider = Provider.semrush) -> tuple[str, str]:
    clients = ClientRepository(db)
    client = clients.create(
        name=slug, slug=slug, domain=f"{slug}.com", timezone="America/New_York"
    )
    clients.upsert_integration(client.id, provider, enabled=True, source_mode=SourceMode.auto)
    job = JobRepository(db).create_run(
        client_ids=[client.id],
        providers=[provider],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 12),
    )[0]
    cell = CellRepository(db).list_for_job(job.id)[0]
    return client.id, cell.id


def test_dashboard_reports_auth_required_not_unavailable_with_no_prior_data(client, db):
    client_id, cell_id = _seed_cell(db, slug="auth-dead")
    CellRepository(db).set_status(
        cell_id, CellStatus.awaiting_auth, error_code="auth_required", error_message="paused"
    )
    db.commit()

    response = client.get("/api/v1/dashboard")
    card = next(item for item in response.json()["clients"] if item["client_id"] == client_id)
    semrush = next(p for p in card["providers"] if p["provider"] == Provider.semrush.value)

    assert semrush["state"] == "auth_required"
    assert semrush["last_error_code"] == "auth_required"


def test_dashboard_keeps_last_success_visible_next_to_auth_required_badge(client, db):
    client_id, _first_cell_id = _seed_cell(db, slug="auth-stale-good")
    job = JobRepository(db).create_run(
        client_ids=[client_id],
        providers=[Provider.semrush],
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 12),
    )[0]
    SnapshotRepository(db).create_immutable(
        job_id=job.id,
        client_id=client_id,
        provider=Provider.semrush,
        source_mode=SourceMode.browser,
        observed_at=datetime.now(UTC),
        quality=Quality.complete,
    )
    # The *latest* attempt (a different, later cell) found the session dead.
    later_cell = CellRepository(db).list_for_job(job.id)[0]
    CellRepository(db).set_status(
        later_cell.id, CellStatus.awaiting_auth, error_code="auth_required"
    )
    db.commit()

    response = client.get("/api/v1/dashboard")
    card = next(item for item in response.json()["clients"] if item["client_id"] == client_id)
    semrush = next(p for p in card["providers"] if p["provider"] == Provider.semrush.value)

    assert semrush["state"] == "auth_required"
    # The data itself is untouched — still the last-successful snapshot.
    assert semrush["quality"] == "complete"
    assert semrush["last_success_at"] is not None


def test_providers_status_reports_auth_required_for_awaiting_auth_cell(client, db):
    client_id, cell_id = _seed_cell(db, slug="auth-status")
    CellRepository(db).set_status(cell_id, CellStatus.awaiting_auth, error_code="auth_required")
    db.commit()

    response = client.get("/api/v1/providers/status")
    row = next(
        r
        for r in response.json()["cells"]
        if r["provider"] == Provider.semrush.value and r["client_id"] == client_id
    )
    assert row["state"] == "auth_required"
    assert row["reason_code"] == "auth_required"


def test_providers_status_timed_out_pause_still_reports_auth_required(client, db):
    """After a pause times out the cell is `failed`/`auth_required` (task-21
    lane timeout), not a plain generic `failed` — still distinguishable from
    an unrelated hard failure.
    """
    client_id, cell_id = _seed_cell(db, slug="auth-timed-out")
    CellRepository(db).set_status(
        cell_id, CellStatus.failed, error_code="auth_required", error_message="timed out"
    )
    db.commit()

    response = client.get("/api/v1/providers/status")
    row = next(
        r
        for r in response.json()["cells"]
        if r["provider"] == Provider.semrush.value and r["client_id"] == client_id
    )
    assert row["state"] == "auth_required"
