"""`app.jobs.control`: cancel / retry-failed-only orchestration (PLAN §9.2/§9.3)."""

from __future__ import annotations

from app.db.repositories.cells import CellRepository
from app.db.repositories.jobs import JobRepository
from app.jobs.control import cancel_run, retry_failed_only
from app.jobs.runner import CellHandle, run_cell
from app.schemas.enums import CellStatus, JobStatus, Provider


def test_cancel_run_flags_queued_cell_and_job(session_scope, make_cell):
    job_id, cell_id, _client_id = make_cell(Provider.ahrefs)

    status = cancel_run(session_scope, job_id=job_id)

    assert status == JobStatus.queued  # transient: not yet claimed by any lane
    with session_scope() as session:
        assert CellRepository(session).get(cell_id).status == CellStatus.cancel_requested
        assert JobRepository(session).get(job_id).status == JobStatus.cancel_requested


def test_cancel_run_never_touches_already_succeeded_cells(session_scope, make_cell, register_fake):
    register_fake(Provider.ahrefs)
    job_id, cell_id, client_id = make_cell(Provider.ahrefs, scenario="success")
    run_cell(
        CellHandle(cell_id=cell_id, job_id=job_id, client_id=client_id, provider=Provider.ahrefs),
        session_scope=session_scope,
        lease_owner="w1",
    )

    cancel_run(session_scope, job_id=job_id)

    with session_scope() as session:
        assert CellRepository(session).get(cell_id).status == CellStatus.succeeded


def test_retry_failed_only_requeues_failed_and_leaves_succeeded_alone(session_scope, register_fake):
    from datetime import date

    from app.db.models import Client, ClientIntegration

    register_fake(Provider.ahrefs)
    from app.schemas.enums import ComparisonMode, PeriodKind

    with session_scope() as session:
        good = Client(name="Good", slug="good", domain="good.com", timezone="America/New_York")
        bad = Client(name="Bad", slug="bad", domain="bad.com", timezone="America/New_York")
        session.add_all([good, bad])
        session.flush()
        session.add_all(
            [
                ClientIntegration(
                    client_id=good.id, provider=Provider.ahrefs, config_json={"scenario": "success"}
                ),
                ClientIntegration(
                    client_id=bad.id, provider=Provider.ahrefs, config_json={"scenario": "fail"}
                ),
            ]
        )
        jobs = JobRepository(session).create_run(
            client_ids=[good.id, bad.id],
            providers=[Provider.ahrefs],
            period_kind=PeriodKind.week,
            comparison_mode=ComparisonMode.closed,
            anchor_date=date(2026, 1, 1),
        )
        batch_id = jobs[0].batch_id
        good_job_id, bad_job_id = jobs[0].id, jobs[1].id
        good_cell_id = CellRepository(session).list_for_job(good_job_id)[0].id
        bad_cell_id = CellRepository(session).list_for_job(bad_job_id)[0].id

    run_cell(
        CellHandle(
            cell_id=good_cell_id, job_id=good_job_id, client_id=good.id, provider=Provider.ahrefs
        ),
        session_scope=session_scope,
        lease_owner="w1",
    )
    run_cell(
        CellHandle(
            cell_id=bad_cell_id, job_id=bad_job_id, client_id=bad.id, provider=Provider.ahrefs
        ),
        session_scope=session_scope,
        lease_owner="w1",
    )

    requeued = retry_failed_only(session_scope, batch_id=batch_id)

    assert requeued == 1
    with session_scope() as session:
        assert CellRepository(session).get(good_cell_id).status == CellStatus.succeeded
        bad_cell = CellRepository(session).get(bad_cell_id)
        assert bad_cell.status == CellStatus.queued
        assert bad_cell.attempt == 2
        assert JobRepository(session).get(bad_job_id).status == JobStatus.queued
