from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import get_settings
from app.db.models import Client
from app.db.repositories.reports import ReportRepository
from app.reports import scheduler as scheduler_module
from app.reports.scheduler import ReportScheduler


def _seed_client(temp_db) -> str:
    with temp_db.session_scope() as session:
        client = Client(name="EFFYS", slug="effys", domain="effys.example.com")
        session.add(client)
        session.flush()
        return client.id


def _queue_run(temp_db, client_id: str, *, source: str, batch_id: str | None = None) -> str:
    with temp_db.session_scope() as session:
        run = ReportRepository(session).create_run(
            client_id, kind="monthly", source=source, params={}, period_count=6,
            batch_id=batch_id,
        )
        return run.id


@pytest.fixture
def scheduler(temp_db, monkeypatch) -> ReportScheduler:
    """A scheduler whose pools never actually execute anything — these tests
    check the synchronous claim/capacity logic in `_fill`, not the
    submit-then-run race (covered separately, with real blocking, by
    `test_a_second_pdf_run_waits_while_the_first_is_genuinely_in_flight`).
    """
    settings = get_settings()
    instance = ReportScheduler(session_scope=temp_db.session_scope, settings=settings)
    instance._api_pool.submit = lambda *a, **k: None  # type: ignore[method-assign]
    instance._batch_api_pool.submit = lambda *a, **k: None  # type: ignore[method-assign]
    instance._pdf_pool.submit = lambda *a, **k: None  # type: ignore[method-assign]
    yield instance
    instance.stop()


def _status_of(temp_db, run_id: str) -> str:
    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
        assert run is not None
        return run.status


def test_pdf_capacity_is_hard_capped_at_one(temp_db, scheduler):
    client_id = _seed_client(temp_db)
    first = _queue_run(temp_db, client_id, source="pdf")
    second = _queue_run(temp_db, client_id, source="pdf")

    scheduler._fill("pdf", scheduler._in_flight_pdf, scheduler._pdf_capacity, scheduler._pdf_pool)

    statuses = {_status_of(temp_db, first), _status_of(temp_db, second)}
    assert statuses == {"running", "queued"}


def test_the_oldest_queued_run_is_claimed_first(temp_db, scheduler):
    client_id = _seed_client(temp_db)
    older = _queue_run(temp_db, client_id, source="api")
    _queue_run(temp_db, client_id, source="api")  # newer

    scheduler._fill(
        "api", scheduler._in_flight_api, 1, scheduler._api_pool
    )  # pretend capacity is 1 for this check

    assert _status_of(temp_db, older) == "running"


def test_api_capacity_allows_several_concurrent_claims(temp_db, scheduler):
    client_id = _seed_client(temp_db)
    run_ids = [_queue_run(temp_db, client_id, source="api") for _ in range(3)]
    scheduler._api_capacity = 3

    scheduler._fill("api", scheduler._in_flight_api, scheduler._api_capacity, scheduler._api_pool)

    assert all(_status_of(temp_db, r) == "running" for r in run_ids)


def test_a_second_pdf_run_waits_while_the_first_is_genuinely_in_flight(temp_db, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def blocking_run_report(session_scope, settings, run_id):
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(scheduler_module, "run_report", blocking_run_report)
    settings = get_settings()
    instance = ReportScheduler(session_scope=temp_db.session_scope, settings=settings)
    try:
        client_id = _seed_client(temp_db)
        first = _queue_run(temp_db, client_id, source="pdf")
        second = _queue_run(temp_db, client_id, source="pdf")

        instance._fill("pdf", instance._in_flight_pdf, instance._pdf_capacity, instance._pdf_pool)
        assert started.wait(timeout=2), "the claimed run never started executing"

        # capacity is still exhausted while the first run blocks in run_report
        instance._fill("pdf", instance._in_flight_pdf, instance._pdf_capacity, instance._pdf_pool)
        assert _status_of(temp_db, second) == "queued"
        assert _status_of(temp_db, first) == "running"
    finally:
        release.set()
        instance.stop()


def test_recovery_fails_a_running_run_with_no_live_owner(temp_db, scheduler):
    client_id = _seed_client(temp_db)
    run_id = _queue_run(temp_db, client_id, source="api")
    stale_cutoff = datetime.now(UTC) - timedelta(minutes=999)
    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
        run.status = "running"
        run.started_at = stale_cutoff
        run.heartbeat_at = stale_cutoff

    scheduler._recover_stale_runs()

    with temp_db.session_scope() as session:
        run = ReportRepository(session).get(run_id)
    assert run.status == "failed"
    assert run.error_code == "interrupted"


def test_batch_and_standalone_api_pools_never_compete_for_the_same_slot(temp_db, scheduler):
    """task-47 §2.3: a batch monopolizing every `reports_api_workers` slot
    must not queue an unrelated ad-hoc single-client report behind it.
    """
    client_id = _seed_client(temp_db)
    with temp_db.session_scope() as session:
        batch = ReportRepository(session).create_batch(
            kind="monthly", source="api", params={}, client_count=1
        )
        batch_id = batch.id
    scheduler._batch_api_capacity = 1
    scheduler._api_capacity = 1

    batch_child = _queue_run(temp_db, client_id, source="api", batch_id=batch_id)
    standalone = _queue_run(temp_db, client_id, source="api")

    scheduler._fill(
        "api", scheduler._in_flight_batch_api, scheduler._batch_api_capacity,
        scheduler._batch_api_pool, batch_only=True,
    )
    scheduler._fill(
        "api", scheduler._in_flight_api, scheduler._api_capacity,
        scheduler._api_pool, batch_only=False,
    )

    assert _status_of(temp_db, batch_child) == "running"
    assert _status_of(temp_db, standalone) == "running"


def test_disabled_scheduler_never_starts_a_background_thread(temp_db, monkeypatch):
    monkeypatch.setattr(scheduler_module, "run_report", lambda *a, **k: None)
    settings = get_settings()
    settings.reports_enabled = False
    instance = ReportScheduler(session_scope=temp_db.session_scope, settings=settings)
    instance.start()
    try:
        assert instance._thread is None
    finally:
        instance.stop()
