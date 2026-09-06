"""`DbMaintenanceScheduler` (task-68 §1): runs exactly once per day no
matter how many ticks land after the configured time, same shape as
`tests/unit/cloud/test_scheduler.py`'s equivalent test for `CloudScheduler`.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.maintenance_scheduler import DbMaintenanceScheduler


def _scope(engine):
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        session = maker()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    return scope


class _FrozenDatetime:
    """Stand-in for the `datetime` name imported into
    `app.db.maintenance_scheduler` — that module does
    `from datetime import ... datetime`, binding the class by reference at
    import time, so patching the stdlib `datetime` module itself would
    have no effect; this replaces the module's own local name instead.
    """

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self, tz: object = None) -> datetime:
        return self._fixed


def test_runs_exactly_once_across_repeated_ticks_past_the_configured_time(engine, monkeypatch):
    settings = Settings(_env_file=None, db_maintenance_at_utc="03:00")
    scheduler = DbMaintenanceScheduler(session_scope=_scope(engine), settings=settings)

    calls: list[None] = []
    monkeypatch.setattr(
        "app.db.maintenance_scheduler.run_maintenance",
        lambda session: (calls.append(None), _FakeResult())[1],
    )
    monkeypatch.setattr(
        "app.db.maintenance_scheduler.datetime",
        _FrozenDatetime(datetime(2026, 9, 6, 3, 30, tzinfo=UTC)),
    )

    scheduler._tick()
    scheduler._tick()
    scheduler._tick()

    assert len(calls) == 1, "a second/third tick the same day must be a no-op"


def test_does_nothing_before_the_configured_time(engine, monkeypatch):
    settings = Settings(_env_file=None, db_maintenance_at_utc="03:00")
    scheduler = DbMaintenanceScheduler(session_scope=_scope(engine), settings=settings)

    calls: list[None] = []
    monkeypatch.setattr(
        "app.db.maintenance_scheduler.run_maintenance",
        lambda session: (calls.append(None), _FakeResult())[1],
    )
    monkeypatch.setattr(
        "app.db.maintenance_scheduler.datetime",
        _FrozenDatetime(datetime(2026, 9, 6, 2, 59, tzinfo=UTC)),
    )

    scheduler._tick()

    assert calls == []


def test_runs_again_the_next_day(engine, monkeypatch):
    settings = Settings(_env_file=None, db_maintenance_at_utc="03:00")
    scheduler = DbMaintenanceScheduler(session_scope=_scope(engine), settings=settings)

    calls: list[None] = []
    monkeypatch.setattr(
        "app.db.maintenance_scheduler.run_maintenance",
        lambda session: (calls.append(None), _FakeResult())[1],
    )

    monkeypatch.setattr(
        "app.db.maintenance_scheduler.datetime",
        _FrozenDatetime(datetime(2026, 9, 6, 3, 30, tzinfo=UTC)),
    )
    scheduler._tick()

    monkeypatch.setattr(
        "app.db.maintenance_scheduler.datetime",
        _FrozenDatetime(datetime(2026, 9, 7, 3, 30, tzinfo=UTC)),
    )
    scheduler._tick()

    assert len(calls) == 2


class _FakeResult:
    checkpoint_busy = 0
    checkpoint_pages = 0
