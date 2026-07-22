"""task-21: the lane checks a browser provider's session at most once per
run, never once per client (regression: Semrush used to open a real browser
and fail on the login page for each of N queued clients in turn).
"""

from __future__ import annotations

import threading
import time
from datetime import date

import pytest

from app.db.models import Client, ClientIntegration
from app.db.repositories.cells import CellRepository
from app.db.repositories.jobs import JobRepository
from app.integrations.browser_session import SessionState
from app.jobs.fake_adapter import FakeAdapter
from app.jobs.lane import run_lane
from app.jobs.runner import CellHandle, run_cell
from app.schemas.enums import CellStatus, ComparisonMode, PeriodKind, Provider


class _CountingAdapter:
    """Wraps `FakeAdapter` to record how many times `preflight`/`collect` were
    actually invoked — the thing that must stay at zero for a parked cell.
    """

    def __init__(self, inner):  # type: ignore[no-untyped-def]
        self.provider = inner.provider
        self._inner = inner
        self.preflight_calls = 0
        self.collect_calls = 0

    def preflight(self, context):  # type: ignore[no-untyped-def]
        self.preflight_calls += 1
        return self._inner.preflight(context)

    def collect(self, context, emit, is_cancel_requested):  # type: ignore[no-untyped-def]
        self.collect_calls += 1
        return self._inner.collect(context, emit, is_cancel_requested)


def _seed_clients(
    session_scope, provider: Provider, n: int, *, slug_prefix: str = "c"
) -> list[str]:
    job_ids = []
    with session_scope() as session:
        for i in range(n):
            slug = f"{slug_prefix}{i}"
            client = Client(
                name=slug, slug=slug, domain=f"{slug}.com", timezone="America/New_York"
            )
            session.add(client)
            session.flush()
            session.add(
                ClientIntegration(
                    client_id=client.id,
                    provider=provider,
                    enabled=True,
                    config_json={"scenario": "success"},
                )
            )
            jobs = JobRepository(session).create_run(
                client_ids=[client.id],
                providers=[provider],
                period_kind=PeriodKind.week,
                comparison_mode=ComparisonMode.closed,
                anchor_date=date(2026, 1, 1),
            )
            job_ids.append(jobs[0].id)
    return job_ids


@pytest.fixture
def run_one_lane(session_scope):
    def _run(provider: Provider, *, check_session, duration_seconds: float, **kwargs) -> None:
        stop_event = threading.Event()
        thread = threading.Thread(
            target=run_lane,
            kwargs={
                "provider": provider,
                "session_scope": session_scope,
                "lease_owner": "test-worker",
                "stop_event": stop_event,
                "lease_ttl_seconds": 60,
                "poll_interval_seconds": 0.02,
                "check_session": check_session,
                **kwargs,
            },
            daemon=True,
        )
        thread.start()
        time.sleep(duration_seconds)
        stop_event.set()
        thread.join(timeout=5)

    return _run


def test_check_session_called_once_across_a_multi_client_batch(
    session_scope, register_fake, run_one_lane
):
    counting = _CountingAdapter(FakeAdapter(Provider.semrush))
    register_fake(Provider.semrush, counting)
    job_ids = _seed_clients(session_scope, Provider.semrush, 3)

    calls: list[Provider] = []

    def _check(provider: Provider) -> SessionState:
        calls.append(provider)
        return SessionState.alive

    run_one_lane(Provider.semrush, check_session=_check, duration_seconds=1.0)

    assert len(calls) == 1, f"expected exactly one check_session call, got {len(calls)}"
    assert counting.collect_calls == 3
    with session_scope() as session:
        for job_id in job_ids:
            assert JobRepository(session).get(job_id).status.value == "succeeded"


def test_dead_session_never_opens_a_browser_for_any_client(
    session_scope, register_fake, run_one_lane
):
    counting = _CountingAdapter(FakeAdapter(Provider.semrush))
    register_fake(Provider.semrush, counting)
    job_ids = _seed_clients(session_scope, Provider.semrush, 3)

    def _check(_provider: Provider) -> SessionState:
        return SessionState.expired

    run_one_lane(Provider.semrush, check_session=_check, duration_seconds=0.5)

    # A dead session is discovered by the cheap probe alone — the fake
    # adapter's preflight/collect (standing in for a real browser trip) is
    # never reached for the parked cell, and no other client is attempted.
    assert counting.preflight_calls == 0
    assert counting.collect_calls == 0

    with session_scope() as session:
        cells = CellRepository(session)
        statuses = [cells.list_for_job(job_id)[0].status for job_id in job_ids]
    assert statuses.count(CellStatus.awaiting_auth) == 1
    assert statuses.count(CellStatus.queued) == 2


def test_non_browser_provider_never_calls_check_session(session_scope, register_fake):
    """`reporting_ninja` has no browser session at all (task-20) — the gate
    must never fire for it, regardless of what `check_session` would say.
    """
    register_fake(Provider.reporting_ninja)

    with session_scope() as session:
        client = Client(name="A", slug="a-rn", domain="a-rn.com", timezone="America/New_York")
        session.add(client)
        session.flush()
        session.add(
            ClientIntegration(
                client_id=client.id,
                provider=Provider.reporting_ninja,
                enabled=True,
                config_json={"scenario": "success"},
            )
        )
        jobs = JobRepository(session).create_run(
            client_ids=[client.id],
            providers=[Provider.reporting_ninja],
            period_kind=PeriodKind.week,
            comparison_mode=ComparisonMode.closed,
            anchor_date=date(2026, 1, 1),
        )
        job_id = jobs[0].id
        cell_id = CellRepository(session).list_for_job(job_id)[0].id

    calls: list[Provider] = []

    def _check(provider: Provider) -> SessionState:
        calls.append(provider)
        return SessionState.expired  # would pause a browser provider

    handle = CellHandle(
        cell_id=cell_id, job_id=job_id, client_id=client.id, provider=Provider.reporting_ninja
    )
    status = run_cell(handle, session_scope=session_scope, lease_owner="w1", check_session=_check)

    assert calls == []
    assert status == CellStatus.succeeded
