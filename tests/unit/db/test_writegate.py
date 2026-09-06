"""`DbWriteGate` (task-66 §2) — the one global write queue + cross-process
flock every SQLite write in the process now routes through. Real threads,
a real file-backed SQLite engine and a real OS `flock`, same testing
philosophy as `tests/unit/webarchive/test_write_controller.py` (task-62):
a regression here should show up as an actual assertion failure, not a
mock passing trivially.
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.writegate import DbWriteGate

_metadata = MetaData()
_counter_table = Table(
    "wg_test_counter", _metadata, Column("id", Integer, primary_key=True), Column("n", Integer)
)


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    _metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def gate(tmp_path):
    g = DbWriteGate(lock_path=tmp_path / "db.write.lock")
    yield g
    g.stop()


def test_submit_applies_the_write_and_returns_its_result(session_factory, gate):
    with session_factory() as session:

        def _insert() -> int:
            session.execute(_counter_table.insert().values(id=1, n=42))
            return 42

        result = gate.submit(session, _insert)
        assert result == 42

    with session_factory() as fresh:
        row = fresh.execute(
            select(_counter_table.c.n).where(_counter_table.c.id == 1)
        ).first()
        assert row is not None and row[0] == 42


def test_concurrent_submits_from_many_threads_never_overlap(session_factory, gate):
    """The actual regression this gate exists for: many threads, each with
    its OWN session, hammer `submit` at once. `in_flight` would exceed 1 if
    two jobs' `fn()`+`commit()` ever ran concurrently.
    """
    threads_n = 8
    writes_per_thread = 20
    in_flight = 0
    max_in_flight = 0
    lock = threading.Lock()
    errors: list[BaseException] = []

    def _worker(thread_id: int) -> None:
        nonlocal in_flight, max_in_flight
        try:
            with session_factory() as own_session:
                for i in range(writes_per_thread):

                    def _insert(i=i, thread_id=thread_id) -> None:
                        nonlocal in_flight, max_in_flight
                        with lock:
                            in_flight += 1
                            max_in_flight = max(max_in_flight, in_flight)
                        time.sleep(0.001)
                        with lock:
                            in_flight -= 1
                        own_session.execute(
                            _counter_table.insert().values(id=thread_id * 1000 + i, n=thread_id)
                        )

                    gate.submit(own_session, _insert)
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(t,)) for t in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert max_in_flight == 1
    with session_factory() as verify:
        row_count = len(list(verify.execute(select(_counter_table.c.id))))
        assert row_count == threads_n * writes_per_thread


def test_exception_in_one_job_does_not_break_the_next(session_factory, gate):
    with session_factory() as session:

        def _boom() -> None:
            raise ValueError("deliberate")

        with pytest.raises(ValueError, match="deliberate"):
            gate.submit(session, _boom)

        def _insert() -> None:
            session.execute(_counter_table.insert().values(id=99, n=1))

        gate.submit(session, _insert)  # the queue keeps working afterwards

    with session_factory() as fresh:
        row = fresh.execute(
            select(_counter_table.c.n).where(_counter_table.c.id == 99)
        ).first()
        assert row is not None


def test_stop_waits_for_the_queue_to_drain(session_factory, gate):
    with session_factory() as session:

        def _slow_insert() -> None:
            time.sleep(0.2)
            session.execute(_counter_table.insert().values(id=1, n=1))

        result_holder: dict[str, object] = {}

        def _submit_in_background() -> None:
            gate.submit(session, _slow_insert)
            result_holder["ok"] = True

        t = threading.Thread(target=_submit_in_background)
        t.start()
        t.join(timeout=0.05)  # let the job get queued before we stop
        gate.stop()  # must not return before the submitted job above finished
        t.join(timeout=5)
        assert result_holder.get("ok") is True


def test_a_second_process_cannot_take_the_flock_while_a_job_runs(tmp_path, session_factory):
    """The cross-process half (task-66 §2.1): while the gate's own thread
    holds the flock inside a job, a SEPARATE `flock` attempt (standing in
    for the other OS process) must fail non-blockingly; once the job
    finishes, it must succeed.
    """
    import fcntl

    lock_path = tmp_path / "db.write.lock"
    gate = DbWriteGate(lock_path=lock_path)
    try:
        entered = threading.Event()
        release = threading.Event()

        with session_factory() as session:

            def _hold() -> None:
                entered.set()
                release.wait(timeout=5)

            t = threading.Thread(target=lambda: gate.submit(session, _hold))
            t.start()
            assert entered.wait(timeout=5)

            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(lock_path, "a+b") as probe_fd:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            release.set()
            t.join(timeout=5)

            with open(lock_path, "a+b") as probe_fd:
                fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # now free
                fcntl.flock(probe_fd, fcntl.LOCK_UN)
    finally:
        gate.stop()


def test_strict_mode_raises_on_a_slow_job(tmp_path, session_factory):
    gate = DbWriteGate(lock_path=tmp_path / "db.write.lock", slow_ms=10, strict=True)
    try:
        with session_factory() as session:

            def _slow() -> None:
                time.sleep(0.05)
                session.execute(_counter_table.insert().values(id=1, n=1))

            with pytest.raises(RuntimeError, match="db_write_gate_slow_job"):
                gate.submit(session, _slow)
    finally:
        gate.stop()


def test_lenient_mode_only_warns_on_a_slow_job(tmp_path, session_factory, caplog):
    # `logging.disabled` guard against test-order pollution, not this
    # module's own concern: `tests/unit/db/test_alembic_migrations.py`
    # (alphabetically earlier) runs real `alembic upgrade head`, whose
    # `migrations/env.py` calls `logging.config.fileConfig(alembic.ini)` —
    # `fileConfig`'s own default (`disable_existing_loggers=True`) silently
    # disables every logger not explicitly listed in `alembic.ini`'s
    # `[loggers]` section for the rest of the pytest process, this one
    # (`app.db.writegate`) included.
    import logging

    logging.getLogger("app.db.writegate").disabled = False

    gate = DbWriteGate(lock_path=tmp_path / "db.write.lock", slow_ms=10, strict=False)
    try:
        with session_factory() as session:

            def _slow() -> int:
                time.sleep(0.05)
                session.execute(_counter_table.insert().values(id=1, n=1))
                return 7

            with caplog.at_level("WARNING"):
                result = gate.submit(session, _slow)
            assert result == 7
            assert any("db_write_gate_slow_job" in r.getMessage() for r in caplog.records)
    finally:
        gate.stop()


def test_reentrant_submit_from_the_gate_thread_does_not_deadlock(tmp_path, session_factory):
    """LIVE INCIDENT 2026-09-06, the exact shape that froze the system.

    `commit_with_retry` submitted a whole page capture as its `fn`; that
    callable's own closing `write_controller.submit(...)`
    (`app/webarchive/capture.py`) then re-entered the gate FROM the gate's
    single thread — which queued a job only that same thread could run and
    waited on it forever, holding the cross-process flock the whole time.
    The worker froze on its first archived URL and every api endpoint on a
    sync session hung behind it.

    Before the re-entrancy guard this test does not fail, it HANGS — hence
    the hard timeout and the separate thread, so a regression is a red test
    in seconds rather than a wedged suite.
    """
    gate = DbWriteGate(lock_path=tmp_path / "db.write.lock", checkpoint_idle_s=0)
    try:
        with session_factory() as session:

            def _outer() -> str:
                # Runs ON the gate thread; the nested submit is the bug.
                def _inner() -> str:
                    session.execute(_counter_table.insert().values(id=7, n=7))
                    return "inner"

                assert gate.submit(session, _inner) == "inner"
                return "outer"

            result: list[str] = []
            t = threading.Thread(target=lambda: result.append(gate.submit(session, _outer)))
            t.start()
            t.join(timeout=10)
            assert not t.is_alive(), "re-entrant submit deadlocked the gate thread"
            assert result == ["outer"]

        with session_factory() as fresh:
            row = fresh.execute(
                select(_counter_table.c.n).where(_counter_table.c.id == 7)
            ).first()
            assert row is not None and row[0] == 7
    finally:
        gate.stop()


def test_the_flock_is_released_after_a_reentrant_submit(tmp_path, session_factory):
    """The deadlock's real damage was cross-process: a gate thread stuck on
    itself never leaves `_flock`, so the OTHER process's gate — and every
    request behind it — blocks forever. Releasing the flock is therefore
    part of the fix, not a detail of it.
    """
    import fcntl

    lock_path = tmp_path / "db.write.lock"
    gate = DbWriteGate(lock_path=lock_path, checkpoint_idle_s=0)
    try:
        with session_factory() as session:

            def _outer() -> None:
                gate.submit(session, lambda: None)

            t = threading.Thread(target=lambda: gate.submit(session, _outer))
            t.start()
            t.join(timeout=10)
            assert not t.is_alive()

        with open(lock_path, "a+b") as probe_fd:
            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
            fcntl.flock(probe_fd, fcntl.LOCK_UN)
    finally:
        gate.stop()


def test_idle_checkpointer_reclaims_the_wal(tmp_path):
    """task-67 §2. With `wal_autocheckpoint=0` nothing reclaims the WAL on
    its own, so the gate's idle checkpointer is mandatory rather than an
    optimisation — without it the WAL grows without bound (measured: 85MB
    for a 4x-daily-volume burst on the production DB).
    """
    import sqlite3 as _sqlite3

    db_path = tmp_path / "ckpt.db"
    conn = _sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")  # exactly what our PRAGMAs do
    conn.execute("create table t(id integer primary key, blob text)")
    for i in range(400):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("insert into t(id, blob) values(?, ?)", (i, "x" * 4000))
        conn.execute("COMMIT")
    wal_before = (tmp_path / "ckpt.db-wal").stat().st_size
    assert wal_before > 512_000, "test needs a WAL big enough to be worth reclaiming"

    gate = DbWriteGate(
        lock_path=tmp_path / "db.write.lock",
        db_path=db_path,
        checkpoint_idle_s=0.05,
        checkpoint_min_wal_mb=0.1,
    )
    try:
        gate.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (tmp_path / "ckpt.db-wal").stat().st_size < wal_before / 2:
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"WAL not reclaimed: still {(tmp_path / 'ckpt.db-wal').stat().st_size}B")
    finally:
        gate.stop()
        conn.close()

    # The data survived the checkpoint — a checkpoint that loses rows would
    # otherwise pass the size assertion above just fine.
    fresh = _sqlite3.connect(db_path)
    assert fresh.execute("select count(*) from t").fetchone()[0] == 400
    fresh.close()


def test_checkpointer_skips_a_wal_below_the_threshold(tmp_path):
    """The gate thread wakes every idle window; checkpointing a nearly
    empty WAL every 2s would be pure syscall churn on an idle system, so
    below the threshold it must wake up and do nothing at all.
    """
    import sqlite3 as _sqlite3

    db_path = tmp_path / "small.db"
    wal = tmp_path / "small.db-wal"
    # Held open for the whole test: SQLite deletes the `-wal` sibling when
    # the LAST connection closes cleanly, which would erase the very thing
    # being measured here.
    conn = _sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("create table t(id integer primary key)")
    for i in range(50):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("insert into t(id) values(?)", (i,))
        conn.execute("COMMIT")
    wal_before = wal.stat().st_size
    assert wal_before > 0

    gate = DbWriteGate(
        lock_path=tmp_path / "db.write.lock",
        db_path=db_path,
        checkpoint_idle_s=0.05,
        checkpoint_min_wal_mb=100.0,  # far above this tiny WAL
    )
    calls: list[int] = []
    original = gate._checkpoint

    def _counting() -> None:
        calls.append(1)
        original()

    gate._checkpoint = _counting  # type: ignore[method-assign]
    try:
        gate.start()
        time.sleep(0.4)
        assert len(calls) >= 2, "the idle loop should keep waking up"
        assert wal.stat().st_size == wal_before, "WAL below the threshold must be left alone"
    finally:
        gate.stop()
        conn.close()


class _BusyCursor:
    """SQLite reports `(busy, log, checkpointed)`; `busy=1` means the
    checkpoint could not get its locks.
    """

    def fetchone(self):
        return (1, -1, -1)


class _BusyConn:
    """Stand-in for a connection whose checkpoint always reports busy — a
    real reader race is not reproducible on demand in a test.
    """

    def execute(self, sql, *args):
        return _BusyCursor()

    def close(self):
        return None


def test_checkpoint_backs_off_while_the_wal_stays_busy(tmp_path, monkeypatch):
    """A checkpoint that cannot get its locks must not be re-probed every
    idle window: it runs on the gate thread while holding the
    cross-process flock, so each attempt is a stall imposed on every
    writer in BOTH processes. Live-measured with a blocking 1000ms
    timeout during a real portfolio capture — 21 worker commits stuck at
    `duration_ms=1012..1023 queue_wait_ms~5000`, i.e. the exact stall this
    task removes, reintroduced by its own checkpointer.
    """
    import sqlite3 as _sqlite3

    db_path = tmp_path / "busy.db"
    conn = _sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("create table t(id integer primary key, blob text)")
    for i in range(300):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("insert into t(id, blob) values(?, ?)", (i, "x" * 4000))
        conn.execute("COMMIT")

    attempts: list[float] = []

    def _always_busy(*args, **kwargs):
        attempts.append(time.monotonic())
        return _BusyConn()

    monkeypatch.setattr("app.db.writegate.sqlite3.connect", _always_busy)
    gate = DbWriteGate(
        lock_path=tmp_path / "db.write.lock",
        db_path=db_path,
        checkpoint_idle_s=0.02,
        checkpoint_min_wal_mb=0.05,
    )
    try:
        gate.start()
        time.sleep(0.6)
    finally:
        gate.stop()
        conn.close()

    # Without back-off a 20ms idle window over 0.6s would attempt ~30
    # times; each doubling (0.02, 0.04, 0.08, ...) must cut that sharply.
    assert 2 <= len(attempts) <= 8, f"back-off not applied: {len(attempts)} attempts"
    gaps = [round(b - a, 3) for a, b in zip(attempts, attempts[1:], strict=False)]
    assert gaps == sorted(gaps), f"back-off must grow monotonically, got {gaps}"
