"""OAuth token + authenticated transport for the direct Google API (task-83 §3.1/§3.4).

One refresh token for the whole portfolio (every client property lives on the
same agency account). The access token is cached **process-wide** under a
`threading.Lock` and refreshed lazily with a 60 s safety margin before
`expires_in` — otherwise eight parallel collection lanes fire eight identical
refresh requests at once.

Secrets never leave this module: the access token, the refresh token and the
client secret go only in POST bodies (never a URL query string), and every
error message is built from an allow-listed response field, never `str(exc)`.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.integrations.google_api.errors import (
    GoogleApiAuthError,
    GoogleApiConfigError,
    GoogleApiError,
    GoogleApiPermissionError,
    GoogleApiRateLimitError,
)

_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Retry budget (task-83 §3.4). Our real load is ~2 requests per client per
# collection, orders of magnitude under GSC's 1200/min/site and GA4's
# 25000 tokens/property/day — so this is a backoff, not a throttle.
_RATE_LIMIT_ATTEMPTS = 3
_RATE_LIMIT_CAP_SECONDS = 30.0
_SERVER_ERROR_RETRIES = 2


@dataclass
class _CachedToken:
    access_token: str
    expires_at_monotonic: float


class GoogleAuth:
    """Holds the refresh token and hands out a live access token."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        timeout_seconds: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not (client_id and client_secret and refresh_token):
            raise GoogleApiConfigError(
                "google_api_client_id / google_api_secret / google_api_refresh_token "
                "must all be set (see scripts/google_oauth_bootstrap.py)"
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._timeout = timeout_seconds
        self._transport = transport
        self._lock = threading.Lock()
        self._cached: _CachedToken | None = None

    def access_token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            now = time.monotonic()
            cached = self._cached
            if (
                not force_refresh
                and cached is not None
                and cached.expires_at_monotonic - 60 > now
            ):
                return cached.access_token
            token, expires_in = self._request_token()
            self._cached = _CachedToken(
                access_token=token,
                expires_at_monotonic=time.monotonic() + expires_in,
            )
            return token

    def _request_token(self) -> tuple[str, float]:
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.post(
                    _TOKEN_URL,
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "refresh_token": self._refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
        except httpx.HTTPError as exc:
            # `type(exc).__name__` only — an httpx error's text can carry the URL.
            raise GoogleApiError(f"token refresh transport error: {type(exc).__name__}") from None
        if response.status_code == 200:
            body = _safe_json(response)
            token = body.get("access_token")
            if not isinstance(token, str) or not token:
                raise GoogleApiAuthError("token refresh returned no access_token")
            try:
                expires_in = float(body.get("expires_in", 3600))
            except (TypeError, ValueError):
                expires_in = 3600.0
            return token, expires_in
        # Google returns {"error": "invalid_grant", "error_description": "..."} —
        # pass only the code, never the description (can echo request detail).
        code = str(_safe_json(response).get("error") or f"http_{response.status_code}")
        raise GoogleApiAuthError(f"token refresh failed: {code}")


class GoogleApiSession:
    """An `httpx.Client` that injects the bearer token and applies the task-83
    §3.4 retry/error-mapping policy. Shared shape for `GscClient`/`Ga4Client`."""

    def __init__(
        self,
        auth: GoogleAuth,
        *,
        timeout_seconds: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._auth = auth
        self._http = httpx.Client(timeout=timeout_seconds, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GoogleApiSession:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST `body` as JSON, return the parsed response object.

        `401` triggers exactly one forced token refresh + retry; `429` /
        `RESOURCE_EXHAUSTED` back off (jittered, capped); `5xx` retries twice;
        `403` splits into permission vs rate limit. Anything else raises
        `GoogleApiError` with a redacted message.
        """
        return self._request("POST", url, body=body)

    def get_json(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """GET `url`, return the parsed response object.

        Same retry/error-mapping policy as `post_json` — the two share
        `_request` so the policy can never drift between verbs. Added by
        task-88 for the read-only GET endpoints its panels need (GA4
        `properties/{id}/metadata` for the property's own key-event names,
        GSC `sites/{site}/sitemaps`); both Search Analytics and the GA4 Data
        API are POST-only, so task-83 had no reason to need this.
        """
        return self._request("GET", url, params=params)

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        refreshed = False
        rate_attempts = 0
        server_retries = 0
        while True:
            token = self._auth.access_token(force_refresh=refreshed)
            try:
                response = self._http.request(
                    method,
                    url,
                    json=body,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                if server_retries < _SERVER_ERROR_RETRIES:
                    server_retries += 1
                    _sleep_backoff(server_retries)
                    continue
                raise GoogleApiError(
                    f"transport error after retries: {type(exc).__name__}"
                ) from None

            status = response.status_code
            if status == 200:
                return _safe_json(response)

            if status == 401 and not refreshed:
                refreshed = True
                continue

            if status == 401:
                raise GoogleApiAuthError("401 after token refresh — re-auth required")

            if status == 403:
                rpc = _rpc_status(response)
                if rpc in ("RESOURCE_EXHAUSTED", "RATE_LIMIT_EXCEEDED"):
                    if rate_attempts < _RATE_LIMIT_ATTEMPTS - 1:
                        rate_attempts += 1
                        _sleep_backoff(rate_attempts, response=response)
                        continue
                    raise GoogleApiRateLimitError("429/403 rate limit outlived the retry budget")
                raise GoogleApiPermissionError(
                    f"403 {rpc or 'PERMISSION_DENIED'} — no access to this property"
                )

            if status == 429:
                if rate_attempts < _RATE_LIMIT_ATTEMPTS - 1:
                    rate_attempts += 1
                    _sleep_backoff(rate_attempts, response=response)
                    continue
                raise GoogleApiRateLimitError("429 rate limit outlived the retry budget")

            if status >= 500:
                if server_retries < _SERVER_ERROR_RETRIES:
                    server_retries += 1
                    _sleep_backoff(server_retries)
                    continue
                raise GoogleApiError(f"server error {status} after {server_retries} retries")

            raise GoogleApiError(f"unexpected HTTP {status}: {_rpc_status(response) or 'error'}")


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _rpc_status(response: httpx.Response) -> str | None:
    error = _safe_json(response).get("error")
    if isinstance(error, dict):
        status = error.get("status")
        return status if isinstance(status, str) else None
    return None


def _sleep_backoff(attempt: int, *, response: httpx.Response | None = None) -> None:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                time.sleep(min(float(retry_after), _RATE_LIMIT_CAP_SECONDS))
                return
            except ValueError:
                pass
    delay = min(2.0**attempt, _RATE_LIMIT_CAP_SECONDS)
    time.sleep(delay + random.uniform(0, delay / 2))


# ── Process-wide singleton (task-83 §3.1) ────────────────────────────────────
_singleton_lock = threading.Lock()
_singleton: GoogleAuth | None = None


def get_google_auth(settings: Settings | None = None) -> GoogleAuth:
    """The one `GoogleAuth` for the process. Raises `GoogleApiConfigError` if
    the environment has no refresh token — the caller turns that into a
    `not_configured` cell, never a fallback to Reporting Ninja (§6.1.3)."""
    global _singleton
    settings = settings or get_settings()
    with _singleton_lock:
        if _singleton is None:
            _singleton = GoogleAuth(
                client_id=settings.google_api_client_id or "",
                client_secret=settings.google_api_secret or "",
                refresh_token=settings.google_api_refresh_token or "",
                timeout_seconds=settings.google_api_timeout_seconds,
            )
        return _singleton


def reset_google_auth_for_tests(auth: GoogleAuth | None = None) -> None:
    """Test-only: the singleton is module-level, so a test that wants a fake
    auth (or a fresh cache) cannot just construct a new one."""
    global _singleton
    with _singleton_lock:
        _singleton = auth
