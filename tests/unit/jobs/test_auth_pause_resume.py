"""task-21: a dead session pauses only its own lane (`CellStatus.awaiting_auth`),
frees its profile lock for the duration, never lets its lease expire into
`failed_interrupted`, auto-resumes exactly where it left off after login, and
closes its backlog honestly if the operator never logs in.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, date, datetime

from app.db.models import Client, ClientIntegration
from app.db.repositories.cells import CellRepository
from app.db.repositories.jobs import JobRepository
from app.integrations.browser_session import SessionState
from app.jobs.auth_gate import AuthGate
from app.jobs.dispatcher import Dispatcher
from app.jobs.fake_adapter import FakeAdapter
from app.jobs.lane import run_lane
from app.jobs.recovery import run_startup_recovery
from app.jobs.slots import SlotPool
from app.schemas.adapter import CollectionResult
from app.schemas.enums import CellStatus, ComparisonMode, JobStatus, PeriodKind, Provider, Quality


def _wait_until(predicate, *, timeout: float = 3.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), f"condition not met within {timeout}s"


def _make_client(session_scope, provider: Provider, slug: str, *, profile: str = "warm") -> str:
    with session_scope() as session:
        client = Client(name=slug, slug=slug, domain=f"{slug}.com", timezone="America/New_York")
        session.add(client)
        session.flush()
        session.add(
            ClientIntegration(
                client_id=client.id,
                provider=provider,
                enabled=True,
                config_json={"scenario": "success", "profile": profile},
            )
        )
        job = JobRepository(session).create_run(
            client_ids=[client.id],
            providers=[provider],
            period_kind=PeriodKind.week,
            comparison_mode=ComparisonMode.closed,
            anchor_date=date(2026, 1, 1),
        )[0]
        return job.id


class _AuthOnceThenSucceedAdapter(FakeAdapter):
    """First `collect()` for `trigger_client_id` reports a dead session
    (mirrors Semrush's real `SessionExpired` -> `error_code="auth_required"`
    path, `app/integrations/semrush.py`); every other call, and every later
    call for that same client, succeeds normally.
    """

    def __init__(self, provider: Provider, trigger_client_id: str, login_event: threading.Event):
        super().__init__(provider)
        self._trigger_client_id = trigger_client_id
        self._login_event = login_event
        self._triggered = False
        self.collect_calls_by_client: dict[str, int] = {}

    def collect(self, context, emit, is_cancel_requested):  # type: ignore[no-untyped-def]
        self.collect_calls_by_client[context.client_id] = (
            self.collect_calls_by_client.get(context.client_id, 0) + 1
        )
        if context.client_id == self._trigger_client_id and not self._triggered:
            self._triggered = True
            self._login_event.clear()  # the session "just died"
            return CollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=datetime.now(UTC),
                metrics=[],
                warnings=["fake: session expired mid-batch"],
                error_code="auth_required",
            )
        return super().collect(context, emit, is_cancel_requested)


def test_resume_after_login_continues_without_recollecting_success(session_scope, register_fake):
    job_a = _make_client(session_scope, Provider.semrush, "auth-a")
    job_b = _make_client(session_scope, Provider.semrush, "auth-b")
    job_c = _make_client(session_scope, Provider.semrush, "auth-c")

    with session_scope() as session:
        client_b_id = JobRepository(session).get(job_b).client_id

    login_event = threading.Event()
    login_event.set()  # alive at the start, so client A collects normally
    adapter = _AuthOnceThenSucceedAdapter(Provider.semrush, client_b_id, login_event)
    register_fake(Provider.semrush, adapter)

    check_session = lambda _p: SessionState.alive if login_event.is_set() else SessionState.expired  # noqa: E731

    stop_event = threading.Event()
    thread = threading.Thread(
        target=run_lane,
        kwargs={
            "provider": Provider.semrush,
            "session_scope": session_scope,
            "lease_owner": "test-worker",
            "stop_event": stop_event,
            "lease_ttl_seconds": 60,
            "poll_interval_seconds": 0.02,
            "check_session": check_session,
            "auth_pause_timeout_seconds": 30,
        },
        daemon=True,
    )
    thread.start()
    try:

        def _b_paused() -> bool:
            with session_scope() as session:
                cell = CellRepository(session).list_for_job(job_b)[0]
                return cell.status == CellStatus.awaiting_auth

        _wait_until(_b_paused, timeout=3.0)

        with session_scope() as session:
            cell_a = CellRepository(session).list_for_job(job_a)[0]
            cell_c = CellRepository(session).list_for_job(job_c)[0]
            assert cell_a.status == CellStatus.succeeded
            assert cell_c.status == CellStatus.queued
            # The dead session's profile lock is free while paused.
            assert CellRepository(session).acquire_profile_lock("browser:warm") is True

        login_event.set()  # operator logs back in

        def _all_done() -> bool:
            with session_scope() as session:
                jobs = JobRepository(session)
                return (
                    jobs.get(job_a).status == JobStatus.succeeded
                    and jobs.get(job_b).status == JobStatus.succeeded
                    and jobs.get(job_c).status == JobStatus.succeeded
                )

        _wait_until(_all_done, timeout=3.0)
    finally:
        stop_event.set()
        thread.join(timeout=5)

    assert adapter.collect_calls_by_client[client_b_id] == 2  # never a third, un-needed attempt
    with session_scope() as session:
        client_a_id = JobRepository(session).get(job_a).client_id
    assert adapter.collect_calls_by_client[client_a_id] == 1  # success never re-collected


def test_auth_pause_timeout_closes_whole_backlog_honestly(session_scope, register_fake):
    register_fake(Provider.semrush)
    job_ids = [
        _make_client(session_scope, Provider.semrush, f"timeout-{i}", profile="warm")
        for i in range(3)
    ]

    stop_event = threading.Event()
    thread = threading.Thread(
        target=run_lane,
        kwargs={
            "provider": Provider.semrush,
            "session_scope": session_scope,
            "lease_owner": "test-worker",
            "stop_event": stop_event,
            "lease_ttl_seconds": 60,
            "poll_interval_seconds": 0.02,
            "check_session": lambda _p: SessionState.expired,
            "auth_pause_timeout_seconds": 0.1,
        },
        daemon=True,
    )
    thread.start()
    try:

        def _all_failed() -> bool:
            with session_scope() as session:
                jobs = JobRepository(session)
                return all(jobs.get(job_id).status == JobStatus.failed for job_id in job_ids)

        _wait_until(_all_failed, timeout=3.0)
    finally:
        stop_event.set()
        thread.join(timeout=5)

    with session_scope() as session:
        cells = CellRepository(session)
        for job_id in job_ids:
            cell = cells.list_for_job(job_id)[0]
            assert cell.status == CellStatus.failed
            assert cell.error_code == "auth_required"


def test_other_provider_lane_keeps_collecting_while_one_is_paused(session_scope, register_fake):
    register_fake(Provider.semrush)
    register_fake(Provider.ahrefs)
    semrush_job = _make_client(session_scope, Provider.semrush, "concurrent-semrush")
    ahrefs_job = _make_client(session_scope, Provider.ahrefs, "concurrent-ahrefs")

    def _check(provider: Provider) -> SessionState:
        return SessionState.expired if provider == Provider.semrush else SessionState.alive

    dispatcher = Dispatcher(
        session_scope=session_scope,
        lease_owner="test-worker",
        providers=(Provider.semrush, Provider.ahrefs),
        lease_ttl_seconds=60,
        supervisor_interval_seconds=0.05,
        check_session=_check,
    )
    thread = threading.Thread(target=dispatcher.run_forever, daemon=True)
    thread.start()
    try:

        def _ahrefs_done_semrush_paused() -> bool:
            with session_scope() as session:
                cells = CellRepository(session)
                ahrefs_cell = cells.list_for_job(ahrefs_job)[0]
                semrush_cell = cells.list_for_job(semrush_job)[0]
                return (
                    ahrefs_cell.status == CellStatus.succeeded
                    and semrush_cell.status == CellStatus.awaiting_auth
                )

        _wait_until(_ahrefs_done_semrush_paused, timeout=3.0)
    finally:
        dispatcher.stop()
        thread.join(timeout=5)


def test_multiple_lanes_pause_once_and_all_resume_together(session_scope, register_fake):
    """task-31: N lanes of one provider must behave like task-21's single lane.

    Only one lane may wait for the login (otherwise the operator is asked N
    times and N lanes race to close the same backlog), the others must stop
    claiming instead of feeding clients to a session known to be dead, and
    every cell parked on that session — not just the pauser's own — has to be
    requeued when the login comes back.
    """
    jobs = [_make_client(session_scope, Provider.semrush, f"multi{i}") for i in range(4)]
    with session_scope() as session:
        first_client_id = JobRepository(session).get(jobs[0]).client_id

    login_event = threading.Event()
    login_event.set()
    adapter = _AuthOnceThenSucceedAdapter(Provider.semrush, first_client_id, login_event)
    register_fake(Provider.semrush, adapter)

    probes: list[float] = []

    def check_session(_provider):
        probes.append(time.monotonic())
        return SessionState.alive if login_event.is_set() else SessionState.expired

    pool = SlotPool(2)
    gate = AuthGate()
    stop_event = threading.Event()
    threads = [
        threading.Thread(
            target=run_lane,
            kwargs={
                "provider": Provider.semrush,
                "session_scope": session_scope,
                "lease_owner": f"test-worker-{index}",
                "stop_event": stop_event,
                "lease_ttl_seconds": 60,
                "poll_interval_seconds": 0.02,
                "check_session": check_session,
                "auth_pause_timeout_seconds": 30,
                "slots": pool,
                "auth_gate": gate,
            },
            daemon=True,
        )
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    try:

        def _someone_parked() -> bool:
            with session_scope() as session:
                repo = CellRepository(session)
                return any(
                    repo.list_for_job(job_id)[0].status == CellStatus.awaiting_auth
                    for job_id in jobs
                )

        _wait_until(_someone_parked, timeout=5.0)
        # Exactly one lane holds the pause; the gate is shut for everyone else.
        assert gate.is_open is False

        def _no_cell_starts_while_paused() -> bool:
            with session_scope() as session:
                repo = CellRepository(session)
                return not any(
                    repo.list_for_job(job_id)[0].status == CellStatus.running for job_id in jobs
                )

        time.sleep(0.2)
        assert _no_cell_starts_while_paused(), "клітинку віддали в мертву сесію під час паузи"

        login_event.set()

        def _all_done() -> bool:
            with session_scope() as session:
                repo = JobRepository(session)
                return all(repo.get(job_id).status == JobStatus.succeeded for job_id in jobs)

        _wait_until(_all_done, timeout=8.0)
        assert gate.is_open is True
    finally:
        stop_event.set()
        gate.release()
        for thread in threads:
            thread.join(timeout=5)


def test_startup_recovery_requeues_stranded_awaiting_auth_cell(session_scope, make_cell):
    _job_id, cell_id, _client_id = make_cell(Provider.semrush)
    with session_scope() as session:
        CellRepository(session).set_status(
            cell_id, CellStatus.awaiting_auth, error_code="auth_required"
        )

    report = run_startup_recovery(session_scope)

    assert report.requeued_awaiting_auth == 1
    with session_scope() as session:
        cell = CellRepository(session).get(cell_id)
        assert cell.status == CellStatus.queued
        assert cell.error_code is None
