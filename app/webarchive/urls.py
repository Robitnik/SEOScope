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


def trailing_slash_variant(url_key: str) -> str | None:
    """The other spelling of `url_key`'s path — with a trailing slash added
    or removed — or `None` when there is nothing to fold (the path is
    exactly `/`, or `url_key` carries no path separator at all).

    `normalize_url` deliberately does NOT fold this itself (see the module
    docstring above, and `app.urlinspector.urlkey`'s own note on the same
    gap for cross-source matching) — its output is what three tables'
    `url_key` UNIQUE constraints are already built on for a live portfolio;
    changing what it emits would silently rewrite the identity of every
    already-stored row. This is instead a second, narrower question a
    caller can ask before creating a NEW row under a key that never
    existed: "does the other spelling already exist?" — see
    `WebArchiveRepository.get_or_create_url`, the only caller today.

    Measured live 2026-09-06: 312 of ALPHA's 1565 tracked URLs (20%) were
    exactly `/products/x` and `/products/x/` as two separate frontier
    rows — the same page crawled, fingerprinted and diffed twice on every
    pass. Existing duplicate rows are NOT touched here (that is a data
    cleanup on a live table, not a bugfix); this only stops the count from
    growing on future discovery.
    """
    host, sep, rest = url_key.partition("/")
    if not sep:
        return None  # `normalize_url` always emits a path; defensive only
    path, has_query, query = rest.partition("?")
    path = f"/{path}"
    if path == "/":
        return None  # the homepage has no slash-or-not variant
    alt_path = path[:-1] if path.endswith("/") else f"{path}/"
    alt = f"{host}{alt_path}"
    return f"{alt}?{query}" if has_query else alt


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


# A same-host `<a href>` can legally point straight at a binary resource —
# a WordPress gallery lightbox linking `<a href=".../photo.webp">` around
# its own thumbnail is the case that surfaced this (task-59, 2026-09-04
# live incident on EFFYS): `_discover_same_host_links` was queuing every
# full-size photo as its own "page" to crawl, and the resulting garbled
# "page" snapshot of raw image bytes then SHADOWED the correctly-captured
# `archive_assets` entry for the same URL at replay time (pages are
# resolved before assets there), so the image rendered as broken.
_NON_PAGE_EXTENSIONS = frozenset(
    {
        # images
        "jpg", "jpeg", "png", "gif", "webp", "avif", "svg", "ico", "bmp", "tiff", "tif", "heic",
        # fonts
        "woff", "woff2", "ttf", "otf", "eot",
        # media
        "mp4", "mp3", "wav", "ogg", "webm", "mov", "avi", "m4a", "m4v", "flac",
        # documents / archives / data
        "pdf", "zip", "rar", "7z", "gz", "tar", "doc", "docx", "xls", "xlsx",
        "ppt", "pptx", "csv", "rtf",
        # code assets
        "css", "js", "mjs", "map", "wasm", "json", "xml",
    }
)
_NON_PAGE_CONTENT_TYPE_PREFIXES = ("image/", "font/", "video/", "audio/")
_NON_PAGE_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "application/zip",
        "application/octet-stream",
        "application/font-woff",
        "application/font-woff2",
        "application/vnd.ms-fontobject",
        "application/x-font-ttf",
    }
)


def is_asset_like_url(url: str) -> bool:
    """True when `url`'s path extension is something a visitor is never
    meant to land on as its own page — see the module-level note above.
    Extension-only (fast, no network): the content-type-based
    `looks_like_binary_content_type` below is the authoritative fallback
    for whatever slips past this (an extensionless URL, a mislabeled path).
    """
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    if "." not in name:
        return False
    return name.rsplit(".", 1)[-1].lower() in _NON_PAGE_EXTENSIONS


def looks_like_binary_content_type(content_type: str) -> bool:
    """True for a response `Content-Type` that is clearly not an HTML page
    — the authoritative check (actual bytes, not a URL guess) used to
    refuse storing/replaying a binary response as a "page".
    """
    main_type = content_type.split(";", 1)[0].strip().lower()
    return (
        main_type.startswith(_NON_PAGE_CONTENT_TYPE_PREFIXES)
        or main_type in _NON_PAGE_CONTENT_TYPES
    )


# Analytics/tracking beacon hosts an iframe must NEVER be left pointing at
# the live internet even though task-60 §2 otherwise leaves embeds live —
# see `app.webarchive.rewrite`'s module note. Overridable via
# `Settings.archive_iframe_analytics_hosts`; this frozenset is only the
# fallback for callers (tests, the CLI backfill path) that don't thread
# settings through.
_DEFAULT_ANALYTICS_IFRAME_HOSTS = frozenset(
    {"googletagmanager.com", "google-analytics.com", "doubleclick.net", "connect.facebook.net"}
)


def is_analytics_iframe_src(absolute_url: str, *, hosts: frozenset[str] | None = None) -> bool:
    """True for an iframe `src` that is a known analytics/tracking beacon,
    not real embedded content. `facebook.com/tr` is Meta's own pixel
    endpoint specifically — other `facebook.com` paths are real embeds (a
    video/post iframe) and stay live like any other third-party embed.
    """
    parts = urlsplit(absolute_url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host in (hosts if hosts is not None else _DEFAULT_ANALYTICS_IFRAME_HOSTS):
        return True
    return host == "facebook.com" and parts.path.startswith("/tr")


def _ascii_safe(url: str) -> str:
    """Percent-encode any character an href can carry literally that
    `urllib`'s HTTP client refuses to put on the request line — live-measured
    (task-59, 2026-09-04): a real client site's own filename contained a
    bare en-dash (`–`, U+2013), and unrelated markup elsewhere on the page
    contained a stray U+2028 line separator; both crashed every capture that
    touched them with `UnicodeEncodeError: 'ascii' codec can't encode...`
    before this existed. `%` stays in the safe set so an already
    percent-encoded sequence (e.g. `%20`) is never double-encoded.

    Always quotes, never short-circuits on `url.isascii()` — a SECOND live
    incident (task-59, 2026-09-04, SDS) hit a real filename containing a
    literal space (`.../1 - PHOTO.mp4`), fully ASCII, which the old
    ascii-skip let through raw; `http.client` rejects a bare space in the
    request line exactly like it rejects non-ASCII bytes. The safe set
    already covers every legitimate URL-structure character, so re-quoting
    an already-clean ASCII URL is a no-op.
    """
    return quote(url, safe="%/:?&=@!$'()*+,;#~")
