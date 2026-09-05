"""Спільний HTTP-safety шар інструментів (task-29).

Гард сам покритий у `test_verify.py` (через реекспорт) — тут перевіряємо, що
витягання не зламало наявний контракт, і те, що є лише тут: politeness-гард.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.tools import http_safety
from app.tools.alt_text import verify
from app.tools.http_safety import HostThrottle, UnsafeUrlError, assert_public_http_url, safe_host


@pytest.mark.parametrize(
    "name",
    [
        "CACHE_BUSTER_PARAM",
        "MAX_REDIRECTS",
        "USER_AGENT",
        "UnsafeUrlError",
        "assert_public_http_url",
        "build_cache_busted_url",
    ],
)
def test_verify_still_exposes_the_moved_names(name: str) -> None:
    """Витягання не має ламати виклики й тести task-27."""
    assert getattr(verify, name) is getattr(http_safety, name)


def test_unsafe_url_error_keeps_its_code() -> None:
    assert UnsafeUrlError.code == "unsafe_url"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/page?token=secret", "example.com"),
        ("http://sub.example.com:8080/x", "sub.example.com"),
        ("not a url", "the host"),
    ],
)
def test_safe_host_never_leaks_the_query(url: str, expected: str) -> None:
    assert safe_host(url) == expected


def test_guard_rejects_loopback() -> None:
    with pytest.raises(UnsafeUrlError):
        assert_public_http_url("http://127.0.0.1/admin")


class TestHostThrottle:
    def test_requests_to_one_host_are_serialized(self) -> None:
        throttle = HostThrottle(min_interval_seconds=0.0)
        overlaps = 0
        active = 0
        guard = threading.Lock()

        def worker() -> None:
            nonlocal overlaps, active
            with throttle.slot("example.com"):
                with guard:
                    active += 1
                    if active > 1:
                        overlaps += 1
                time.sleep(0.02)
                with guard:
                    active -= 1

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert overlaps == 0

    def test_different_hosts_do_not_wait_for_each_other(self) -> None:
        throttle = HostThrottle(min_interval_seconds=0.2)
        started = time.monotonic()
        with throttle.slot("a.example"):
            pass
        with throttle.slot("b.example"):
            pass
        assert time.monotonic() - started < 0.15

    def test_second_call_to_the_same_host_waits_the_interval(self) -> None:
        throttle = HostThrottle(min_interval_seconds=0.1)
        started = time.monotonic()
        with throttle.slot("a.example"):
            pass
        with throttle.slot("a.example"):
            pass
        assert time.monotonic() - started >= 0.1

    def test_host_matching_is_case_insensitive(self) -> None:
        throttle = HostThrottle(min_interval_seconds=0.1)
        started = time.monotonic()
        with throttle.slot("A.example"):
            pass
        with throttle.slot("a.EXAMPLE"):
            pass
        assert time.monotonic() - started >= 0.1


class TestHostThrottleConcurrency:
    """task-58: `concurrency > 1` is a distinct code path — these pin that
    `concurrency=1` (the default, every existing caller) is untouched and
    that the new path actually allows overlap up to the given bound."""

    def test_concurrency_one_is_the_unchanged_mutex_behavior(self) -> None:
        throttle = HostThrottle(min_interval_seconds=0.0, concurrency=1)
        overlaps = 0
        active = 0
        guard = threading.Lock()

        def worker() -> None:
            nonlocal overlaps, active
            with throttle.slot("example.com"):
                with guard:
                    active += 1
                    if active > 1:
                        overlaps += 1
                time.sleep(0.02)
                with guard:
                    active -= 1

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert overlaps == 0

    def test_concurrency_n_allows_up_to_n_overlapping_requests(self) -> None:
        throttle = HostThrottle(min_interval_seconds=0.0, concurrency=3)
        max_active = 0
        active = 0
        guard = threading.Lock()

        def worker() -> None:
            nonlocal max_active, active
            with throttle.slot("example.com"):
                with guard:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                with guard:
                    active -= 1

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert max_active == 3

    def test_concurrency_n_never_exceeds_the_bound(self) -> None:
        throttle = HostThrottle(min_interval_seconds=0.0, concurrency=2)
        active = 0
        guard = threading.Lock()
        breached = False

        def worker() -> None:
            nonlocal active, breached
            with throttle.slot("example.com"):
                with guard:
                    active += 1
                    if active > 2:
                        breached = True
                time.sleep(0.03)
                with guard:
                    active -= 1

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not breached

    def test_concurrency_n_min_interval_gates_starts_not_completions(self) -> None:
        """A slow first request must not delay a second request's START by
        its own duration — only by `min_interval`. That is exactly the
        "slowest page dictates everyone's pace" bug `concurrency=1`'s
        completion-based gating (`_last_call` set in `finally`, after the
        request returns) would reintroduce."""
        throttle = HostThrottle(min_interval_seconds=0.05, concurrency=2)
        second_started_at: list[float] = []

        def slow_worker() -> None:
            with throttle.slot("example.com"):
                time.sleep(0.4)  # much longer than min_interval

        def fast_worker() -> None:
            with throttle.slot("example.com"):
                second_started_at.append(time.monotonic())

        started = time.monotonic()
        t1 = threading.Thread(target=slow_worker)
        t1.start()
        time.sleep(0.01)  # let the slow one actually acquire its slot first
        t2 = threading.Thread(target=fast_worker)
        t2.start()
        t1.join()
        t2.join()
        # The fast request starts once ~min_interval has passed since the
        # slow one STARTED (~0.05s in) — nowhere near the slow one's 0.4s
        # completion, which a completion-gated throttle would have forced.
        assert second_started_at[0] - started < 0.2

    def test_concurrency_n_still_respects_min_interval_between_starts(self) -> None:
        throttle = HostThrottle(min_interval_seconds=0.15, concurrency=4)
        starts: list[float] = []
        guard = threading.Lock()

        def worker() -> None:
            with throttle.slot("example.com"):
                with guard:
                    starts.append(time.monotonic())
                time.sleep(0.01)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        starts.sort()
        gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
        assert all(gap >= 0.13 for gap in gaps)  # small slack for scheduling jitter

    def test_concurrency_n_different_hosts_are_independent(self) -> None:
        throttle = HostThrottle(min_interval_seconds=0.0, concurrency=2)
        with throttle.slot("a.example"):
            with throttle.slot("b.example"):
                pass  # no deadlock, no cross-host blocking
