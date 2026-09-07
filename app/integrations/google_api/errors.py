"""Typed errors for the direct Google API transport (task-83 §3.4).

HTTP status / RPC `status` codes are mapped onto this small hierarchy so the
calling side (`gsc.py` / `reporting_ninja.py` dataset forks) branches on the
*kind* of failure, not on a raw string:

- `GoogleApiConfigError`  — no refresh token / client credentials. The cell
  must become `not_configured` with an explicit warning, NEVER a silent
  fallback to Reporting Ninja (task-83 §6.1.3).
- `GoogleApiAuthError`    — `401` survived one retried token refresh, or the
  refresh itself returned `invalid_grant`. "re-auth required".
- `GoogleApiPermissionError` — `403 PERMISSION_DENIED`: the agency account
  has no access to this property. This is `not_configured`, not `failed`: a
  client we simply cannot read must not paint the lane red (PLAN §9.2).
- `GoogleApiRateLimitError` — `429` / `RESOURCE_EXHAUSTED` after the bounded
  backoff gave up.
- `GoogleApiError`        — everything else (exhausted `5xx` retries, an
  unexpected body shape).

Every message here is built from an allow-listed field of the response body
(`error`, `error.status`, `error.message`) — never `str(exc)` over an httpx
exception, whose text can carry the request URL. Access tokens, the refresh
token and the client secret never travel in `data=` query strings (only in
form/JSON bodies), so they cannot leak through a URL either.
"""

from __future__ import annotations


class GoogleApiError(Exception):
    """Base class — a Google API call failed in a way the caller should treat
    as "dataset unavailable this run", not crash on."""


class GoogleApiConfigError(GoogleApiError):
    """The `google_api` transport was selected but is not usable: no
    `google_api_refresh_token` (or client id/secret) in the environment."""


class GoogleApiAuthError(GoogleApiError):
    """Authentication failed and a single retried token refresh did not fix
    it — the refresh token is revoked or expired. Recovery is a new OAuth
    bootstrap (`scripts/google_oauth_bootstrap.py`)."""


class GoogleApiPermissionError(GoogleApiError):
    """`403 PERMISSION_DENIED`: the agency account cannot read this property.
    Maps to `not_configured`, never `failed` (task-83 §3.4)."""


class GoogleApiRateLimitError(GoogleApiError):
    """`429` / `RESOURCE_EXHAUSTED` that outlived the retry budget."""
