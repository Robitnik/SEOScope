from __future__ import annotations

from app.webarchive.urls import (
    is_analytics_iframe_src,
    is_asset_like_url,
    looks_like_binary_content_type,
    normalize_url,
    resolve,
)


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


def test_resolve_percent_encodes_a_literal_ascii_space() -> None:
    """Live incident (task-59, 2026-09-04, SDS): a real filename contained a
    literal space (`1 - PHOTO.mp4`) — fully ASCII, so the old
    `if url.isascii(): return url` fast path let it through raw, and
    `http.client`'s own request-line validator rejects a bare space exactly
    like it rejects non-ASCII bytes.
    """
    result = resolve("https://example.com/", "/uploads/1 - PHOTO.mp4")
    assert result is not None
    assert " " not in result
    assert result == "https://example.com/uploads/1%20-%20PHOTO.mp4"


def test_resolve_ascii_url_is_untouched() -> None:
    result = resolve("https://example.com/", "/plain/path.jpg?a=1&b=2")
    assert result == "https://example.com/plain/path.jpg?a=1&b=2"


def test_is_asset_like_url_flags_gallery_lightbox_targets() -> None:
    """Live incident (task-59, 2026-09-04): EFFYS's own gallery wraps each
    thumbnail in `<a href="...-scaled.webp">` for a lightbox popup — a
    perfectly real same-host link that is nonetheless not a page.
    """
    assert is_asset_like_url("https://effyscafe.com/wp-content/uploads/2026/06/photo-scaled.webp")
    assert is_asset_like_url("https://example.com/brochure.pdf")
    assert is_asset_like_url("https://example.com/fonts/main.woff2")


def test_is_asset_like_url_allows_real_pages() -> None:
    assert not is_asset_like_url("https://example.com/gallery/")
    assert not is_asset_like_url("https://example.com/about-us")
    assert not is_asset_like_url("https://example.com/2026/09/04/post-title")


def test_looks_like_binary_content_type_flags_images_and_documents() -> None:
    assert looks_like_binary_content_type("image/webp")
    assert looks_like_binary_content_type("image/jpeg; charset=binary")
    assert looks_like_binary_content_type("application/pdf")
    assert looks_like_binary_content_type("font/woff2")


def test_looks_like_binary_content_type_allows_html_and_text() -> None:
    assert not looks_like_binary_content_type("text/html; charset=UTF-8")
    assert not looks_like_binary_content_type("text/plain")
    assert not looks_like_binary_content_type("application/xhtml+xml")


def test_is_analytics_iframe_src_flags_gtm_and_www_variants() -> None:
    """349 of the portfolio's iframe embeds are GTM's own noscript pixel
    (task-60 plan §0.6) — `www.googletagmanager.com` is the form actually
    seen in real markup, so the `www.`-stripping matters, not just the
    bare host.
    """
    assert is_analytics_iframe_src("https://www.googletagmanager.com/ns.html?id=GTM-XXXX")
    assert is_analytics_iframe_src("https://googletagmanager.com/ns.html?id=GTM-XXXX")
    assert is_analytics_iframe_src("https://google-analytics.com/x")
    assert is_analytics_iframe_src("https://doubleclick.net/x")
    assert is_analytics_iframe_src("https://connect.facebook.net/x")


def test_is_analytics_iframe_src_flags_facebook_pixel_path_only() -> None:
    assert is_analytics_iframe_src("https://www.facebook.com/tr?id=123&ev=PageView")
    assert not is_analytics_iframe_src("https://www.facebook.com/video/embed?video_id=1")


def test_is_analytics_iframe_src_allows_real_embeds() -> None:
    """The ~85 real embeds (task-60 plan §0.6) — maps, Spotify, YouTube,
    Vimeo — must never be misclassified as analytics beacons.
    """
    assert not is_analytics_iframe_src("https://www.google.com/maps/embed?pb=abc")
    assert not is_analytics_iframe_src("https://open.spotify.com/embed/track/abc")
    assert not is_analytics_iframe_src("https://www.youtube.com/embed/abc")
    assert not is_analytics_iframe_src("https://player.vimeo.com/video/abc")


def test_is_analytics_iframe_src_honors_custom_host_set() -> None:
    custom = frozenset({"tracker.example.com"})
    assert is_analytics_iframe_src("https://tracker.example.com/beacon", hosts=custom)
    # A custom set REPLACES the built-in default for this call, matching
    # `Settings.archive_iframe_analytics_hosts` being the canonical list.
    assert not is_analytics_iframe_src("https://googletagmanager.com/ns.html", hosts=custom)
