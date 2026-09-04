from __future__ import annotations

from app.webarchive.urls import normalize_url, resolve


def test_normalize_strips_www_and_scheme() -> None:
    assert normalize_url("https://www.example.com/a/") == normalize_url("http://example.com/a/")


def test_normalize_sorts_query_and_drops_tracking_params() -> None:
    a = normalize_url("https://example.com/a?b=2&a=1&utm_source=newsletter")
    b = normalize_url("https://example.com/a?a=1&b=2")
    assert a == b


def test_normalize_keeps_real_query_params() -> None:
    # LAXTON's own `?taxonomy=...&term=123` pages are real, distinct pages
    # (task-59 plan §2) — never collapsed away like tracking params are.
    key = normalize_url("https://example.com/shop?taxonomy=product_shipping_class&term=123")
    assert "taxonomy=product_shipping_class" in key
    assert "term=123" in key


def test_normalize_default_port_dropped() -> None:
    assert normalize_url("https://example.com:443/a") == normalize_url("https://example.com/a")


def test_normalize_nondefault_port_kept() -> None:
    assert normalize_url("https://example.com:8443/a") != normalize_url("https://example.com/a")


def test_normalize_fragment_dropped() -> None:
    assert normalize_url("https://example.com/a#section") == normalize_url("https://example.com/a")


def test_resolve_rejects_non_http_schemes() -> None:
    base = "https://example.com/page"
    assert resolve(base, "javascript:void(0)") is None
    assert resolve(base, "mailto:test@example.com") is None
    assert resolve(base, "tel:+15551234567") is None
    assert resolve(base, "#anchor") is None
    assert resolve(base, "data:image/png;base64,abc") is None
    assert resolve(base, "blob:https://example.com/uuid") is None
    assert resolve(base, "") is None


def test_resolve_relative_against_base() -> None:
    assert resolve("https://example.com/dir/page", "../other") == "https://example.com/other"


def test_resolve_protocol_relative() -> None:
    assert resolve("https://example.com/page", "//cdn.example.com/x.js") == "https://cdn.example.com/x.js"


def test_resolve_percent_encodes_non_ascii_characters() -> None:
    """Live incident (task-59, 2026-09-04): a real client site's own
    filename contained a bare en-dash (U+2013); `urllib`'s HTTP client
    refuses to put a non-ASCII request line on the wire and crashed every
    capture that touched it before this existed.
    """
    result = resolve("https://example.com/", "/menu–special.jpg")
    assert result is not None
    assert result.isascii()
    assert result == "https://example.com/menu%E2%80%93special.jpg"


def test_resolve_does_not_double_encode_existing_percent_sequences() -> None:
    result = resolve("https://example.com/", "/already%20encoded.jpg")
    assert result == "https://example.com/already%20encoded.jpg"


def test_resolve_ascii_url_is_untouched() -> None:
    result = resolve("https://example.com/", "/plain/path.jpg?a=1&b=2")
    assert result == "https://example.com/plain/path.jpg?a=1&b=2"
