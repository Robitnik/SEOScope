"""One matching key for "the same page" across every per-URL table (task-61
§0.2/§2.1, measured on the real portfolio).

`app.webarchive.urls.normalize_url` is the right base (scheme dropped,
`www.` stripped, tracking params removed) and is imported here, never
copied — task-60 owns that module and works in it in parallel; forking the
notion of "same URL" would give the two tasks two different answers within
a week.

What it does NOT do, and what actually breaks GSC<->GA4 matching on this
portfolio: normalize a trailing slash. GSC stores
`https://www.nysapphire.com/a/`, GA4's `ga4_landing_pages` stores `/a` for
the identical page. Measured on live data (task-61 plan §0.2): the naive
`normalize_url` key finds 4 of 859 GSC page-URLs also present in 3XE's GA4
landing pages; the slash-insensitive key here finds 690 (×172). Collisions
introduced by that slash-folding were checked and are negligible (<=35 on
the worst client, 3XE) and are all http/https or www/bare variants of the
SAME page, never two different pages.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from app.webarchive.urls import normalize_url


def inspector_key(url: str) -> str:
    """`normalize_url` + trailing-slash folding on the path component.

    The root path `/` is never folded to `""` — an empty path would collide
    with "no path at all" and is not how any of the source tables spell the
    homepage.
    """
    key = normalize_url(url)
    host, _, rest = key.partition("/")
    path_and_query = f"/{rest}"
    path, _, query = path_and_query.partition("?")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    result = f"{host}{path}"
    if query:
        result = f"{result}?{query}"
    return result


def _host_variants(host: str) -> list[str]:
    bare = host[4:] if host.startswith("www.") else host
    return [bare, f"www.{bare}"]


def _path_variants(path: str) -> list[str]:
    if not path or path == "/":
        return ["/"]
    stripped = path.rstrip("/") or "/"
    return [stripped, f"{stripped}/"] if stripped != "/" else ["/"]


def lookup_variants(url: str) -> list[str]:
    """Every plausible stored spelling of `url`, for an `IN (...)` lookup
    against columns that store a full URL (`gsc_dimension_metrics.dimension_value`,
    `monitor_pages.url`, `archive_urls.url`, `monitor_changes.subject`, ...):
    `{http, https} x {host, www.host} x {path, path/}` — 8 forms (task-61
    §0.5). Measured cheap: 0.8-66ms per query on the real portfolio, so no
    migration/new indexed column is needed to support this.

    Query string is preserved verbatim (not multiplied) — a query-string
    variant is a different page (task-59's own `normalize_url` docstring:
    LAXTON's `?taxonomy=...` pages are real, distinct pages), so guessing
    query variants here would silently widen the match beyond "this URL".
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    query = parts.query
    variants: list[str] = []
    seen: set[str] = set()
    for scheme in ("https", "http"):
        for h in _host_variants(host):
            for p in _path_variants(path):
                candidate = urlunsplit((scheme, h, p, query, ""))
                if candidate not in seen:
                    seen.add(candidate)
                    variants.append(candidate)
    return variants


def ga4_landing_variants(url: str) -> list[str]:
    """GA4's `ga4_landing_pages` dataset stores a bare PATH, not a URL
    (`landingPage` dimension) — `/a`, `/a/`, or `/` for the homepage, never
    a host. Root and non-root paths need different folding: the homepage is
    stored as `/`, never `""`."""
    parts = urlsplit(url.strip())
    path = parts.path or "/"
    if path == "/":
        return ["/"]
    stripped = path.rstrip("/") or "/"
    return [stripped, f"{stripped}/"]
