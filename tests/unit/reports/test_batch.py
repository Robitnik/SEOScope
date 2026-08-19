from __future__ import annotations

import pytest

from app.db.models import Client
from app.db.repositories.reports import ReportRepository
from app.reports.batch import (
    NoClientsSelectedError,
    cancel_batch,
    create_batch,
    latest_runs_by_client,
    rebuild_run,
    reconcile_batches,
)


def _seed_clients(temp_db, n: int) -> list[str]:
    with temp_db.session_scope() as session:
        ids = []
        for i in range(n):
            client = Client(name=f"CLIENT{i}", slug=f"client{i}", domain=f"client{i}.example.com")
            session.add(client)
            session.flush()
            ids.append(client.id)
        return ids


def _make_batch(temp_db, client_ids: list[str]):
    with temp_db.session_scope() as session:
        return create_batch(
            session, client_ids=client_ids, kind="monthly", source="api", params={},
            max_months=24, max_comparator_years=4,
        )


def _set_status(temp_db, run_id: str, status: str) -> None:
    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
        run.status = status


def test_create_batch_fans_out_one_run_per_client(temp_db):
    client_ids = _seed_clients(temp_db, 3)
    batch = _make_batch(temp_db, client_ids)

    with temp_db.session_scope() as session:
        children = ReportRepository(session).list_runs_for_batch(batch.id)
    assert len(children) == 3
    assert {c.client_id for c in children} == set(client_ids)
    assert all(c.status == "queued" for c in children)
    assert all(c.period_count == 6 for c in children)  # monthly = 6 periods


def test_create_batch_rejects_an_empty_selection(temp_db):
    with temp_db.session_scope() as session:
        with pytest.raises(NoClientsSelectedError):
            create_batch(
                session, client_ids=[], kind="monthly", source="api", params={},
                max_months=24, max_comparator_years=4,
            )


def test_a_client_removed_before_submit_is_skipped_not_fatal(temp_db):
    client_ids = _seed_clients(temp_db, 2)
    batch = _make_batch(temp_db, [*client_ids, "does-not-exist"])

    with temp_db.session_scope() as session:
        children = ReportRepository(session).list_runs_for_batch(batch.id)
        refreshed = ReportRepository(session).get_batch(batch.id)
    assert len(children) == 2
    assert refreshed.client_count == 2


def test_one_child_failing_leaves_the_batch_partial_not_failed(temp_db):
    client_ids = _seed_clients(temp_db, 3)
    batch = _make_batch(temp_db, client_ids)
    with temp_db.session_scope() as session:
        children = ReportRepository(session).list_runs_for_batch(batch.id)
    _set_status(temp_db, children[0].id, "succeeded")
    _set_status(temp_db, children[1].id, "succeeded")
    _set_status(temp_db, children[2].id, "failed")

    reconcile_batches(temp_db.session_scope)

    with temp_db.session_scope() as session:
        refreshed = ReportRepository(session).get_batch(batch.id)
    assert refreshed.status == "partial"
    assert refreshed.clients_done == 2
    assert refreshed.clients_failed == 1


def test_every_child_failing_fails_the_whole_batch(temp_db):
    client_ids = _seed_clients(temp_db, 2)
    batch = _make_batch(temp_db, client_ids)
    with temp_db.session_scope() as session:
        children = ReportRepository(session).list_runs_for_batch(batch.id)
    for child in children:
        _set_status(temp_db, child.id, "failed")

    reconcile_batches(temp_db.session_scope)

    with temp_db.session_scope() as session:
        refreshed = ReportRepository(session).get_batch(batch.id)
    assert refreshed.status == "failed"


def test_batch_moves_to_running_as_soon_as_one_child_starts(temp_db):
    client_ids = _seed_clients(temp_db, 2)
    batch = _make_batch(temp_db, client_ids)
    with temp_db.session_scope() as session:
        children = ReportRepository(session).list_runs_for_batch(batch.id)
    _set_status(temp_db, children[0].id, "running")

    reconcile_batches(temp_db.session_scope)

    with temp_db.session_scope() as session:
        refreshed = ReportRepository(session).get_batch(batch.id)
    assert refreshed.status == "running"
    assert refreshed.started_at is not None


def test_cancelling_a_batch_cancels_a_queued_child_immediately(temp_db):
    client_ids = _seed_clients(temp_db, 2)
    batch = _make_batch(temp_db, client_ids)

    error = cancel_batch(temp_db.session_scope, batch.id)

    assert error is None
    with temp_db.session_scope() as session:
        children = ReportRepository(session).list_runs_for_batch(batch.id)
    assert all(c.status == "cancelled" for c in children)


def test_cancelling_a_batch_sets_cancel_requested_on_a_running_child(temp_db):
    client_ids = _seed_clients(temp_db, 1)
    batch = _make_batch(temp_db, client_ids)
    with temp_db.session_scope() as session:
        child = ReportRepository(session).list_runs_for_batch(batch.id)[0]
    _set_status(temp_db, child.id, "running")

    cancel_batch(temp_db.session_scope, batch.id)

    with temp_db.session_scope() as session:
        refreshed = ReportRepository(session).get(child.id)
    assert refreshed.status == "running"  # untouched — the runner's own loop stops it
    assert refreshed.cancel_requested is True


def test_a_cancelled_batch_reports_cancelled_even_with_a_finished_client(temp_db):
    """task-47 DoD: "Скасування батчу … вже готові звіти лишаються
    готовими" — a finished child KEEPS its own `succeeded` status, but the
    batch as a whole is honestly labelled cancelled, the same way one
    standalone run's own `is_cancelled()` short-circuit already works.
    """
    client_ids = _seed_clients(temp_db, 2)
    batch = _make_batch(temp_db, client_ids)
    with temp_db.session_scope() as session:
        children = ReportRepository(session).list_runs_for_batch(batch.id)
    _set_status(temp_db, children[0].id, "succeeded")

    cancel_batch(temp_db.session_scope, batch.id)  # cancels the still-queued second child
    reconcile_batches(temp_db.session_scope)

    with temp_db.session_scope() as session:
        refreshed = ReportRepository(session).get_batch(batch.id)
        first_child = ReportRepository(session).get(children[0].id)
    assert refreshed.status == "cancelled"
    assert first_child.status == "succeeded"


def test_cancel_unknown_batch_returns_not_found(temp_db):
    error = cancel_batch(temp_db.session_scope, "does-not-exist")
    assert error == "batch_not_found"


def test_cancel_an_already_finished_batch_is_rejected(temp_db):
    client_ids = _seed_clients(temp_db, 1)
    batch = _make_batch(temp_db, client_ids)
    with temp_db.session_scope() as session:
        b = ReportRepository(session).get_batch(batch.id)
        b.status = "succeeded"

    error = cancel_batch(temp_db.session_scope, batch.id)

    assert error == "batch_not_cancellable"


def test_recovery_reconciles_a_batch_stuck_running_after_a_restart(temp_db):
    """task-47 DoD: a worker restart mid-batch must not leave the batch
    'running' forever — `ReportScheduler._recover_stale_runs` calls
    `reconcile_batches` for exactly this reason.
    """
    client_ids = _seed_clients(temp_db, 2)
    batch = _make_batch(temp_db, client_ids)
    with temp_db.session_scope() as session:
        children = ReportRepository(session).list_runs_for_batch(batch.id)
    for child in children:
        _set_status(temp_db, child.id, "failed")  # e.g. recovered as "interrupted" on restart

    reconcile_batches(temp_db.session_scope)

    with temp_db.session_scope() as session:
        refreshed = ReportRepository(session).get_batch(batch.id)
    assert refreshed.status == "failed"
    assert refreshed.finished_at is not None


# --- task-49: batch-internal retry/rebuild without an orphan ----------------


def test_latest_runs_by_client_keeps_only_the_newest_row_per_client(temp_db):
    client_ids = _seed_clients(temp_db, 2)
    batch = _make_batch(temp_db, client_ids)
    with temp_db.session_scope() as session:
        original = ReportRepository(session).list_runs_for_batch(batch.id)
    _set_status(temp_db, original[0].id, "failed")

    error, new_run_id = rebuild_run(
        temp_db.session_scope, original[0].id, force_refresh=False,
        max_months=24, max_comparator_years=4,
    )
    assert error is None

    with temp_db.session_scope() as session:
        children = ReportRepository(session).list_runs_for_batch(batch.id)
    latest = latest_runs_by_client(children)
    assert len(latest) == 2  # never 3, even though there are 3 rows in the DB
    assert latest[original[0].client_id].id == new_run_id
    assert latest[original[1].client_id].id == original[1].id


def test_rebuild_run_preserves_batch_id_and_overrides_force_refresh(temp_db):
    client_ids = _seed_clients(temp_db, 1)
    batch = _make_batch(temp_db, client_ids)
    with temp_db.session_scope() as session:
        original = ReportRepository(session).list_runs_for_batch(batch.id)[0]
    _set_status(temp_db, original.id, "failed")

    error, new_run_id = rebuild_run(
        temp_db.session_scope, original.id, force_refresh=True,
        max_months=24, max_comparator_years=4,
    )
    assert error is None

    with temp_db.session_scope() as session:
        new_run = ReportRepository(session).get(new_run_id)
    assert new_run.batch_id == batch.id
    assert new_run.params_json["force_refresh"] is True
    assert new_run.status == "queued"


def test_rebuild_run_reopens_a_finished_batch(temp_db):
    client_ids = _seed_clients(temp_db, 2)
    batch = _make_batch(temp_db, client_ids)
    with temp_db.session_scope() as session:
        children = ReportRepository(session).list_runs_for_batch(batch.id)
    _set_status(temp_db, children[0].id, "succeeded")
    _set_status(temp_db, children[1].id, "failed")
    reconcile_batches(temp_db.session_scope)
    with temp_db.session_scope() as session:
        finished = ReportRepository(session).get_batch(batch.id)
    assert finished.status == "partial"
    assert finished.finished_at is not None

    error, new_run_id = rebuild_run(
        temp_db.session_scope, children[1].id, force_refresh=False,
        max_months=24, max_comparator_years=4,
    )
    assert error is None

    with temp_db.session_scope() as session:
        reopened = ReportRepository(session).get_batch(batch.id)
    assert reopened.status == "running"
    assert reopened.finished_at is None

    # ... and the retried client can bring the batch all the way to succeeded,
    # with no orphan run left dangling outside it (task-49 §2.4).
    _set_status(temp_db, new_run_id, "succeeded")
    reconcile_batches(temp_db.session_scope)
    with temp_db.session_scope() as session:
        final = ReportRepository(session).get_batch(batch.id)
    assert final.status == "succeeded"
    assert final.clients_done == 2
    assert final.clients_failed == 0


def test_rebuild_run_rejects_an_unknown_run(temp_db):
    error, new_run_id = rebuild_run(
        temp_db.session_scope, "does-not-exist", force_refresh=False,
        max_months=24, max_comparator_years=4,
    )
    assert error == "run_not_found"
    assert new_run_id is None


def test_rebuild_run_rejects_a_run_still_in_progress(temp_db):
    client_ids = _seed_clients(temp_db, 1)
    batch = _make_batch(temp_db, client_ids)
    with temp_db.session_scope() as session:
        run_id = ReportRepository(session).list_runs_for_batch(batch.id)[0].id
    _set_status(temp_db, run_id, "running")

    error, new_run_id = rebuild_run(
        temp_db.session_scope, run_id, force_refresh=False,
        max_months=24, max_comparator_years=4,
    )
    assert error == "run_not_retryable"
    assert new_run_id is None
