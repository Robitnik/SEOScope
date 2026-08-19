"""Browser-session fallback for the Redirection plugin's export (task-51

follow-up, live finding 2026-08-19): none of the 10 portfolio clients with
`redirection/v1` had ever had an Application Password issued —
`wp_redirection.py`'s Basic-Auth path returned `auth_failed` on every one —
while the agency's own regular wp-admin login worked (confirmed live on
`effyscafe.com`/`grillpointnyc.com`, 1 and 430 real redirects respectively).
This module drives a real (stealth) browser through that regular login, then
reaches the exact same REST export endpoint `wp_redirection.py` already
parses — cookie + nonce authentication instead of Basic Auth, nothing else
different.

**Why not just click the Import/Export screen's own Export button.** The
React admin app (`tools.php?page=redirection.php&sub=io`) already calls this
same REST route itself, authenticated with a nonce it embeds directly in the
page (`Redirectioni10n.api.WP_API_nonce`, `wp_localize_script` in the
plugin's own `redirection-admin.php`). Reusing that nonce against the REST
API directly is the same data with far less that can go wrong than driving
the button's own click/download UI (file-chooser dialogs, download
interception, format-menu state).

**Why the nonce is read via regex on `<script>` text, not `page.evaluate`.**
`patchright` (this project's stealth Chromium fork, `app/reports/
stealth_browser.py`) runs `page.evaluate` in an isolated JS world as part of
its anti-fingerprinting design — a `var` a page's own inline script declares
never becomes visible there (confirmed live: DOM `textContent` of the
script tag WAS visible via `page.evaluate`; `typeof Redirectioni10n` from
the same call was not). The DOM itself is shared across worlds, so reading
the raw script text and parsing it in Python sidesteps the isolation
entirely rather than fighting it.

**Why cookies+nonce go into a plain `urllib` request instead of an in-page
`fetch()`.** Once the nonce is in hand there is no reason to keep the
browser open for the actual data call — a plain HTTP request under the same
SSRF/throttle guard (`app.tools.http_safety`) every other adapter in this
package already uses is simpler, and one less thing that depends on
patchright's JS-world quirks.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from app.monitor.redirects.contract import RedirectSourceResult
from app.monitor.redirects.credentials import WpLoginCredentials
from app.monitor.redirects.wp_redirection import RestResponse, parse_export_envelope
from app.reports.stealth_browser import new_stealth_context, sync_playwright
from app.tools.http_safety import (
    NO_CACHE_HEADERS,
    USER_AGENT,
    HostThrottle,
    UnsafeUrlError,
    assert_public_http_url,
    safe_host,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page

__all__ = ["SOURCE", "cookie_header", "login_and_fetch_redirects", "parse_localized_script"]

SOURCE = "redirection"
LOGIN_TIMEOUT_MS = 20_000
NAV_TIMEOUT_MS = 30_000
REQUEST_TIMEOUT_SECONDS = 15.0
MAX_BODY_BYTES = 4 * 1024 * 1024

_THROTTLE = HostThrottle()
_NONCE_RE = re.compile(r'"WP_API_nonce"\s*:\s*"([a-f0-9]+)"')
_ROOT_RE = re.compile(r'"WP_API_root"\s*:\s*"([^"]+)"')


def _is_login_error(page: Page) -> bool:
    return page.locator("#login_error").count() > 0


def login(page: Page, base_url: str, *, wp_login: str, password: str) -> bool:
    """One login attempt against `wp-login.php`. `False` means the

    credentials were rejected (or the page never actually reached
    wp-admin) — this never raises for a plain wrong password, only for a
    genuine network/navigation failure, which propagates as-is.
    """
    page.goto(f"{base_url}/wp-login.php", wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS)
    page.fill("#user_login", wp_login)
    page.fill("#user_pass", password)
    page.click("#wp-submit")
    page.wait_for_load_state("domcontentloaded", timeout=LOGIN_TIMEOUT_MS)
    if _is_login_error(page):
        return False
    return "wp-admin" in page.url and "wp-login" not in page.url


def localized_script_text(page: Page) -> str:
    """The Redirection admin screen's own `wp_localize_script` output, read

    as DOM text (not a JS global — see module docstring on `patchright`'s
    isolated evaluate world).
    """
    text: str = page.evaluate(
        "() => document.getElementById('redirection-js-extra')?.textContent || ''"
    )
    return text


def parse_localized_script(text: str) -> tuple[str, str] | None:
    """`(api_root, nonce)` parsed out of `var Redirectioni10n = {...};` —

    regex, not `json.loads`: the `var X = ...;` wrapper isn't valid JSON on
    its own, and these two fields are the only thing this needs out of it.
    """
    nonce_match = _NONCE_RE.search(text)
    root_match = _ROOT_RE.search(text)
    if not nonce_match or not root_match:
        return None
    return root_match.group(1), nonce_match.group(1)


def cookie_header(cookies: Sequence[Any], host: str) -> str:
    """A `Cookie:` value from Playwright's own `context.cookies()` shape,

    scoped to `host` — a login to one client's site must never leak that
    client's session cookie into a request aimed at a different one.
    """
    return "; ".join(
        f"{cookie['name']}={cookie['value']}"
        for cookie in cookies
        if host in cookie.get("domain", "")
    )


def _fetch_export(api_root: str, nonce: str, cookies: str) -> RestResponse:
    url = f"{api_root}redirection/v1/export/all/json"
    assert_public_http_url(url)
    host = safe_host(url)
    request = urllib.request.Request(  # noqa: S310 - схема перевірена гардом вище
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "X-WP-Nonce": nonce,
            "Cookie": cookies,
            **NO_CACHE_HEADERS,
        },
    )
    with _THROTTLE.slot(host):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
                raw = response.read(MAX_BODY_BYTES + 1)[:MAX_BODY_BYTES]
                return RestResponse(response.status, response.headers.get_content_type() or "", raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read(MAX_BODY_BYTES + 1)[:MAX_BODY_BYTES]
            content_type = exc.headers.get_content_type() if exc.headers else ""
            return RestResponse(exc.code, content_type or "", raw)


def login_and_fetch_redirects(
    base_url: str,
    credentials: WpLoginCredentials,
    *,
    max_rules: int = 20_000,
    headless: bool = True,
) -> RedirectSourceResult:
    """One full cycle: launch a stealth browser, log in with a REGULAR

    wp-admin account (not an Application Password — none of the portfolio
    had one issued, task-51 follow-up), read the Redirection admin screen's
    own REST nonce, and fetch the same export `wp_redirection.py` parses,
    over cookie+nonce auth instead of Basic Auth.

    Not covered by a unit test with a real browser (mirrors
    `app.reports.ninja_browser.download_periods`'s own precedent: the
    launch/login/close lifecycle is exercised live, not faked) — every piece
    that does not itself need a live page (`parse_localized_script`,
    `cookie_header`, the reused `wp_redirection` parser) is.
    """
    if not base_url.lower().startswith("https://"):
        return RedirectSourceResult(
            ok=False,
            source=SOURCE,
            unavailable_reason="unsafe_url",
            detail=(
                "Only HTTPS sites can be checked (login cookies must not "
                "travel over plaintext HTTP)."
            ),
        )
    try:
        assert_public_http_url(base_url)
    except UnsafeUrlError as exc:
        return RedirectSourceResult(
            ok=False, source=SOURCE, unavailable_reason="unsafe_url", detail=str(exc)
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        try:
            context = new_stealth_context(browser)
            page = context.new_page()
            try:
                if not login(
                    page, base_url, wp_login=credentials.login, password=credentials.password
                ):
                    return RedirectSourceResult(
                        ok=False,
                        source=SOURCE,
                        unavailable_reason="auth_failed",
                        detail="The WordPress login was rejected.",
                    )
                page.goto(
                    f"{base_url}/wp-admin/tools.php?page=redirection.php&sub=io",
                    wait_until="networkidle",
                    timeout=NAV_TIMEOUT_MS,
                )
                parsed = parse_localized_script(localized_script_text(page))
                if parsed is None:
                    return RedirectSourceResult(
                        ok=False,
                        source=SOURCE,
                        unavailable_reason="plugin_missing",
                        detail="Logged in, but the Redirection admin screen did not load.",
                    )
                api_root, nonce = parsed
                cookies = cookie_header(context.cookies(), safe_host(base_url))
            finally:
                context.close()
        finally:
            browser.close()

    response = _fetch_export(api_root, nonce, cookies)
    if response.http_status in (401, 403):
        return RedirectSourceResult(ok=False, source=SOURCE, unavailable_reason="auth_failed")
    if response.http_status == 404:
        return RedirectSourceResult(ok=False, source=SOURCE, unavailable_reason="plugin_missing")
    if response.http_status != 200:
        return RedirectSourceResult(ok=False, source=SOURCE, unavailable_reason="unavailable")

    parsed_body = parse_export_envelope(response.body)
    if parsed_body is None:
        return RedirectSourceResult(ok=False, source=SOURCE, unavailable_reason="unavailable")
    rules, raw_snapshot = parsed_body
    return RedirectSourceResult(
        ok=True, source=SOURCE, rules=tuple(rules[:max_rules]), raw_snapshot=raw_snapshot
    )
