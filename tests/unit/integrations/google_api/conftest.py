"""Fixtures for the direct Google API transport tests (task-83 §10)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.integrations.google_api.auth import reset_google_auth_for_tests

from .fakes import FakeGoogle


@pytest.fixture(autouse=True)
def _reset_google_auth_singleton() -> Iterator[None]:
    reset_google_auth_for_tests(None)
    yield
    reset_google_auth_for_tests(None)


@pytest.fixture
def fake_google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Capture backoff sleeps instead of waiting."""
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)
    return slept
