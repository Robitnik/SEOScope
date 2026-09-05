"""`ArchiveWriteController` (task-62 §2.2) — the fix for the measured
2026-09-05 lock storm: 6 parallel capture workers, each with its own
session, committing independently. These tests use REAL threads and a REAL
file-backed SQLite engine (the `session_scope` fixture) so a regression here
would show up as an actual `database is locked` error, not a mock passing
trivially.
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, select

from app.webarchive.writequeue import ArchiveWriteController

_metadata = MetaData()
_counter_table = Table(
    "wc_test_counter", _metadata, Column("id", Integer, primary_key=True), Column("n", Integer)
)


@pytest.fixture
def counter_table(engine):
    _metadata.create_all(engine)
    return _counter_table


def test_submit_applies_the_write_and_returns_its_result(session, session_scope, counter_table):
    controller = ArchiveWriteController()
    controller.start()
    try:
        def _insert() -> int:
            session.execute(counter_table.insert().values(id=1, n=42))
            return 42

        result = controller.submit(session, _insert)
        assert result == 42
        with session_scope() as fresh:
            row = fresh.execute(select(counter_table.c.n).where(counter_table.c.id == 1)).first()
            assert row is not None and row[0] == 42
    finally:
        controller.stop()


def test_concurrent_submits_from_many_threads_never_raise_database_locked(
    session_scope, counter_table
):
    """The actual regression test: 8 threads, each with its OWN session
    (mirroring 8 parallel capture workers), hammer `submit` at once. Before
    task-62, this exact shape (independent sessions calling
    `commit_with_retry` directly) produced `archive_db_locked_retry` bursts
    under real portfolio load.
    """
    controller = ArchiveWriteController()
    controller.start()
    errors: list[BaseException] = []
    threads_n = 8
    writes_per_thread = 20

    def _worker(thread_id: int) -> None:
        try:
            with session_scope() as own_session:
                for i in range(writes_per_thread):
                    def _insert(i=i, thread_id=thread_id) -> None:
                        own_session.execute(
                            counter_table.insert().values(id=thread_id * 1000 + i, n=thread_id)
                        )

                    controller.submit(own_session, _insert)
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)

    try:
        threads = [threading.Thread(target=_worker, args=(t,)) for t in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == []
        with session_scope() as verify:
            row_count = len(list(verify.execute(select(counter_table.c.id))))
            assert row_count == threads_n * writes_per_thread  # every write landed, none lost
    finally:
        controller.stop()


def test_exception_in_one_job_does_not_break_the_next(session, session_scope, counter_table):
    controller = ArchiveWriteController()
    controller.start()
    try:
        def _boom() -> None:
            raise ValueError("deliberate")

        with pytest.raises(ValueError, match="deliberate"):
            controller.submit(session, _boom)

        def _insert() -> None:
            session.execute(counter_table.insert().values(id=99, n=1))

        controller.submit(session, _insert)  # the queue keeps working afterwards
        with session_scope() as fresh:
            row = fresh.execute(select(counter_table.c.n).where(counter_table.c.id == 99)).first()
            assert row is not None
    finally:
        controller.stop()


def test_stop_waits_for_the_queue_to_drain(session, counter_table):
    controller = ArchiveWriteController()
    controller.start()

    def _slow_insert() -> None:
        time.sleep(0.2)
        session.execute(counter_table.insert().values(id=1, n=1))

    result_holder: dict[str, object] = {}

    def _submit_in_background() -> None:
        result_holder["ok"] = True
        controller.submit(session, _slow_insert)

    t = threading.Thread(target=_submit_in_background)
    t.start()
    t.join(timeout=5)
    controller.stop()  # must not return before the submitted job above finished
    assert result_holder.get("ok") is True
