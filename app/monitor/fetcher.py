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
_CHALLENGE_MARKERS = (
    b"cf-chl",
    b"cf-please-wait",
    b"checking your browser",
    b"just a moment",
    b"attention required! | cloudflare",
    b"ddos protection by",
    b"captcha-delivery",
)


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


def _looks_blocked(status: int, body: bytes) -> bool:
    if status not in (403, 429, 503):
        return False
    sample = body[:4096].lower()
    return any(marker in sample for marker in _CHALLENGE_MARKERS)


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

    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
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
                response_headers = {k: v for k, v in response.headers.items()}
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
            response_headers = {k: v for k, v in exc.headers.items()} if exc.headers else {}
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
    error = "blocked" if _looks_blocked(status, raw) else None
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
