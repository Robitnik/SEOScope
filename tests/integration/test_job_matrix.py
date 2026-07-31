"""Матриця «клієнт × сервіс» під справжнім worker-ом (PLAN §9.1/§9.2, §15).

Тут не перевіряються внутрішні функції job-engine (це юніти task-05), а
поведінка запущеного `Dispatcher`-а на реальній SQLite: паралельність
доріжок, послідовність усередині доріжки, ізоляція клітинок і «нуль падінь».
"""

from __future__ import annotations

from datetime import date

import pytest
from harness import FakeScenario, running_worker, wait_for

from app.db.repositories.cells import CellRepository
from app.db.repositories.jobs import JobRepository
from app.schemas.enums import CellStatus, ComparisonMode, JobStatus, PeriodKind, Provider

pytestmark = pytest.mark.integration

ANCHOR = date(2026, 6, 15)


def _create_run(temp_db, client_ids, providers, *, mode="collect_missing", idempotency_key=None):
    with temp_db.session_scope() as session:
        jobs = JobRepository(session).create_run(
            client_ids=list(client_ids),
            providers=list(providers),
            period_kind=PeriodKind.week,
            comparison_mode=ComparisonMode.closed,
            anchor_date=ANCHOR,
            mode=mode,
            idempotency_key=idempotency_key,
        )
        return [job.id for job in jobs], jobs[0].batch_id


def _cells(temp_db, job_id):
    with temp_db.session_scope() as session:
        cells = CellRepository(session).list_for_job(job_id)
        return {
            cell.provider: (cell.status, cell.started_at, cell.finished_at, cell.error_code)
            for cell in cells
        }


def _all_terminal(temp_db, job_ids):
    terminal = {
        CellStatus.succeeded,
        CellStatus.partial,
        CellStatus.failed,
        CellStatus.failed_interrupted,
        CellStatus.not_configured,
        CellStatus.skipped,
        CellStatus.cancelled,
    }

    def _check() -> bool:
        with temp_db.session_scope() as session:
            repo = CellRepository(session)
            return all(
                cell.status in terminal
                for job_id in job_ids
                for cell in repo.list_for_job(job_id)
            )

    return _check


def test_services_run_in_parallel_while_clients_run_sequentially(
    temp_db, fake_adapters, seed_client
):
    """Доріжки сервісів перекриваються в часі, клієнти всередині сервісу — ні."""
    scenarios = {
        Provider.ahrefs: FakeScenario(stage_seconds=0.05),
        Provider.semrush: FakeScenario(stage_seconds=0.05),
    }
    client_a = seed_client(slug="alpha", domain="alpha.example.com", scenarios=scenarios)
    client_b = seed_client(slug="beta", domain="beta.example.com", scenarios=scenarios)

    job_ids, _ = _create_run(
        temp_db, [client_a, client_b], [Provider.ahrefs, Provider.semrush]
    )

    with running_worker(temp_db.session_scope, providers=(Provider.ahrefs, Provider.semrush)):
        wait_for(_all_terminal(temp_db, job_ids), timeout=30, what="усі клітинки завершені")

    with temp_db.session_scope() as session:
        repo = CellRepository(session)
        spans = [
            (cell.provider, cell.client_id, cell.started_at, cell.finished_at)
            for job_id in job_ids
            for cell in repo.list_for_job(job_id)
        ]

    assert len(spans) == 4
    assert all(start is not None and end is not None for _, _, start, end in spans)

    def overlaps(left, right) -> bool:
        return left[2] < right[3] and right[2] < left[3]

    for provider in (Provider.ahrefs, Provider.semrush):
        same_lane = [s for s in spans if s[0] == provider]
        assert not overlaps(*same_lane), f"дві клітинки доріжки {provider} виконувались одночасно"

    cross_lane = [
        (left, right)
        for left in spans
        for right in spans
        if left[0] != right[0] and overlaps(left, right)
    ]
    assert cross_lane, "жодна пара різних сервісів не перетнулась у часі — доріжки не паралельні"


def test_provider_lanes_run_up_to_capacity_at_once(temp_db, fake_adapters, seed_client):
    """task-31: with N lanes configured, N cells of the SAME provider overlap —
    and never more than N, because the cap is enforced in the claim itself."""
    scenarios = {Provider.ahrefs: FakeScenario(stage_seconds=0.05, profile_store="main")}
    client_ids = [
        seed_client(slug=f"p{index}", domain=f"p{index}.example.com", scenarios=scenarios)
        for index in range(6)
    ]

    job_ids, _ = _create_run(temp_db, client_ids, [Provider.ahrefs])

    with running_worker(
        temp_db.session_scope,
        providers=(Provider.ahrefs,),
        parallelism={Provider.ahrefs: 3},
    ):
        wait_for(_all_terminal(temp_db, job_ids), timeout=30, what="усі клітинки завершені")

    with temp_db.session_scope() as session:
        repo = CellRepository(session)
        cells = [cell for job_id in job_ids for cell in repo.list_for_job(job_id)]

    spans = [(cell.started_at, cell.finished_at) for cell in cells]
    assert len(spans) == 6
    assert all(start is not None and end is not None for start, end in spans)

    # Peak concurrency measured off the recorded spans — the same evidence the
    # single-lane test uses, read the other way round.
    edges = sorted(
        [(start, 1) for start, _ in spans] + [(end, -1) for _, end in spans],
        key=lambda edge: (edge[0], edge[1]),
    )
    peak = 0
    current = 0
    for _, delta in edges:
        current += delta
        peak = max(peak, current)

    assert peak > 1, "жодні дві клітинки одного сервісу не перетнулись — паралельності немає"
    assert peak <= 3, f"перевищено місткість профілю: одночасно виконувалось {peak} клітинок"
    assert {cell.slot for cell in cells} <= {0, 1, 2}
    assert len({cell.slot for cell in cells}) > 1, "усі клітинки пройшли через один слот"


def test_single_lane_provider_still_runs_one_cell_at_a_time(temp_db, fake_adapters, seed_client):
    """The default stays what PLAN §9.1 promised: capacity 1 per profile."""
    scenarios = {Provider.ahrefs: FakeScenario(stage_seconds=0.05, profile_store="main")}
    client_ids = [
        seed_client(slug=f"s{index}", domain=f"s{index}.example.com", scenarios=scenarios)
        for index in range(3)
    ]

    job_ids, _ = _create_run(temp_db, client_ids, [Provider.ahrefs])

    with running_worker(temp_db.session_scope, providers=(Provider.ahrefs,)):
        wait_for(_all_terminal(temp_db, job_ids), timeout=30, what="усі клітинки завершені")

    with temp_db.session_scope() as session:
        repo = CellRepository(session)
        spans = [
            (cell.started_at, cell.finished_at)
            for job_id in job_ids
            for cell in repo.list_for_job(job_id)
        ]

    for left in spans:
        for right in spans:
            if left is right:
                continue
            assert not (left[0] < right[1] and right[0] < left[1]), "клітинки перетнулись у часі"


def test_one_failed_cell_never_touches_its_neighbours(temp_db, fake_adapters, seed_client):
    """Виняток у клітинці ізольований: доріжка триває, решта клітинок штатні."""
    client_id = seed_client(
        scenarios={
            Provider.ahrefs: FakeScenario(scenario="fail"),
            Provider.semrush: FakeScenario(),
            Provider.gsc: FakeScenario(scenario="partial"),
        }
    )
    job_ids, _ = _create_run(
        temp_db, [client_id], [Provider.ahrefs, Provider.semrush, Provider.gsc]
    )

    with running_worker(temp_db.session_scope):
        wait_for(_all_terminal(temp_db, job_ids), timeout=30, what="усі клітинки завершені")

    cells = _cells(temp_db, job_ids[0])
    assert cells[Provider.ahrefs][0] == CellStatus.failed
    assert cells[Provider.semrush][0] == CellStatus.succeeded
    assert cells[Provider.gsc][0] == CellStatus.partial

    with temp_db.session_scope() as session:
        job = JobRepository(session).get(job_ids[0])
        assert job is not None
        assert job.status == JobStatus.partial


def test_client_without_integration_is_not_configured_not_failed(
    temp_db, fake_adapters, seed_client
):
    """Новий клієнт, не заведений у сервісі: `not_configured`, не краш і не нуль."""
    client_id = seed_client(scenarios={Provider.semrush: FakeScenario()})
    job_ids, _ = _create_run(temp_db, [client_id], [Provider.ahrefs, Provider.semrush])

    with running_worker(temp_db.session_scope):
        wait_for(_all_terminal(temp_db, job_ids), timeout=30, what="усі клітинки завершені")

    cells = _cells(temp_db, job_ids[0])
    assert cells[Provider.ahrefs][0] == CellStatus.not_configured
    assert cells[Provider.ahrefs][3] == "not_configured"
    assert cells[Provider.semrush][0] == CellStatus.succeeded

    from sqlalchemy import select

    from app.db.models import MetricValue

    with temp_db.session_scope() as session:
        providers_with_values = set(
            session.scalars(select(MetricValue.provider).where(MetricValue.client_id == client_id))
        )
    assert providers_with_values == {Provider.semrush}, (
        "не налаштований сервіс не має створювати жодного значення метрики"
    )


def test_hung_adapter_times_out_without_blocking_the_lane(temp_db, fake_adapters, seed_client):
    """Завислий адаптер помирає по таймауту, наступна клітинка доріжки йде далі."""
    stuck = seed_client(
        slug="stuck",
        domain="stuck.example.com",
        scenarios={Provider.ahrefs: FakeScenario(scenario="timeout")},
    )
    healthy = seed_client(
        slug="healthy",
        domain="healthy.example.com",
        scenarios={Provider.ahrefs: FakeScenario()},
    )
    job_ids, _ = _create_run(temp_db, [stuck, healthy], [Provider.ahrefs])

    with running_worker(
        temp_db.session_scope, providers=(Provider.ahrefs,), cell_timeout_seconds=1
    ):
        wait_for(_all_terminal(temp_db, job_ids), timeout=30, what="усі клітинки завершені")

    statuses = {
        job_id: next(iter(_cells(temp_db, job_id).values()))[:1][0] for job_id in job_ids
    }
    assert CellStatus.failed in statuses.values()
    assert CellStatus.succeeded in statuses.values()

    with temp_db.session_scope() as session:
        timed_out = [
            cell
            for job_id in job_ids
            for cell in CellRepository(session).list_for_job(job_id)
            if cell.status == CellStatus.failed
        ]
    assert [cell.error_code for cell in timed_out] == ["timeout"]
