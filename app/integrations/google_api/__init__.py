"""Direct Google API transport for the three datasets that feed `#/trends`
(task-83): GSC Search Analytics API for `gsc_daily`, GA4 Data API for
`ga4_daily` and `ga4_daily_channels`.

Transport ≠ provider (task-83 §2). `Provider` is unchanged; the choice lives
in `client_integrations.config_json["transport"]` (optional; default
`"reporting_ninja"`) and in the row `provenance`. An absent key is exactly
today's behaviour, so nothing changes for anyone until a deliberate switch.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from app.core.config import Settings, get_settings
from app.integrations.google_api.admin_client import (
    Ga4AdminClient,
    Ga4KeyEvent,
    Ga4PropertyInfo,
)
from app.integrations.google_api.auth import (
    GoogleApiSession,
    GoogleAuth,
    get_google_auth,
    reset_google_auth_for_tests,
)
from app.integrations.google_api.errors import (
    GoogleApiAuthError,
    GoogleApiConfigError,
    GoogleApiError,
    GoogleApiPermissionError,
    GoogleApiRateLimitError,
)
from app.integrations.google_api.ga4_client import Ga4Client, Ga4Row
from app.integrations.google_api.gsc_client import GscClient, GscDailyRow, GscRow, SitemapEntry

Transport = Literal["google_api", "reporting_ninja"]

REPORTING_NINJA: Transport = "reporting_ninja"
GOOGLE_API: Transport = "google_api"
_VALID: frozenset[str] = frozenset({GOOGLE_API, REPORTING_NINJA})

__all__ = [
    "GOOGLE_API",
    "REPORTING_NINJA",
    "Ga4AdminClient",
    "Ga4Client",
    "Ga4KeyEvent",
    "Ga4PropertyInfo",
    "Ga4Row",
    "GoogleApiAuthError",
    "GoogleApiConfigError",
    "GoogleApiError",
    "GoogleApiPermissionError",
    "GoogleApiRateLimitError",
    "GoogleApiSession",
    "GoogleAuth",
    "GscClient",
    "GscDailyRow",
    "GscRow",
    "SitemapEntry",
    "Transport",
    "get_google_auth",
    "open_google_api_session",
    "reset_google_auth_for_tests",
    "resolve_transport",
]


def resolve_transport(
    config_json: Mapping[str, Any] | None,
    *,
    settings: Settings | None = None,
) -> Transport:
    """Which transport fills this client's datasets, in priority order
    (task-83 §6.1): the client's own `config_json["transport"]`, then the
    global `Settings.google_api_default_transport`, then `"reporting_ninja"`.

    This never checks whether the refresh token exists — that check belongs to
    the collection path, which must surface a missing token as `not_configured`
    with a warning, not silently fall back here.
    """
    settings = settings or get_settings()
    raw = str((config_json or {}).get("transport") or "").strip().lower()
    if raw in _VALID:
        return raw  # type: ignore[return-value]
    fallback = str(settings.google_api_default_transport or "").strip().lower()
    return fallback if fallback in _VALID else REPORTING_NINJA  # type: ignore[return-value]


def open_google_api_session(settings: Settings | None = None) -> GoogleApiSession:
    """A ready `GoogleApiSession` built from the process singleton auth.

    Raises `GoogleApiConfigError` when the environment has no refresh token —
    the caller turns that into a `not_configured` cell (§6.1.3).
    """
    settings = settings or get_settings()
    auth = get_google_auth(settings)
    return GoogleApiSession(auth, timeout_seconds=settings.google_api_timeout_seconds)
