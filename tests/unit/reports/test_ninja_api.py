from __future__ import annotations

import httpx
import pytest

from app.reports.ninja_api import APIError, RateLimitError, ReportingNinjaClient


def _client(handler) -> ReportingNinjaClient:
    client = ReportingNinjaClient("fake-key")
    client._http = httpx.Client(
        base_url="https://api.reportingninja.com/v1",
        transport=httpx.MockTransport(handler),
    )
    return client


def test_query_returns_rows_on_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "data": {"rows": [{"clicks": 1}]}})

    client = _client(handler)
    rows = client.query("gsc", "seo@x", "sc-domain:x.com", ["clicks"], "2026-07-01", "2026-07-31")
    assert rows == [{"clicks": 1}]
    assert client.request_count == 1


def test_internal_error_is_retried_and_then_succeeds(monkeypatch):
    monkeypatch.setattr("app.reports.ninja_api.time.sleep", lambda _seconds: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200, json={"status": "error", "error_code": "INTERNAL", "message": "boom"}
            )
        return httpx.Response(200, json={"status": "ok", "data": {"rows": []}})

    client = _client(handler)
    client._request("/query", {}, retries=3)
    assert calls["n"] == 2
    assert client.request_count == 2


def test_internal_error_raises_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr("app.reports.ninja_api.time.sleep", lambda _seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "error", "error_code": "INTERNAL", "message": "boom"}
        )

    client = _client(handler)
    with pytest.raises(APIError) as exc_info:
        client._request("/query", {}, retries=2)
    assert exc_info.value.error_code == "INTERNAL"


def test_auth_error_is_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200, json={"status": "error", "error_code": "UNAUTHORIZED", "message": "no"}
        )

    client = _client(handler)
    with pytest.raises(APIError):
        client._request("/query", {}, retries=3)
    assert calls["n"] == 1


def test_transient_5xx_is_retried(monkeypatch):
    monkeypatch.setattr("app.reports.ninja_api.time.sleep", lambda _seconds: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={})
        return httpx.Response(200, json={"status": "ok", "data": {"rows": []}})

    client = _client(handler)
    client._request("/query", {}, retries=3)
    assert calls["n"] == 2


def test_429_raises_rate_limit_error_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr("app.reports.ninja_api.time.sleep", lambda _seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "12"}, json={})

    client = _client(handler)
    with pytest.raises(RateLimitError) as exc_info:
        client._request("/query", {}, retries=2)
    assert exc_info.value.retry_after == 12.0


def test_empty_api_key_is_rejected():
    with pytest.raises(ValueError):
        ReportingNinjaClient("")


# --- Rate limiter (task-47 §1.6/§2.3) ---------------------------------------


def test_rate_limiter_throttles_a_burst_past_the_configured_rate(monkeypatch):
    """280 requests against a low `reports_api_max_rpm` must not run faster
    than the configured rate allows — the account ceiling this guards is
    300 req/min, and a 14-client batch alone issues ~280 of them (task-47
    §1.6, measured live).
    """
    from app.reports import ninja_api as ninja_api_module

    fake_now = [0.0]
    sleeps: list[float] = []

    def fake_monotonic() -> float:
        return fake_now[0]

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        fake_now[0] += seconds

    monkeypatch.setattr(ninja_api_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(ninja_api_module.time, "sleep", fake_sleep)
    ninja_api_module.reset_rate_limiter_for_tests(60.0)  # 1 request/second

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "data": {"rows": []}})

    client = _client(handler)
    for _ in range(65):
        client._request("/query", {}, retries=1)

    # 60 tokens available immediately (the full bucket), the remaining 5
    # requests must each wait for a fresh token — 65 requests never complete
    # in less than ~5 simulated seconds at this rate.
    assert fake_now[0] >= 4.0
    assert client.request_count == 65


def test_rate_limiter_does_not_throttle_within_the_configured_burst(monkeypatch):
    from app.reports import ninja_api as ninja_api_module

    ninja_api_module.reset_rate_limiter_for_tests(240.0)
    sleep_calls: list[float] = []
    monkeypatch.setattr(ninja_api_module.time, "sleep", lambda s: sleep_calls.append(s))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "data": {"rows": []}})

    client = _client(handler)
    for _ in range(50):
        client._request("/query", {}, retries=1)

    assert sleep_calls == []
