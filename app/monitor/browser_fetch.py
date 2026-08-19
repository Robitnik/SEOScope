"""Browser-based fetcher for the Site Monitor's antibot lane (task-50).

Drop-in for `app.monitor.fetcher.fetch`: same `FetchResult` contract, so
`app.monitor.runner._process_page` can switch transports by swapping which
function it calls — nothing downstream (parsing/fingerprinting/diffing/
storage) needs to know or care which one produced the bytes.

Reserved for clients whose site fails through the cheap `urllib` transport —
task-50 §0.5 (live 2026-08-19, alphastyle.store): an ordinary browser header
set (`Accept-Language` especially) clears SOME of Cloudflare's bot-score, but
not all of it — the same header set from this container's own TLS/HTTP2
stack still drew a 403 where a real browser's stack drew a 200. Only an
actual browser engine consistently passes.

`response.body()` is what gets hashed and stored — NEVER `page.content()`:
measured live, the DOM after JS execution differs from the wire bytes on
every single load (task-50 §0.8: two back-to-back loads of the same URL
disagreed by 16 bytes of live-injected script state), which would fingerprint
as a content change on every run even when nothing on the page actually
changed. `response.body()` matched the plain-HTTP fetch's `signals_sha256`
exactly in that same measurement — the switch is transparent to diffing.
"""

from __future__ import annotations

import time
from typing import Any

from app.monitor.fetcher import FetchResult, FetchStats, Headers, classify_block
from app.reports.stealth_browser import is_timeout_error, new_stealth_context, sync_playwright
from app.tools.http_safety import HostThrottle, UnsafeUrlError, assert_public_http_url, safe_host

# Cut per-page transfer and, just as important, cut the NUMBER of requests
# against the very host we are trying not to rate-limit further (task-50
# §B1) — a product page pulls dozens of image/font/stylesheet requests that
# tell us nothing about SEO signals.
_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font", "stylesheet"})


class BrowserFetchSession:
    """One Chromium + one context for an entire client run.

    Launching costs ~0.5s measured (task-50 §0.7) — paying that once per run
    instead of once per page is the difference between a run finishing in
    minutes and one that spends most of its wall-clock time on browser
    startup. A single shared context also accumulates the site's own
    anti-bot cookies (`__cf_bm`, …) across pages instead of presenting as a
    brand-new, cookie-less visitor on every request — which is itself a
    bot-shaped pattern.
    """

    def __init__(self) -> None:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        self._context = new_stealth_context(self._browser)
        self._unsafe_detail: str | None = None
        self._context.route("**/*", self._route)
        self._page = self._context.new_page()

    def _route(self, route: Any) -> None:
        request = route.request
        if request.resource_type in _BLOCKED_RESOURCE_TYPES:
            route.abort()
            return
        # Every navigation — including each redirect hop, which Playwright
        # routes as its own "document" request — is SSRF-checked before it
        # is allowed out (task-50 §B1, mirrors `GuardedRedirectHandler`).
        # Sitemap/redirect targets are site-controlled content, exactly the
        # input the SSRF guard exists to distrust (PLAN §13).
        if request.resource_type == "document":
            try:
                assert_public_http_url(request.url)
            except UnsafeUrlError as exc:
                self._unsafe_detail = str(exc)
                route.abort()
                return
        route.continue_()

    def goto(self, url: str, *, timeout_ms: float) -> Any:
        self._unsafe_detail = None
        try:
            return self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            if self._unsafe_detail is not None:
                raise UnsafeUrlError(self._unsafe_detail) from exc
            raise

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            try:
                self._browser.close()
            finally:
                self._playwright.stop()


def _empty_result(
    url: str, *, elapsed_ms: int, error: str, error_detail: str | None
) -> FetchResult:
    return FetchResult(
        requested_url=url,
        final_url=url,
        status=None,
        hop_count=0,
        headers={},
        body=None,
        truncated_body=False,
        elapsed_ms=elapsed_ms,
        error=error,
        error_detail=error_detail,
    )


def fetch_via_browser(
    url: str,
    *,
    session: BrowserFetchSession,
    throttle: HostThrottle,
    stats: FetchStats,
    timeout: float = 20.0,
    max_body_bytes: int = 3 * 1024 * 1024,
) -> FetchResult:
    started = time.monotonic()
    try:
        assert_public_http_url(url)
    except UnsafeUrlError as exc:
        return _empty_result(url, elapsed_ms=0, error="unsafe_url", error_detail=str(exc))

    host = safe_host(url)
    stats.hosts_seen.add(host)
    with throttle.slot(host):
        stats.requests_made += 1
        try:
            response = session.goto(url, timeout_ms=timeout * 1000)
        except UnsafeUrlError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            return _empty_result(url, elapsed_ms=elapsed, error="unsafe_url", error_detail=str(exc))
        except Exception as exc:  # patchright/playwright TimeoutError or a navigation failure
            elapsed = int((time.monotonic() - started) * 1000)
            fetch_error = "timeout" if is_timeout_error(exc) else "connection_error"
            return _empty_result(
                url, elapsed_ms=elapsed, error=fetch_error, error_detail=str(exc)[:300]
            )

        if response is None:
            elapsed = int((time.monotonic() - started) * 1000)
            return _empty_result(
                url, elapsed_ms=elapsed, error="connection_error", error_detail="no response"
            )

        raw = response.body()
        truncated = len(raw) > max_body_bytes
        raw = raw[:max_body_bytes]
        headers = Headers(response.headers)
        status = response.status
        final_url = response.url
        hop_count = 0 if final_url == url else 1

    stats.bytes_downloaded += len(raw)
    elapsed = int((time.monotonic() - started) * 1000)
    error, retry_after_seconds = classify_block(status, headers, raw)
    return FetchResult(
        requested_url=url,
        final_url=final_url,
        status=status,
        hop_count=hop_count,
        headers=headers,
        body=raw,
        truncated_body=truncated,
        elapsed_ms=elapsed,
        error=error,
        retry_after_seconds=retry_after_seconds,
    )
