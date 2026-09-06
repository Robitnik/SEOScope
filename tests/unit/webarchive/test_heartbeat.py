"""Unit tests for the gate-independent heartbeat write (task-69 §1)."""

from __future__ import annotations

import fcntl
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.db.repositories.clients import ClientRepository
from app.db.repositories.web_archive import WebArchiveRepository
from app.db.session import default_write_lock_path
from app.webarchive.heartbeat import refresh_heartbeat_best_effort


def _client_and_capture(session):
    client = ClientRepository(session).create(
        name="Acme", slug="acme", domain="acme.com", timezone="America/New_York"
    )
    session.commit()
    repo = WebArchiveRepository(session)
    repo.get_or_create_settings(client.id)
    capture = repo.create_capture(
        client.id,
        slot_key="2026-01-01-1330",
        scheduled_for=datetime(2026, 1, 1, 13, 30, tzinfo=UTC),
        trigger="manual",
    )
    session.commit()
    return capture


def test_refresh_heartbeat_writes_when_lock_is_free(db_settings, session) -> None:
    capture = _client_and_capture(session)
    stale = datetime(2020, 1, 1, tzinfo=UTC)
    session.execute(
        text("UPDATE archive_captures SET heartbeat_at = :hb WHERE id = :id"),
        {"hb": stale, "id": capture.id},
    )
    session.commit()

    ok = refresh_heartbeat_best_effort(db_settings, capture.id)

    assert ok is True
    session.expire_all()
    repo = WebArchiveRepository(session)
    refreshed = repo.get_capture(capture.id)
    assert refreshed is not None
    assert refreshed.heartbeat_at is not None
    assert refreshed.heartbeat_at.replace(tzinfo=UTC) > stale + timedelta(days=1)


def test_refresh_heartbeat_skips_silently_when_lock_held(db_settings, session) -> None:
    capture = _client_and_capture(session)
    lock_path = default_write_lock_path(db_settings)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    holder = open(lock_path, "a+b")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        ok = refresh_heartbeat_best_effort(db_settings, capture.id)
        assert ok is False  # must not raise, must not block
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()


def test_refresh_heartbeat_unknown_capture_returns_false(db_settings) -> None:
    # No row exists yet for this id — the UPDATE affects zero rows.
    assert refresh_heartbeat_best_effort(db_settings, "does-not-exist") is False


def test_refresh_heartbeat_never_blocks_caller(db_settings, session) -> None:
    """The whole point of the direct path: a lock held by ANOTHER thread for
    a long time must not make this call wait for it — it must return
    (with `False`) near-instantly.
    """
    capture = _client_and_capture(session)
    lock_path = default_write_lock_path(db_settings)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    release = threading.Event()

    def _hold_lock() -> None:
        fd = open(lock_path, "a+b")
        fcntl.flock(fd, fcntl.LOCK_EX)
        release.wait(timeout=5)
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()

    holder_thread = threading.Thread(target=_hold_lock)
    holder_thread.start()
    try:
        # Give the holder thread a moment to actually acquire the lock.
        time.sleep(0.2)
        started = time.monotonic()
        ok = refresh_heartbeat_best_effort(db_settings, capture.id)
        elapsed = time.monotonic() - started
        assert ok is False
        assert elapsed < 1.0
    finally:
        release.set()
        holder_thread.join(timeout=5)


def test_refresh_heartbeat_does_not_corrupt_db_under_real_contention(db_settings, session) -> None:
    """A direct connection bypassing the ORM must still respect the same
    file lock as any other writer — sanity check that concurrent direct
    writes from multiple threads never crash with `database is locked`
    because each one takes the flock first.
    """
    capture = _client_and_capture(session)
    errors: list[Exception] = []

    def _hammer() -> None:
        for _ in range(20):
            try:
                refresh_heartbeat_best_effort(db_settings, capture.id)
            except Exception as exc:  # noqa: BLE001 - test assertion, not app code
                errors.append(exc)

    threads = [threading.Thread(target=_hammer) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    # The row must still be readable and consistent afterwards.
    conn = sqlite3.connect(str(db_settings.database_url.split("sqlite:///", 1)[-1]))
    try:
        row = conn.execute(
            "SELECT heartbeat_at FROM archive_captures WHERE id = ?", (capture.id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] is not None
