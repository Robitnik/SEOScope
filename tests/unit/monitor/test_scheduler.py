"""Scheduler due-logic, jitter, dedup, and pause-awareness (task-35 §3.6/§5).

Exercises `_due`/`_jitter_minutes` directly (pure) and `MonitorScheduler._tick`
against a real temp-DB session (no threads/network — `_tick` only decides
what to submit; the submission itself is monkeypatched to record calls).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.db.repositories.clients import ClientRepository
from app.db.repositories.monitor import MonitorRepository
from app.monitor.scheduler import FULL, LIGHT, MonitorScheduler, _due, _jitter_minutes
from app.schemas.enums import ClientStatus


def test_jitter_is_stable_and_within_span():
    a = _jitter_minutes("client-1")
    b = _jitter_minutes("client-1")
    assert a == b
    assert 0 <= a < 20


def test_jitter_differs_across_clients_in_general():
    values = {_jitter_minutes(f"client-{i}") for i in range(10)}
    assert len(values) > 1


def test_due_is_true_when_never_run(session):
    client_id = ClientRepository(session).create(name="A", slug="a", domain="a.com").id
    repo = MonitorRepository(session)
    assert _due(repo, client_id, kind="light", interval_hours=1, now=datetime.now(UTC)) is True


def test_due_is_false_immediately_after_a_run(session):
    client_id = ClientRepository(session).create(name="A", slug="a", domain="a.com").id
    repo = MonitorRepository(session)
    run = repo.create_run(client_id, kind="light")
    repo.finish_run(run.id, status="succeeded")
    session.commit()
    assert _due(repo, client_id, kind="light", interval_hours=1, now=datetime.now(UTC)) is False


def test_due_is_true_after_the_interval_plus_jitter_elapses(session):
    client_id = ClientRepository(session).create(name="A", slug="a", domain="a.com").id
    repo = MonitorRepository(session)
    old_start = datetime.now(UTC) - timedelta(hours=2)
    run = repo.create_run(client_id, kind="light")
    run.started_at = old_start
    repo.finish_run(run.id, status="succeeded")
    session.commit()
    assert _due(repo, client_id, kind="light", interval_hours=1, now=datetime.now(UTC)) is True


def _build_scheduler(engine, settings=None):
    from sqlalchemy.orm import sessionmaker

    maker = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        session = maker()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    return MonitorScheduler(session_scope=scope, settings=settings or Settings(_env_file=None))


def test_tick_skips_paused_clients_without_opt_in(engine):
    from sqlalchemy.orm import sessionmaker

    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as session:
        client = ClientRepository(session).create(name="A", slug="a", domain="a.com")
        ClientRepository(session).set_status(client.id, ClientStatus.paused)
        session.commit()

    scheduler = _build_scheduler(engine)
    submitted = []
    scheduler._submit = lambda client, kind, **kw: submitted.append((client.id, kind))  # type: ignore[method-assign]
    scheduler._tick()
    assert submitted == []


def test_tick_includes_paused_client_when_monitor_when_paused_is_set(engine):
    from sqlalchemy.orm import sessionmaker

    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as session:
        client = ClientRepository(session).create(name="A", slug="a", domain="a.com")
        ClientRepository(session).set_status(client.id, ClientStatus.paused)
        MonitorRepository(session).update_settings(client.id, monitor_when_paused=True)
        session.commit()
        cid = client.id

    scheduler = _build_scheduler(engine)
    submitted = []
    scheduler._submit = lambda client, kind, **kw: submitted.append((client.id, kind))  # type: ignore[method-assign]
    scheduler._tick()
    assert (cid, FULL) in submitted


def test_tick_never_queues_light_behind_a_due_full_run(engine):
    from sqlalchemy.orm import sessionmaker

    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as session:
        client = ClientRepository(session).create(name="A", slug="a", domain="a.com")
        session.commit()
        cid = client.id

    scheduler = _build_scheduler(engine)
    submitted = []
    scheduler._submit = lambda client, kind, **kw: submitted.append((client.id, kind))  # type: ignore[method-assign]
    scheduler._tick()
    # Both are due (never run) — only `full` should be queued this tick.
    assert submitted == [(cid, FULL)]


def test_tick_does_not_resubmit_a_client_with_a_run_already_in_flight(engine):
    from sqlalchemy.orm import sessionmaker

    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as session:
        client = ClientRepository(session).create(name="A", slug="a", domain="a.com")
        MonitorRepository(session).create_run(client.id, kind="full")
        session.commit()

    scheduler = _build_scheduler(engine)
    submitted = []
    scheduler._submit = lambda client, kind, **kw: submitted.append((client.id, kind))  # type: ignore[method-assign]
    scheduler._tick()
    assert submitted == []


def test_tick_claims_a_manually_queued_run(engine):
    from sqlalchemy.orm import sessionmaker

    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as session:
        client = ClientRepository(session).create(name="A", slug="a", domain="a.com")
        queued = MonitorRepository(session).enqueue_run(client.id, kind="light")
        session.commit()
        cid, run_id = client.id, queued.id

    scheduler = _build_scheduler(engine)
    submitted = []
    scheduler._submit = lambda client, kind, **kw: submitted.append(
        (client.id, kind, kw.get("existing_run_id"))
    )  # type: ignore[method-assign]
    scheduler._tick()
    assert (cid, LIGHT, run_id) in submitted
