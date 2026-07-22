"""`run_cell`: the "zero crashes" guarded boundary (PLAN §9.2/§9.3)."""

from __future__ import annotations

from app.db.repositories.cells import CellRepository
from app.db.repositories.jobs import JobRepository
from app.db.repositories.metrics import MetricRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.jobs.runner import CellHandle, run_cell
from app.schemas.enums import CellStatus, JobStatus, Provider


def _run(session_scope, job_id: str, cell_id: str, client_id: str, provider: Provider, **kwargs):
    handle = CellHandle(cell_id=cell_id, job_id=job_id, client_id=client_id, provider=provider)
    return run_cell(handle, session_scope=session_scope, lease_owner="w1", **kwargs)


def test_success_persists_snapshot_and_metrics_and_succeeds(
    session_scope, make_cell, register_fake
):
    register_fake(Provider.ahrefs)
    job_id, cell_id, client_id = make_cell(Provider.ahrefs, scenario="success")

    status = _run(session_scope, job_id, cell_id, client_id, Provider.ahrefs)

    assert status == CellStatus.succeeded
    with session_scope() as session:
        cell = CellRepository(session).get(cell_id)
        assert cell.status == CellStatus.succeeded
        assert cell.finished_at is not None
        job = JobRepository(session).get(job_id)
        assert job.status == JobStatus.succeeded
        assert job.progress_percent == 100.0
        snapshot = SnapshotRepository(session).latest_success(client_id, Provider.ahrefs)
        assert snapshot is not None
        # `history()` only returns current/snapshot-role rows by contract;
        # the fake adapter also emits a `previous`-role row for the same key.
        current_metrics = MetricRepository(session).history(client_id, "ahrefs_fake_metric")
        assert len(current_metrics) == 1


def test_partial_quality_maps_to_partial_cell_status(session_scope, make_cell, register_fake):
    register_fake(Provider.ahrefs)
    job_id, cell_id, client_id = make_cell(Provider.ahrefs, scenario="partial")

    status = _run(session_scope, job_id, cell_id, client_id, Provider.ahrefs)

    assert status == CellStatus.partial
    with session_scope() as session:
        # A lone `partial` cell still aggregates to job `succeeded` — the
        # job-level `partial` status is reserved for a *mix* of attempted
        # cells succeeding and hard-failing (PLAN §9.2), not a single cell's
        # own data-quality nuance (that nuance lives on the snapshot/metric).
        assert JobRepository(session).get(job_id).status == JobStatus.succeeded


def test_unavailable_quality_persists_snapshot_with_no_metrics(
    session_scope, make_cell, register_fake
):
    register_fake(Provider.gsc)
    job_id, cell_id, client_id = make_cell(Provider.gsc, scenario="unavailable")

    status = _run(session_scope, job_id, cell_id, client_id, Provider.gsc)

    assert status == CellStatus.partial  # attempted, no data — not a hard failure
    with session_scope() as session:
        snapshot = SnapshotRepository(session).latest_success(client_id, Provider.gsc)
        # `unavailable` is deliberately excluded from `_SUCCESS_QUALITIES`.
        assert snapshot is None


def test_adapter_exception_isolates_cell_as_failed_never_raises(
    session_scope, make_cell, register_fake
):
    register_fake(Provider.ahrefs)
    job_id, cell_id, client_id = make_cell(Provider.ahrefs, scenario="fail")

    status = _run(session_scope, job_id, cell_id, client_id, Provider.ahrefs)

    assert status == CellStatus.failed
    with session_scope() as session:
        cell = CellRepository(session).get(cell_id)
        assert cell.error_code == "RuntimeError"
        # message is the safe generic string, never the raw exception text
        assert "RuntimeError" != cell.error_message
        assert JobRepository(session).get(job_id).status == JobStatus.failed


def test_timeout_does_not_block_forever_and_marks_failed(session_scope, make_cell, register_fake):
    register_fake(Provider.ahrefs)
    job_id, cell_id, client_id = make_cell(Provider.ahrefs, scenario="timeout")

    status = _run(
        session_scope, job_id, cell_id, client_id, Provider.ahrefs, cell_timeout_seconds=1
    )

    assert status == CellStatus.failed
    with session_scope() as session:
        assert CellRepository(session).get(cell_id).error_code == "timeout"


def test_cancel_requested_mid_collect_marks_cell_cancelled(session_scope, make_cell, register_fake):
    register_fake(Provider.ahrefs)
    job_id, cell_id, client_id = make_cell(Provider.ahrefs, scenario="cancel")

    # Simulate a cancel request arriving while this cell is `running` — the
    # fake adapter polls `is_cancel_requested()` at its very first stage.
    with session_scope() as session:
        CellRepository(session).set_status(cell_id, CellStatus.cancel_requested)

    status = _run(session_scope, job_id, cell_id, client_id, Provider.ahrefs)

    assert status == CellStatus.cancelled


def test_missing_integration_is_not_configured_not_failed_not_crash(
    session_scope, make_cell, register_fake
):
    register_fake(Provider.semrush)
    job_id, cell_id, client_id = make_cell(Provider.semrush, no_integration=True)

    status = _run(session_scope, job_id, cell_id, client_id, Provider.semrush)

    assert status == CellStatus.not_configured
    with session_scope() as session:
        cell = CellRepository(session).get(cell_id)
        assert cell.error_code == "not_configured"
        # not_configured never counts as a failure at the job level.
        assert JobRepository(session).get(job_id).status == JobStatus.succeeded


def test_disabled_integration_is_not_configured(session_scope, make_cell, register_fake):
    register_fake(Provider.semrush)
    job_id, cell_id, client_id = make_cell(Provider.semrush, integration_enabled=False)

    status = _run(session_scope, job_id, cell_id, client_id, Provider.semrush)

    assert status == CellStatus.not_configured


def test_adapter_preflight_not_configured_reason_code_propagates(
    session_scope, make_cell, register_fake
):
    register_fake(Provider.semrush)
    job_id, cell_id, client_id = make_cell(Provider.semrush, scenario="not_configured")

    status = _run(session_scope, job_id, cell_id, client_id, Provider.semrush)

    assert status == CellStatus.not_configured
    with session_scope() as session:
        assert CellRepository(session).get(cell_id).error_code == "not_configured"


def test_unregistered_adapter_fails_cell_without_crashing(session_scope, make_cell):
    # No `register_fake` call: nothing is registered for `reporting_ninja`.
    job_id, cell_id, client_id = make_cell(Provider.reporting_ninja)

    status = _run(session_scope, job_id, cell_id, client_id, Provider.reporting_ninja)

    assert status == CellStatus.failed
    with session_scope() as session:
        assert CellRepository(session).get(cell_id).error_code == "adapter_not_registered"


def test_mixed_success_and_failure_across_a_batch_is_partial(session_scope, register_fake):
    from datetime import date

    from app.db.models import Client, ClientIntegration
    from app.schemas.enums import ComparisonMode, PeriodKind

    register_fake(Provider.ahrefs)
    with session_scope() as session:
        good = Client(name="Good", slug="good", domain="good.com", timezone="America/New_York")
        bad = Client(name="Bad", slug="bad", domain="bad.com", timezone="America/New_York")
        session.add_all([good, bad])
        session.flush()
        session.add_all(
            [
                ClientIntegration(
                    client_id=good.id,
                    provider=Provider.ahrefs,
                    enabled=True,
                    config_json={"scenario": "success"},
                ),
                ClientIntegration(
                    client_id=bad.id,
                    provider=Provider.ahrefs,
                    enabled=True,
                    config_json={"scenario": "fail"},
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
        good_cell = CellRepository(session).list_for_job(good_job_id)[0]
        bad_cell = CellRepository(session).list_for_job(bad_job_id)[0]

    _run(session_scope, good_job_id, good_cell.id, good.id, Provider.ahrefs)
    _run(session_scope, bad_job_id, bad_cell.id, bad.id, Provider.ahrefs)

    with session_scope() as session:
        assert JobRepository(session).get(good_job_id).status == JobStatus.succeeded
        assert JobRepository(session).get(bad_job_id).status == JobStatus.failed
        assert JobRepository(session).aggregate_status(batch_id=batch_id) == JobStatus.partial
