"""Рестарт worker-а не лишає «вічно running» job (PLAN §9.2/§9.3, §18).

Обрив процесу відтворюється не вбивством потоку (це недетерміновано й нічого
не доводить), а точним станом, який такий обрив лишає в БД: клітинка
`running` з протухлою lease або `cancel_requested`, за якою вже ніхто не
стежить. Далі проганяється справжня послідовність старту worker-а —
`run_startup_recovery` перед диспетчером, як у `app.jobs.worker.main`.
"""

from __future__ import annotations

from datetime import date

import pytest
from harness import FakeScenario, running_worker, wait_for

from app.db.repositories.cells import CellRepository
from app.db.repositories.events import EventRepository
from app.db.repositories.jobs import JobRepository
from app.jobs.recovery import run_startup_recovery
from app.schemas.enums import CellStatus, ComparisonMode, JobStatus, PeriodKind, Provider

pytestmark = pytest.mark.integration

ANCHOR = date(2026, 6, 15)


def _create_run(temp_db, client_id, providers):
    with temp_db.session_scope() as session:
        jobs = JobRepository(session).create_run(
            client_ids=[client_id],
            providers=list(providers),
            period_kind=PeriodKind.week,
            comparison_mode=ComparisonMode.closed,
            anchor_date=ANCHOR,
        )
        return jobs[0].id


def _statuses(temp_db, job_id):
    with temp_db.session_scope() as session:
        return {c.provider: c.status for c in CellRepository(session).list_for_job(job_id)}


def test_interrupted_cell_is_requeued_and_finishes_after_restart(
    temp_db, fake_adapters, seed_client
):
    client_id = seed_client(scenarios={Provider.ahrefs: FakeScenario()})
    job_id = _create_run(temp_db, client_id, [Provider.ahrefs])

    # Так виглядає БД після kill -9 посеред збору: клітинка взята в роботу
    # процесом, якого вже немає, з lease у минулому.
    with temp_db.session_scope() as session:
        claimed = CellRepository(session).claim_next_cell(
            Provider.ahrefs, lease_owner="dead-worker:1", lease_ttl_seconds=-1
        )
        assert claimed is not None
        cell_id = claimed.id

    report = run_startup_recovery(temp_db.session_scope)
    assert report.requeued_interrupted == 1

    with temp_db.session_scope() as session:
        cell = CellRepository(session).get(cell_id)
        assert cell is not None
        assert cell.status == CellStatus.queued
        assert cell.attempt == 2, "перезапуск має рахуватись новою спробою"
        assert cell.lease_owner is None

        events = EventRepository(session).since(job_id, 0)
        assert any(e.stage == "recovery" and e.level == "warning" for e in events), (
            "обрив має лишити слід в аудиті, а не зникнути мовчки"
        )

    with running_worker(temp_db.session_scope, providers=(Provider.ahrefs,)):
        wait_for(
            lambda: _statuses(temp_db, job_id)[Provider.ahrefs] == CellStatus.succeeded,
            timeout=30,
            what="перевзята клітинка завершилась успіхом",
        )

    with temp_db.session_scope() as session:
        job = JobRepository(session).get(job_id)
        assert job is not None
        assert job.status == JobStatus.succeeded
        assert job.finished_at is not None, "job не має лишитись без finished_at"


def test_orphaned_cancel_request_is_finalized_not_silently_requeued(
    temp_db, fake_adapters, seed_client
):
    """`cancel_requested` від померлого процесу стає `cancelled`, а не `queued`."""
    client_id = seed_client(scenarios={Provider.gsc: FakeScenario()})
    job_id = _create_run(temp_db, client_id, [Provider.gsc])

    with temp_db.session_scope() as session:
        CellRepository(session).request_cancel(job_id=job_id)

    report = run_startup_recovery(temp_db.session_scope)
    assert report.finalized_cancelled == 1
    assert _statuses(temp_db, job_id)[Provider.gsc] == CellStatus.cancelled

    # Найважливіше: після рестарту доріжка не має «випадково» підхопити цю
    # клітинку знову — скасоване лишається скасованим.
    with running_worker(temp_db.session_scope, providers=(Provider.gsc,)):
        wait_for(lambda: True, timeout=1, what="worker піднявся")
    assert _statuses(temp_db, job_id)[Provider.gsc] == CellStatus.cancelled


def test_restart_leaves_no_cell_running_forever(temp_db, fake_adapters, seed_client):
    """Після відновлення жодна клітинка не лишається в `running`."""
    client_id = seed_client(
        scenarios={p: FakeScenario() for p in (Provider.ahrefs, Provider.semrush, Provider.gsc)}
    )
    job_id = _create_run(temp_db, client_id, [Provider.ahrefs, Provider.semrush, Provider.gsc])

    with temp_db.session_scope() as session:
        repo = CellRepository(session)
        for provider in (Provider.ahrefs, Provider.semrush):
            assert (
                repo.claim_next_cell(provider, lease_owner="dead-worker:1", lease_ttl_seconds=-1)
                is not None
            )

    run_startup_recovery(temp_db.session_scope)
    with running_worker(temp_db.session_scope):
        wait_for(
            lambda: all(
                status != CellStatus.running and status != CellStatus.queued
                for status in _statuses(temp_db, job_id).values()
            ),
            timeout=30,
            what="жодної running/queued клітинки після відновлення",
        )

    assert set(_statuses(temp_db, job_id).values()) == {CellStatus.succeeded}
