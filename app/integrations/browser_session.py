"""Browser session bridge (task-20, PLAN §8.3/§13/§17/§19.4).

The physical thing this unblocks: an operator needs to SEE a login page and type into it. AISeoTools
already keeps one warm, logged-in Chrome per provider on the HOST (`seo_audit.browser.warm`); this
module is the only thing that talks to it from inside this container, over CDP, via
`network_mode: host` + `SEOTOOLS_REMOTE_CDP_HOST` (see compose.yaml).

Two entry points, both deliberately narrow (PLAN §13 — no arbitrary command/path/URL from a
caller):

  * `check_session(provider)` — cheap liveness probe, no collection run.
  * `start_login(provider)` — opens the provider's login page in the warm Chrome's own
    (host-visible) window and returns a `login_session_id` to poll via `login_status`. Never drives
    the page beyond that one navigation — the human takes it from there, exactly as
    `seo_audit.browser.auth` does.

Both take a `Provider` enum member, never a raw string or CDP URL: `_require_browser_provider`
is the whole allowlist. If AISeoTools is not installed in this container (checkout absent), the
import errors surface as-is — there is nothing meaningful to degrade to.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from app.core.config import get_settings
from app.schemas.enums import Provider

# Providers AISeoTools keeps an actual browser profile for. `reporting_ninja` authenticates via API
# key (seo_audit.clientreport.api_client.ReportingNinjaClient) and has no browser session at all —
# never a valid target here, not even to report `no_profile`.
BROWSER_PROVIDERS: frozenset[Provider] = frozenset(
    {Provider.ahrefs, Provider.semrush, Provider.gsc}
)


class SessionState(str, Enum):
    """What `check_session` can report — three-way, never collapsed to a bool (PLAN invariant:
    absence of data must stay distinguishable from a real 0/failure)."""

    alive = "alive"
    expired = "expired"
    no_profile = "no_profile"


class NoWarmChrome(RuntimeError):
    """No warm host Chrome is reachable for this provider. `remedy` is the exact, ready-to-run
    operator command — the documented fallback path (task-20 item 5), not a dead end."""

    def __init__(self, provider: Provider, remedy: str):
        self.provider = provider
        self.remedy = remedy
        super().__init__(f"{provider.value}: no warm Chrome reachable — {remedy}")


@dataclass
class LoginSession:
    login_session_id: str
    provider: Provider
    started_at: float = field(default_factory=time.monotonic)


# Single worker process (PLAN §5.1, same assumption as app.integrations.registry) — a plain
# module-level dict is sufficient bookkeeping for "which login did I just trigger".
_active_logins: dict[str, LoginSession] = {}


def _require_browser_provider(provider: Provider) -> None:
    if provider not in BROWSER_PROVIDERS:
        allowed = ", ".join(sorted(p.value for p in BROWSER_PROVIDERS))
        raise ValueError(f"{provider!r} has no browser session (allowed: {allowed})")


def _fallback_hint(provider: Provider) -> str:
    return (
        f"on the machine running the warm Chrome: seo-audit browser-up --provider {provider.value}"
        f" (opens a visible, logged-in window that stays open for later runs)"
    )


def check_session(provider: Provider) -> SessionState:
    """Is the stored session for `provider` still good? Cheap — never runs a full collection.

    `no_profile`: nothing has ever been logged in (no persistent Chrome profile on disk yet —
    first-time setup, not a failure). `expired`: a profile exists but the warm Chrome is down,
    unreachable, or its current tab is not on an authenticated provider URL. `alive`: logged in
    right now, OR another run is actively driving the warm browser (busy) — busy IS a form of
    alive, not a reason to report a false expiry mid-collection.

    `no_profile` is only ever returned on the machine that OWNS the profile. This container
    deliberately does not mount the profile directory (see compose.yaml), so `profile.exists` is
    False here even when the host is fully logged in — returning `no_profile` from that would be
    a flat lie, and it is what made the dashboard offer first-time login for every provider while
    a live, logged-in warm Chrome sat right behind it. For a consumer the warm Chrome is the only
    truth there is, so the probe starts at the warm state instead.
    """
    _require_browser_provider(provider)
    from seo_audit.browser import warm as seo_warm
    from seo_audit.browser.profile import BrowserProfile

    profile = BrowserProfile(provider.value)
    if not seo_warm.attach_only() and not profile.exists:
        return SessionState.no_profile

    try:
        status = seo_warm.status(provider.value)
    except Exception:
        return SessionState.expired

    if not status.running:
        return SessionState.expired
    if status.busy:
        return SessionState.alive
    return SessionState.alive if status.authenticated else SessionState.expired


def start_login(provider: Provider) -> str:
    """Open `provider`'s login page in the warm Chrome's own window — visible to whoever is at the
    host screen — and return a `login_session_id` to poll via `login_status`/`check_session`.

    Requires a warm Chrome already running (`NoWarmChrome` names the exact fix otherwise): this
    container has no Chrome binary and no visible display of its own, so it can only navigate an
    ALREADY-OPEN host window, never launch one. The busy lock is held only for the duration of the
    one navigation, not for however long the human takes to finish signing in — mirroring
    `seo_audit.browser.auth.login`'s rule of touching the page once, then leaving it alone.
    """
    _require_browser_provider(provider)
    from seo_audit.browser import warm as seo_warm
    from seo_audit.browser.profile import BrowserProfile
    from seo_audit.browser.stealth import playwright_api

    profile = BrowserProfile(provider.value)
    cdp_url = seo_warm.warm_cdp_url(provider.value)
    if not cdp_url:
        raise NoWarmChrome(provider, _fallback_hint(provider))

    settings = get_settings()
    with profile.busy_lock():
        pw_ctx = playwright_api()()
        pw = pw_ctx.__enter__()
        try:
            browser = pw.chromium.connect_over_cdp(cdp_url, timeout=10_000)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(
                profile.login_url,
                wait_until="domcontentloaded",
                timeout=int(settings.browser_login_timeout_seconds * 1000),
            )
            # Never browser.close() here — this is the shared warm Chrome, not one we own; closing
            # it would kill the very window the operator is about to use.
        finally:
            try:
                pw_ctx.__exit__(None, None, None)
            except Exception:
                pass

    login_session_id = uuid.uuid4().hex
    _active_logins[login_session_id] = LoginSession(login_session_id, provider)
    return login_session_id


def login_status(login_session_id: str) -> SessionState:
    """Poll a `start_login` in progress — just `check_session` scoped to that login's provider."""
    session = _active_logins.get(login_session_id)
    if session is None:
        raise KeyError(f"unknown login_session_id {login_session_id!r}")
    return check_session(session.provider)
