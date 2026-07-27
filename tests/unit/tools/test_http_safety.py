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
