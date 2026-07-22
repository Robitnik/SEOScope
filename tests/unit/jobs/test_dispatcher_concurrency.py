"""Dispatcher/lane concurrency model (PLAN §9.1): between services in
parallel, within a service sequential, and the same `(provider, profile)`
never active twice at once.
"""

from __future__ import annotations

import threading
import time
from datetime import date

import pytest

from app.db.models import Client, ClientIntegration
from app.db.repositories.jobs import JobRepository
from app.jobs.dispatcher import Dispatcher
from app.jobs.fake_adapter import FakeAdapter
from app.schemas.enums import CellStatus, ComparisonMode, PeriodKind, Provider


class _RecordingAdapter(FakeAdapter):
    """Wraps `FakeAdapter.collect` to record its wall-clock window, so tests
    can assert on real overlap/non-overlap instead of internal call order.
    """

    def __init__(self, provider: Provider, log: list) -> None:
        super().__init__(provider)
        self._log = log

    def collect(self, context, emit, is_cancel_requested):  # type: ignore[no-untyped-def]
        start = time.monotonic()
        result = super().collect(context, emit, is_cancel_requested)
        end = time.monotonic()
        self._log.append((str(context.provider), context.client_id, start, end))
        return result


def _overlaps(a: tuple, b: tuple) -> bool:
    _, _, a_start, a_end = a
    _, _, b_start, b_end = b
    return a_start < b_end and b_start < a_end


@pytest.fixture
def run_dispatcher(session_scope):
    def _run(providers: tuple[Provider, ...], duration_seconds: float) -> None:
        dispatcher = Dispatcher(
            session_scope=session_scope,
            lease_owner="test-worker",
            providers=providers,
            lease_ttl_seconds=60,
            supervisor_interval_seconds=0.05,
        )
        thread = threading.Thread(target=dispatcher.run_forever, daemon=True)
        thread.start()
        time.sleep(duration_seconds)
        dispatcher.stop()
        thread.join(timeout=5)

    return _run


def test_different_providers_run_concurrently(session_scope, register_fake, run_dispatcher):
    log: list = []
    register_fake(Provider.ahrefs, _RecordingAdapter(Provider.ahrefs, log))
    register_fake(Provider.semrush, _RecordingAdapter(Provider.semrush, log))

    with session_scope() as session:
        client = Client(name="A", slug="a", domain="a.com", timezone="America/New_York")
        session.add(client)
        session.flush()
        session.add_all(
            [
                ClientIntegration(
                    client_id=client.id,
                    provider=Provider.ahrefs,
                    config_json={"scenario": "success", "stage_seconds": 0.15},
                ),
                ClientIntegration(
                    client_id=client.id,
                    provider=Provider.semrush,
                    config_json={"scenario": "success", "stage_seconds": 0.15},
                ),
            ]
        )
        JobRepository(session).create_run(
            client_ids=[client.id],
            providers=[Provider.ahrefs, Provider.semrush],
            period_kind=PeriodKind.week,
            comparison_mode=ComparisonMode.closed,
            anchor_date=date(2026, 1, 1),
        )

    run_dispatcher((Provider.ahrefs, Provider.semrush), duration_seconds=1.5)

    assert len(log) == 2
    assert _overlaps(log[0], log[1]), f"expected overlapping lanes, got {log}"


def test_same_provider_two_clients_never_overlap_sequential_within_lane(
    session_scope, register_fake, run_dispatcher
):
    log: list = []
    register_fake(Provider.ahrefs, _RecordingAdapter(Provider.ahrefs, log))

    with session_scope() as session:
        for slug in ("a", "b"):
            client = Client(name=slug, slug=slug, domain=f"{slug}.com", timezone="America/New_York")
            session.add(client)
            session.flush()
            session.add(
                ClientIntegration(
                    client_id=client.id,
                    provider=Provider.ahrefs,
                    config_json={"scenario": "success", "stage_seconds": 0.15},
                )
            )
            JobRepository(session).create_run(
                client_ids=[client.id],
                providers=[Provider.ahrefs],
                period_kind=PeriodKind.week,
                comparison_mode=ComparisonMode.closed,
                anchor_date=date(2026, 1, 1),
            )

    run_dispatcher((Provider.ahrefs,), duration_seconds=2.0)

    assert len(log) == 2
    assert not _overlaps(log[0], log[1]), f"expected sequential lane, got overlap: {log}"


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_lane_survives_thread_death_and_gets_restarted(
    session_scope, register_fake, run_dispatcher
):
    """A lane whose thread dies unexpectedly is restarted (PLAN §9.2
    "доріжка супервізорить"); simulated by handing the dispatcher a
    provider whose lane raises once via a broken `session_scope` wrapper.
    """
    attempts = {"n": 0}
    real_scope = session_scope

    def _flaky_scope():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("simulated lane crash on first claim attempt")
        return real_scope()

    register_fake(Provider.ahrefs)
    with session_scope() as session:
        client = Client(name="A", slug="a", domain="a.com", timezone="America/New_York")
        session.add(client)
        session.flush()
        session.add(
            ClientIntegration(
                client_id=client.id, provider=Provider.ahrefs, config_json={"scenario": "success"}
            )
        )
        jobs = JobRepository(session).create_run(
            client_ids=[client.id],
            providers=[Provider.ahrefs],
            period_kind=PeriodKind.week,
            comparison_mode=ComparisonMode.closed,
            anchor_date=date(2026, 1, 1),
        )
        job_id = jobs[0].id

    dispatcher = Dispatcher(
        session_scope=_flaky_scope,
        lease_owner="test-worker",
        providers=(Provider.ahrefs,),
        lease_ttl_seconds=60,
        supervisor_interval_seconds=0.05,
    )
    thread = threading.Thread(target=dispatcher.run_forever, daemon=True)
    thread.start()
    time.sleep(1.0)
    dispatcher.stop()
    thread.join(timeout=5)

    assert attempts["n"] >= 2  # first attempt crashed, lane got restarted and tried again
    with session_scope() as session:
        from app.db.repositories.cells import CellRepository

        cells = CellRepository(session).list_for_job(job_id)
    assert cells and all(c.status == CellStatus.succeeded for c in cells)
