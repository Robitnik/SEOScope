from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from app.webarchive.db_retry import commit_with_retry


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
    assert attempts["n"] == 3  # exactly the configured number of attempts
