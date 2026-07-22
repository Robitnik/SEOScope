"""Startup recovery: no cell left `running` forever after a restart (PLAN §9.2)."""

from __future__ import annotations

from app.db.repositories.cells import CellRepository
from app.db.repositories.events import EventRepository
from app.jobs.recovery import run_startup_recovery
from app.schemas.enums import CellStatus, Provider


def test_expired_running_lease_is_requeued_after_recording_interruption(session_scope, make_cell):
    job_id, cell_id, _client_id = make_cell(Provider.ahrefs)
    with session_scope() as session:
        CellRepository(session).claim_next_cell(
            Provider.ahrefs, lease_owner="dead-worker", lease_ttl_seconds=-10
        )

    report = run_startup_recovery(session_scope)

    assert report.requeued_interrupted == 1
    with session_scope() as session:
        cell = CellRepository(session).get(cell_id)
        assert cell.status == CellStatus.queued
        assert cell.attempt == 2  # requeue_for_retry bumped it
        events = EventRepository(session).since(job_id, 0)
        assert any("interrupted" in e.message for e in events)


def test_no_eternal_running_cells_survive_recovery(session_scope, make_cell):
    make_cell(Provider.ahrefs)
    with session_scope() as session:
        CellRepository(session).claim_next_cell(
            Provider.ahrefs, lease_owner="dead-worker", lease_ttl_seconds=-10
        )

    run_startup_recovery(session_scope)

    with session_scope() as session:
        assert CellRepository(session).expired_cells() == []


def test_stale_cancel_requested_is_finalized_cancelled(session_scope, make_cell):
    _job_id, cell_id, _client_id = make_cell(Provider.semrush)
    with session_scope() as session:
        CellRepository(session).set_status(cell_id, CellStatus.cancel_requested)

    report = run_startup_recovery(session_scope)

    assert report.finalized_cancelled == 1
    with session_scope() as session:
        assert CellRepository(session).get(cell_id).status == CellStatus.cancelled


def test_recovery_is_a_no_op_when_nothing_is_stuck(session_scope, make_cell):
    make_cell(Provider.ahrefs)  # stays `queued`, never claimed

    report = run_startup_recovery(session_scope)

    assert report.requeued_interrupted == 0
    assert report.finalized_cancelled == 0
