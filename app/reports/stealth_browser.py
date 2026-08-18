"""Stealth Chromium launch for Reporting Ninja's own site (task-47 follow-up).

Scoped to `ninja_browser.py` alone — the only module in this package that
drives a THIRD-PARTY site. `client_pdf.py` renders this app's own
`/document` page; there is nothing to look "less automated" for there.

Prefers `patchright` — a Playwright fork that patches known CDP/webdriver/
headless automation fingerprints at the browser-protocol level, not just in
page-visible JS — over plain `playwright`, falling back to it when
`patchright` isn't installed. This is the SAME choice AISeoTools' own
`seo_audit/collect/render.py` already made for its Playwright render pass
("stealth: patches CDP/webdriver/headless leaks"); `patchright` arrives here
as that project's transitive dependency, so this is picking up an existing,
already-live convention rather than introducing a new one. Both packages
expose the identical generated API shape (same method names on `Browser`/
`BrowserContext`/`Page`), so every other line in `ninja_browser.py` works
unmodified regardless of which one actually loaded — only the TWO things
that differ (which package's `TimeoutError` class an attempt raises, and how
the browser is launched/contexted) are handled here.

Why this matters beyond politeness: an automated login that announces
itself as headless/bot-shaped is exactly the kind of thing a SaaS site
serves a slower, degraded, or intermittently-blocked path to — indistinguish-
able, from the outside, from the "the report is just slow to render"
timeouts task-47's own retry fix was written for. This is the agency's own
paid Reporting Ninja account, automated with its own real credentials; nothing
here evades access control, it just stops LOOKING like automation traffic
serving a machine no different from a human operator.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

try:
    from patchright.sync_api import sync_playwright as _sync_playwright
    STEALTH_DRIVER = "patchright"
except ImportError:  # pragma: no cover - patchright is a transitive AISeoTools dep
    from playwright.sync_api import sync_playwright as _sync_playwright  # type: ignore[assignment]
    STEALTH_DRIVER = "playwright"

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext

# A realistic, current desktop Chrome fingerprint. Kept in sync with the
# major version AISeoTools' own `seo_audit/collect/headers.py` UA pool uses.
STEALTH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
STEALTH_LOCALE = "en-US"
STEALTH_TIMEZONE = "America/New_York"
STEALTH_VIEWPORT = {"width": 1440, "height": 900}
# The one JS-visible flag `navigator.webdriver` still leaves in plain
# Chromium. `patchright` already patches this below the JS layer (CDP
# itself never announces it), so this is a no-op there — but costs nothing,
# and is the one thing that still matters on the plain-`playwright` fallback
# path (no `patchright` installed). Same defense-in-depth AISeoTools'
# `render.py` already applies for the same reason.
_HIDE_WEBDRIVER_SCRIPT = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"


def sync_playwright() -> Any:
    """Drop-in for `playwright.sync_api.sync_playwright` — use identically:
    `with sync_playwright() as playwright: browser = playwright.chromium.launch(...)`.
    """
    return _sync_playwright()


def new_stealth_context(browser: Browser, **overrides: Any) -> BrowserContext:
    """`browser.new_context(...)` pre-filled with the fingerprint above.
    Any keyword here can still be overridden (`storage_state`, a call site
    that needs something specific) — `overrides` always wins.
    """
    kwargs: dict[str, Any] = {
        "viewport": STEALTH_VIEWPORT,
        "user_agent": STEALTH_USER_AGENT,
        "locale": STEALTH_LOCALE,
        "timezone_id": STEALTH_TIMEZONE,
    }
    kwargs.update(overrides)
    ctx = browser.new_context(**kwargs)
    ctx.add_init_script(_HIDE_WEBDRIVER_SCRIPT)
    return ctx


def is_timeout_error(exc: BaseException) -> bool:
    """`patchright` and `playwright` each raise their OWN `TimeoutError`
    class (`patchright._impl._errors.TimeoutError` vs
    `playwright._impl._errors.TimeoutError`) — genuinely different classes,
    not a shared base, confirmed live 2026-08-18. An `isinstance` check
    against one package's class silently stops matching the moment the
    other driver is the one actually installed. Both packages deliberately
    keep the same class NAME for exactly this kind of duck-typed check.
    """
    return type(exc).__name__ == "TimeoutError"
