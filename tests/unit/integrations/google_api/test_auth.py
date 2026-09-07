"""GoogleAuth / GoogleApiSession — token cache, retry, secret redaction (task-83 §10.1/§10.4)."""

from __future__ import annotations

import httpx
import pytest

from app.integrations.google_api.auth import GoogleApiSession, GoogleAuth
from app.integrations.google_api.errors import (
    GoogleApiAuthError,
    GoogleApiConfigError,
    GoogleApiError,
    GoogleApiPermissionError,
    GoogleApiRateLimitError,
)

from .fakes import FakeGoogle, collect_strings

SECRET = "refresh-value-shh"
CLIENT_SECRET = "secret-value-shh"


def test_missing_credentials_raise_config_error() -> None:
    with pytest.raises(GoogleApiConfigError):
        GoogleAuth(client_id="", client_secret="", refresh_token="")


def test_access_token_is_cached_across_calls(fake_google: FakeGoogle) -> None:
    auth = fake_google.auth()
    first = auth.access_token()
    second = auth.access_token()
    assert first == second == "access-xyz"
    assert fake_google.refresh_count == 1  # cached, not refreshed twice


def test_expired_token_is_refreshed(fake_google: FakeGoogle) -> None:
    fake_google.on(
        "oauth2.googleapis.com/token",
        [
            httpx.Response(200, json={"access_token": "old", "expires_in": 30}),
            httpx.Response(200, json={"access_token": "new", "expires_in": 3599}),
        ],
    )
    auth = fake_google.auth()
    assert auth.access_token() == "old"
    # 30s expiry is inside the 60s safety margin -> next call refreshes.
    assert auth.access_token() == "new"


def test_401_triggers_one_refresh_then_retry(fake_google: FakeGoogle) -> None:
    calls: list[str] = []

    def api(request: httpx.Request) -> httpx.Response:
        token = request.headers["Authorization"]
        calls.append(token)
        if len(calls) == 1:
            return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})
        return httpx.Response(200, json={"ok": True})

    fake_google.on("example.com/api", api)
    session = fake_google.session()
    result = session.post_json("https://example.com/api", {})
    assert result == {"ok": True}
    assert len(calls) == 2
    assert fake_google.refresh_count == 2  # initial + one forced refresh


def test_401_twice_raises_auth_error(fake_google: FakeGoogle) -> None:
    fake_google.on("example.com/api", httpx.Response(401, json={"error": {"status": "X"}}))
    with pytest.raises(GoogleApiAuthError):
        fake_google.session().post_json("https://example.com/api", {})


def test_invalid_grant_on_refresh_raises_auth_error_without_leaking_secret(
    fake_google: FakeGoogle,
) -> None:
    fake_google.on(
        "oauth2.googleapis.com/token",
        httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": f"token {SECRET} bad"},
        ),
    )
    auth = fake_google.auth()
    with pytest.raises(GoogleApiError) as excinfo:
        auth.access_token()
    message = str(excinfo.value)
    assert "invalid_grant" in message
    assert SECRET not in message
    assert "error_description" not in message  # only the code is passed through


def test_403_permission_denied_maps_to_permission_error(fake_google: FakeGoogle) -> None:
    fake_google.on(
        "example.com/api",
        httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED", "message": "nope"}}),
    )
    with pytest.raises(GoogleApiPermissionError):
        fake_google.session().post_json("https://example.com/api", {})


def test_429_backs_off_then_succeeds_on_third_attempt(
    fake_google: FakeGoogle, no_sleep: list[float]
) -> None:
    responses = [
        httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}}),
        httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}}),
        httpx.Response(200, json={"ok": 1}),
    ]
    fake_google.on("example.com/api", responses)
    result = fake_google.session().post_json("https://example.com/api", {})
    assert result == {"ok": 1}
    assert len(no_sleep) == 2  # two backoff sleeps for two 429s


def test_429_outlives_budget_raises_rate_limit_error(
    fake_google: FakeGoogle, no_sleep: list[float]
) -> None:
    fake_google.on("example.com/api", httpx.Response(429, json={}))
    with pytest.raises(GoogleApiRateLimitError):
        fake_google.session().post_json("https://example.com/api", {})


def test_5xx_retries_twice_then_raises(fake_google: FakeGoogle, no_sleep: list[float]) -> None:
    fake_google.on("example.com/api", httpx.Response(503, json={}))
    with pytest.raises(GoogleApiError):
        fake_google.session().post_json("https://example.com/api", {})
    api_calls = [r for r in fake_google.requests if "example.com/api" in str(r.url)]
    assert len(api_calls) == 3  # initial + 2 retries


def test_no_secret_in_any_error_path(fake_google: FakeGoogle) -> None:
    """A blanket check: whatever the failure, `str(exc)` never contains the
    refresh token or client secret (task-83 §10.1)."""
    fake_google.on("example.com/api", httpx.Response(500, json={"detail": "boom"}))
    session = fake_google.session()
    with pytest.raises(GoogleApiError) as excinfo:
        session.post_json("https://example.com/api", {})
    blob = " ".join(collect_strings({"msg": str(excinfo.value), "args": excinfo.value.args}))
    assert SECRET not in blob
    assert CLIENT_SECRET not in blob
    assert "access-xyz" not in blob


def test_token_never_in_request_url(fake_google: FakeGoogle) -> None:
    fake_google.on("example.com/api", httpx.Response(200, json={}))
    fake_google.session().post_json("https://example.com/api", {"q": 1})
    for request in fake_google.requests:
        assert "access-xyz" not in str(request.url)
        assert SECRET not in str(request.url)
        # refresh creds go in the POST body, never the query string
        assert CLIENT_SECRET not in str(request.url)


def test_transport_error_is_wrapped_not_reraised(no_sleep: list[float]) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        if "oauth2.googleapis.com/token" in str(request.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3599})
        raise httpx.ConnectError("dns failed for secret-host")

    transport = httpx.MockTransport(boom)
    auth = GoogleAuth(
        client_id="c", client_secret="s", refresh_token="r", transport=transport
    )
    session = GoogleApiSession(auth, transport=transport)
    with pytest.raises(GoogleApiError) as excinfo:
        session.post_json("https://example.com/api", {})
    assert "secret-host" not in str(excinfo.value)
