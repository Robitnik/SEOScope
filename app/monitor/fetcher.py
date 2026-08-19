"""stdlib HTTP fetcher over the shared SSRF/UA guard (task-35 §3.3/§1.7).

Deliberately does NOT send `NO_CACHE_HEADERS` or a cache-buster the way
`app.tools.alt_text.verify`/`app.tools.redirects.trace` do: the monitor wants
exactly what a normal crawler request sees through the client's CDN — the
same view Googlebot gets — not a forced-fresh origin hit.
"""

from __future__ import annotations

import gzip as gzip_module
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from app.tools.http_safety import (
    USER_AGENT,
    GuardedRedirectHandler,
    HostThrottle,
    UnsafeUrlError,
    assert_public_http_url,
    safe_host,
)

# Response bodies that smell like a WAF/bot challenge rather than the real
# page — a fair guess from a handful of live vendors, not an exhaustive list.
# Reported as the honest `blocked` state (task-35 risk table) instead of a
# false `site_down`.
#
# LIVE 2026-08-19 (task-50): alphastyle.store's actual Cloudflare/Shopify
# interstitial ("Verifying your connection...") matched NONE of the original
# seven — it uses `_cf_chl` (underscore, not the `cf-chl` hyphen form here)
# and `cf-injected-html`, and the page carries `429`, a status this list
# never even considered a challenge candidate. 304 of 355 pages on that
# client got recorded as real content because of this gap.
_CHALLENGE_MARKERS = (
    b"cf-chl",
    b"cf-please-wait",
    b"checking your browser",
    b"just a moment",
    b"attention required! | cloudflare",
    b"ddos protection by",
    b"captcha-delivery",
    b"verifying your connection",
    b"cf-injected-html",
    b"_cf_chl",
    b"enable javascript and cookies",
)


class Headers(dict[str, str]):
    """Response headers with case-insensitive lookup.

    HTTP field names are case-insensitive (RFC 9110 §5.1) and HTTP/2 requires
    them **lowercase**, so most modern CDNs answer with `content-encoding` /
    `x-robots-tag`. `urllib`'s own header object handles that; a plain
    `dict(response.headers.items())` silently throws it away — and every
    `.get("Content-Encoding")` / `.get("X-Robots-Tag")` in this package then
    reads `None` on exactly the hosts that matter.

    LIVE 2026-08-12: that is not hypothetical. `alphastyle.store` (Shopify
    behind Cloudflare) sends lowercase `content-encoding: gzip`; the gzip body
    was stored and parsed as binary, so **all 355 of its pages had no title,
    no meta robots and no canonical** — a `noindex` there would have been
    invisible to the monitor whose whole job is to catch it.

    Original casing is preserved for display; only lookup is case-blind.
    """

    def __init__(self, items: Any = ()) -> None:
        super().__init__(items)
        self._lower = {key.lower(): value for key, value in self.items()}

    def get(self, key: str, default: Any = None) -> Any:
        return self._lower.get(key.lower(), default)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key.lower() in self._lower


@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    final_url: str
    status: int | None
    hop_count: int
    headers: dict[str, str]
    body: bytes | None
    truncated_body: bool
    elapsed_ms: int
    error: str | None  # None | "timeout" | "connection_error" | "blocked" | "unsafe_url"
    error_detail: str | None = None
    # Populated only when `error == "blocked"` and the response carried a
    # usable `Retry-After` (task-50 §B3) — seconds until it is worth trying
    # this host again. `None` when absent or unparseable (HTTP-date form is
    # not handled: every live bot-mitigation vendor seen so far uses the
    # delta-seconds form).
    retry_after_seconds: float | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None


@dataclass
class FetchStats:
    """Accumulated across a run — folded into `MonitorRun.requests_made`/
    `bytes_downloaded` by the caller."""

    requests_made: int = 0
    bytes_downloaded: int = 0
    hosts_seen: set[str] = field(default_factory=set)


def _parse_retry_after(headers: Headers) -> float | None:
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None  # HTTP-date form — not handled, see `FetchResult.retry_after_seconds`


def classify_block(
    status: int | None, headers: Headers, body: bytes
) -> tuple[str | None, float | None]:
    """`(error, retry_after_seconds)` — `error` is `"blocked"` or `None`.

    task-50 §0.3/§0.5 (live 2026-08-19): a bare marker scan over the first 4KB
    missed alphastyle.store's real challenge page entirely. Three independent
    signals now, any one of which is decisive on its own:

    - `429` is ALWAYS a rate-limit, never page content — treating it as a
      `broken` page status is a category error regardless of the body.
    - `cf-mitigated` is Cloudflare's own admission that it intervened — the
      most reliable signal there is, when present.
    - `Retry-After` on a 403/503 is a vendor telling us to slow down, which a
      real page never does.

    Only when none of those fire do we fall back to a body-marker scan — and
    now over the WHOLE stored body (already capped by `max_body_bytes`), not
    just the first 4KB: the live ALPHA challenge page's markers sat at byte
    ~1500, comfortably inside 4KB by luck, not by any guarantee this list can
    rely on for the next vendor.
    """
    if status is None:
        return None, None
    retry_after = _parse_retry_after(headers)
    if status == 429:
        return "blocked", retry_after
    if headers.get("cf-mitigated"):
        return "blocked", retry_after
    if status in (403, 503):
        if retry_after is not None:
            return "blocked", retry_after
        sample = body.lower()
        if any(marker in sample for marker in _CHALLENGE_MARKERS):
            return "blocked", retry_after
    return None, None


def fetch(
    url: str,
    *,
    throttle: HostThrottle,
    stats: FetchStats,
    timeout: float = 20.0,
    max_body_bytes: int = 3 * 1024 * 1024,
    method: str = "GET",
    extra_headers: dict[str, str] | None = None,
) -> FetchResult:
    """One SSRF-guarded, throttled, size-capped GET/HEAD request.

    Every redirect hop is itself SSRF-checked by `GuardedRedirectHandler`
    (mirrors `app.tools.redirects.trace`), so `final_url` is always safe to
    fetch again on the next run.
    """
    started = time.monotonic()
    try:
        assert_public_http_url(url)
    except UnsafeUrlError as exc:
        return FetchResult(
            requested_url=url,
            final_url=url,
            status=None,
            hop_count=0,
            headers={},
            body=None,
            truncated_body=False,
            elapsed_ms=0,
            error="unsafe_url",
            error_detail=str(exc),
        )

    # task-50 §0.5 (live 2026-08-19, alphastyle.store): isolated header-by-
    # header against a real Cloudflare challenge — `Accept-Language` alone is
    # what flips a 403 to a 200, even under an openly bot-shaped User-Agent.
    # `Accept`/`Upgrade-Insecure-Requests` cost nothing and make the request
    # look like an ordinary navigation instead of a bare script fetch. This
    # is necessary hygiene, not sufficient on its own (§0.5 note: the same
    # bare header set from a different TLS/HTTP2 stack still got a 200 where
    # this container's got a 403) — the antibot browser lane exists for that.
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
            "image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip",
        "Upgrade-Insecure-Requests": "1",
    }
    headers.update(extra_headers or {})
    request = urllib.request.Request(url, headers=headers, method=method)
    opener = urllib.request.build_opener(GuardedRedirectHandler)

    host = safe_host(url)
    stats.hosts_seen.add(host)
    with throttle.slot(host):
        stats.requests_made += 1
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(max_body_bytes + 1)
                truncated = len(raw) > max_body_bytes
                raw = raw[:max_body_bytes]
                response_headers = Headers(response.headers.items())
                if response_headers.get("Content-Encoding", "").lower() == "gzip":
                    try:
                        raw = gzip_module.decompress(raw)
                    except OSError:
                        pass  # truncated gzip stream — keep the raw bytes as-is
                status = response.status
                final_url = response.geturl()
                # Approximate — "did at least one redirect happen", not an exact
                # hop count. Entry-point checks that need the real hop-by-hop
                # chain (task-35 §3.3 item 7) go through
                # `app.tools.redirects.trace` directly instead of this fetcher.
                hop_count = 0 if final_url == url else 1
        except urllib.error.HTTPError as exc:
            raw = exc.read(max_body_bytes) if exc.fp else b""
            # Error responses carry `X-Robots-Tag` and `Content-Type` too — the
            # same case-insensitive treatment, or a 4xx page's headers read as
            # empty on the same hosts.
            response_headers = Headers(exc.headers.items()) if exc.headers else Headers()
            # ...and an error body is compressed just like a successful one.
            # Without this, a gzipped WAF challenge page never matches
            # `_CHALLENGE_MARKERS` and gets misreported as a plain 4xx.
            if response_headers.get("Content-Encoding", "").lower() == "gzip":
                try:
                    raw = gzip_module.decompress(raw)
                except OSError:
                    pass
            status = exc.code
            final_url = exc.geturl() if hasattr(exc, "geturl") else url
            hop_count = 0
            truncated = False
        except UnsafeUrlError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            return FetchResult(
                requested_url=url,
                final_url=url,
                status=None,
                hop_count=0,
                headers={},
                body=None,
                truncated_body=False,
                elapsed_ms=elapsed,
                error="unsafe_url",
                error_detail=str(exc),
            )
        except TimeoutError:
            elapsed = int((time.monotonic() - started) * 1000)
            return FetchResult(
                requested_url=url,
                final_url=url,
                status=None,
                hop_count=0,
                headers={},
                body=None,
                truncated_body=False,
                elapsed_ms=elapsed,
                error="timeout",
                error_detail=f"timed out after {timeout:.0f}s",
            )
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            return FetchResult(
                requested_url=url,
                final_url=url,
                status=None,
                hop_count=0,
                headers={},
                body=None,
                truncated_body=False,
                elapsed_ms=elapsed,
                error="connection_error",
                error_detail=str(getattr(exc, "reason", exc)),
            )

    stats.bytes_downloaded += len(raw)
    elapsed = int((time.monotonic() - started) * 1000)
    error, retry_after_seconds = classify_block(status, response_headers, raw)
    return FetchResult(
        requested_url=url,
        final_url=final_url,
        status=status,
        hop_count=hop_count,
        headers=response_headers,
        body=raw,
        truncated_body=truncated,
        elapsed_ms=elapsed,
        error=error,
        retry_after_seconds=retry_after_seconds,
    )


def crawl_delay_floor(crawl_delay: float | None, minimum: float) -> float:
    """`max(crawl_delay, minimum)` — task-35 §3.3: robots.txt is not obeyed
    for the SEO directives, but `Crawl-delay` is always honored.
    """
    if crawl_delay is None:
        return minimum
    return max(crawl_delay, minimum)


def host_of(url: str) -> str:
    return urlsplit(url).hostname or url
