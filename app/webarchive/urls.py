"""URL normalization for the Web Archive (task-59 §2).

Every archive table that looks up "the same URL" (per-client pages, the
globally-shared asset registry, replay resolution) keys on `url_key`, not on
the raw URL string — `https://www.example.com/a?b=1&a=2` and
`https://example.com/a/?a=2&b=1` are two different strings but the same
resource in practice, and `SDS`'s own monitor rows carry BOTH the `www.` and
bare-domain form for the exact same page (live-checked task-59 §0.6).
"""

from __future__ import annotations

from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}

# Parameters that vary per-visit/per-campaign and never change what the page
# actually shows — dropping them means a shared link with a different
# `utm_source` resolves to the SAME archived resource instead of quietly
# forking the archive in two.
_TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "twclid",
        "_ga", "_gl", "mc_cid", "mc_eid", "igshid", "yvcheck",
    }
)


def normalize_url(url: str, *, keep_query: bool = True) -> str:
    """Collapse a URL to its identity key: scheme dropped, host lower-cased
    and `www.`-stripped, default port dropped, fragment dropped, query
    parameters sorted with tracking params removed.

    `keep_query=False` is for callers that intentionally want to dedupe
    query-string variants of the same path (not the archive's own default —
    `LAXTON`'s `?taxonomy=...&term=123` pages are real, distinct pages, task-59
    §2 — but useful for, e.g., grouping asset cache-buster variants later).
    """
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    if port and port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{port}"
    path = parts.path or "/"

    query = ""
    if keep_query and parts.query:
        pairs = sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_PARAMS
        )
        query = urlencode(pairs)

    key = f"{host}{path}"
    if query:
        key = f"{key}?{query}"
    return key


def resolve(base_url: str, maybe_relative: str) -> str | None:
    """Absolute http(s) URL for a possibly-relative reference, or `None` for
    anything the archive never follows (`data:`, `blob:`, `javascript:`,
    `mailto:`, `tel:`, a bare fragment, or a non-http(s) scheme).
    """
    candidate = maybe_relative.strip()
    if not candidate or candidate.startswith(
        ("data:", "blob:", "javascript:", "mailto:", "tel:", "#", "about:")
    ):
        return None
    absolute = urljoin(base_url, candidate)
    if urlsplit(absolute).scheme not in ("http", "https"):
        return None
    return _ascii_safe(absolute)


def _ascii_safe(url: str) -> str:
    """Percent-encode any character an href can carry literally that
    `urllib`'s HTTP client refuses to put on the request line — live-measured
    (task-59, 2026-09-04): a real client site's own filename contained a
    bare en-dash (`–`, U+2013), and unrelated markup elsewhere on the page
    contained a stray U+2028 line separator; both crashed every capture that
    touched them with `UnicodeEncodeError: 'ascii' codec can't encode...`
    before this existed. `%` stays in the safe set so an already
    percent-encoded sequence (e.g. `%20`) is never double-encoded.
    """
    if url.isascii():
        return url
    return quote(url, safe="%/:?&=@!$'()*+,;#~")
