from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from app.webarchive import db_retry
from app.webarchive.db_retry import commit_with_retry


@pytest.fixture(autouse=True)
def _instant_retries(monkeypatch):
    """The real delays are deliberately long (they wait out a lock storm
    that SQLite's own 30s `busy_timeout` already failed to survive) — the
    retry LOGIC is what these tests are about, not the wall clock.
    """
    monkeypatch.setattr(db_retry, "LOCK_RETRY_DELAYS_SECONDS", (0.0, 0.0, 0.0))


def test_recovers_from_transient_lock(session) -> None:
    attempts = {"n": 0}

    def _flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        return "ok"

    result = commit_with_retry(session, _flaky)

    assert result == "ok"
    assert attempts["n"] == 3


def test_reraises_unrelated_operational_errors(session) -> None:
    def _always_fails():
        raise OperationalError("INSERT", {}, Exception("no such table: bogus"))

    with pytest.raises(OperationalError, match="no such table"):
        commit_with_retry(session, _always_fails)


def test_gives_up_after_exhausting_retries(session) -> None:
    attempts = {"n": 0}

    def _always_locked():
        attempts["n"] += 1
        raise OperationalError("INSERT", {}, Exception("database is locked"))

    with pytest.raises(OperationalError, match="database is locked"):
        commit_with_retry(session, _always_locked)
    assert attempts["n"] == len(db_retry.LOCK_RETRY_DELAYS_SECONDS)


def test_fn_runs_on_the_caller_thread_not_the_gate_thread(session) -> None:
    """task-67 §1. `fn` is real work, not a staging closure:
    `app/webarchive/capture.py` passes an entire page capture (HTTP fetch
    of the page and every subresource) through here. Running it on the
    gate's single thread holds the ONE global cross-process flock for the
    whole network round-trip — every other writer in BOTH processes waits
    on a remote server — and, because those same callables submit to the
    gate themselves, self-deadlocks it (live incident 2026-09-06).

    Only the commit may hop to the gate. Asserting on the thread identity
    is the only way to pin this down: a test on the RESULT passes happily
    either way, which is exactly how the regression shipped.
    """
    import threading

    caller = threading.current_thread()
    seen: dict[str, threading.Thread] = {}

    def _fn() -> str:
        seen["fn"] = threading.current_thread()
        return "ok"

    assert commit_with_retry(session, _fn) == "ok"
    assert seen["fn"] is caller
    assert seen["fn"].name != "db-write-gate"
