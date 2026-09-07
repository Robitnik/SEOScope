from __future__ import annotations

from app.webarchive.rewrite import live_iframe_hosts, replay_url, rewrite_css, rewrite_html

TS = "20260904133000"
BASE = "https://example.com/page/"


def test_rewrites_href_src_srcset_and_stylesheet_link() -> None:
    html = (
        '<a href="/a">A</a>'
        '<img src="/img.jpg" srcset="/img-2x.jpg 2x, /img-3x.jpg 3x">'
        '<link rel="stylesheet" href="/style.css">'
    )
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert f"/archive/r/{TS}/https://example.com/a" in out
    assert f"/archive/r/{TS}/https://example.com/img.jpg" in out
    assert f"/archive/r/{TS}/https://example.com/img-2x.jpg" in out
    assert f"/archive/r/{TS}/https://example.com/img-3x.jpg" in out
    assert f"/archive/r/{TS}/https://example.com/style.css" in out


def test_script_body_is_left_untouched() -> None:
    html = (
        '<script>var link = "https://cdn.example.com/lib.js"; '
        'document.location.href = "https://other.com/x";</script>'
    )
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert "cdn.example.com" in out
    assert "other.com/x" in out
    assert "/archive/r/" not in out


def test_script_src_attribute_is_rewritten() -> None:
    html = '<script src="/app.js"></script>'
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert f"/archive/r/{TS}/https://example.com/app.js" in out


def test_style_tag_css_url_rewritten() -> None:
    html = "<style>body { background: url(/bg.png); }</style>"
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert f"/archive/r/{TS}/https://example.com/bg.png" in out


def test_inline_style_attribute_rewritten() -> None:
    html = '<div style="background-image:url(/bg2.png)"></div>'
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert f"/archive/r/{TS}/https://example.com/bg2.png" in out


def test_base_tag_removed() -> None:
    html = "<head><base href=\"https://example.com/\"></head><body></body>"
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert "<base" not in out


def test_integrity_and_crossorigin_stripped() -> None:
    html = '<script src="/app.js" integrity="sha384-abc" crossorigin="anonymous"></script>'
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert "integrity" not in out
    assert "crossorigin" not in out


def test_data_and_anchor_urls_left_alone() -> None:
    html = '<img src="data:image/png;base64,AAAA"><a href="#section">Jump</a>'
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert "data:image/png;base64,AAAA" in out
    assert 'href="#section"' in out


def test_no_external_origin_survives_a_realistic_page() -> None:
    """The core security/truthfulness property (task-59 §1.6): every
    external reference must resolve through OUR OWN `/archive/r/<ts>/`
    path, never bare — a bare external URL in an attribute a browser would
    fetch means CSP is the only thing standing between a viewer and today's
    live content under yesterday's byline.
    """
    html = (
        "<html><head>"
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css?family=Foo">'
        '<script src="https://cdn.example.com/lib.js"></script>'
        "</head><body>"
        '<img src="//images.example.com/logo.png">'
        '<a href="https://example.com/other-page">Other</a>'
        "</body></html>"
    )
    out = rewrite_html(html, base_url=BASE, ts=TS)
    for external_host in ("fonts.googleapis.com", "cdn.example.com", "images.example.com"):
        idx = out.index(external_host)
        prefix = out[max(0, idx - 80) : idx]
        assert f"/archive/r/{TS}/" in prefix, f"{external_host} is not routed through replay"


def test_rewrite_css_file() -> None:
    css = (
        "@font-face { src: url('fonts/a.woff2') format('woff2'); } "
        '.bg { background: url("/img/bg.jpg"); }'
    )
    out = rewrite_css(css, base_url=BASE, ts=TS)
    assert f"/archive/r/{TS}/https://example.com/page/fonts/a.woff2" in out
    assert f"/archive/r/{TS}/https://example.com/img/bg.jpg" in out


def test_replay_url_format() -> None:
    assert replay_url("https://example.com/x", ts=TS) == f"/archive/r/{TS}/https://example.com/x"


# --- task-60 §2: iframes left live -----------------------------------------


def test_live_iframe_is_left_byte_for_byte_unrewritten() -> None:
    """The user's own decision (task-60 §2): a map/video/audio embed is
    left pointing at its live host instead of being archived.
    """
    html = '<iframe src="https://www.google.com/maps/embed?pb=abc" width="600"></iframe>'
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert html in out
    assert "/archive/r/" not in out


def test_frame_tag_is_left_live_too() -> None:
    html = '<frameset><frame src="https://www.google.com/maps/embed?pb=abc"></frameset>'
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert 'src="https://www.google.com/maps/embed?pb=abc"' in out
    assert "/archive/r/" not in out


def test_about_blank_iframe_placeholder_is_left_alone() -> None:
    """~1,521 of the portfolio's iframes are exactly this — an inert
    lazy-load placeholder (task-60 plan §0.6) — left alone either way.
    """
    html = '<iframe src="about:blank" data-lazy-src="/widget"></iframe>'
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert html in out


def test_analytics_iframe_still_rewritten_and_blocked_at_replay() -> None:
    """349 of the portfolio's iframes are GTM's own noscript pixel
    (task-60 plan §0.6/§2.2) — loading it for real would send a live hit
    into the client's own GA account on every archived-snapshot view.
    """
    html = '<iframe src="https://www.googletagmanager.com/ns.html?id=GTM-X"></iframe>'
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert f"/archive/r/{TS}/https://www.googletagmanager.com/ns.html?id=GTM-X" in out


def test_live_iframes_flag_off_reverts_to_the_old_rewrite_behavior() -> None:
    """`Settings.archive_live_iframes=False` — the kill-switch (task-60
    §2) — must fully revert to archiving/blocking every iframe, no
    exceptions, matching pre-task-60 behavior.
    """
    html = '<iframe src="https://www.google.com/maps/embed?pb=abc"></iframe>'
    out = rewrite_html(html, base_url=BASE, ts=TS, live_iframes=False)
    assert f"/archive/r/{TS}/https://www.google.com/maps/embed?pb=abc" in out


def test_live_iframe_custom_analytics_hosts_setting_is_honored() -> None:
    html = '<iframe src="https://tracker.example.com/beacon"></iframe>'
    out = rewrite_html(
        html, base_url=BASE, ts=TS, analytics_hosts=frozenset({"tracker.example.com"})
    )
    assert f"/archive/r/{TS}/https://tracker.example.com/beacon" in out


def test_iframe_src_property_and_lazy_attributes_are_untouched_by_html_rewrite() -> None:
    """A lazy-load plugin's `data-lazy-src` on a live-left iframe must
    survive unrewritten too — the whole tag is stashed byte-for-byte, not
    just its `src`.
    """
    html = '<iframe src="about:blank" data-lazy-src="https://open.spotify.com/embed/track/x"></iframe>'
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert 'data-lazy-src="https://open.spotify.com/embed/track/x"' in out


def test_live_iframe_hosts_lists_distinct_non_analytics_hosts() -> None:
    html = (
        '<iframe src="https://www.google.com/maps/embed?pb=abc"></iframe>'
        '<iframe src="https://open.spotify.com/embed/track/x"></iframe>'
        '<iframe src="https://www.google.com/maps/embed?pb=xyz"></iframe>'  # same host again
        '<iframe src="https://www.googletagmanager.com/ns.html?id=GTM-X"></iframe>'  # excluded
        '<iframe src="about:blank"></iframe>'  # excluded, nothing to report
    )
    hosts = live_iframe_hosts(html, base_url=BASE)
    assert hosts == ["open.spotify.com", "www.google.com"]


def test_live_iframe_hosts_empty_when_flag_off() -> None:
    html = '<iframe src="https://www.google.com/maps/embed?pb=abc"></iframe>'
    assert live_iframe_hosts(html, base_url=BASE, live_iframes=False) == []


def test_live_iframe_hosts_empty_for_a_page_with_no_iframes() -> None:
    assert live_iframe_hosts("<p>No embeds here.</p>", base_url=BASE) == []


# --- task-81 §2.1-§2.3: unlazy — replay must not depend on page JS -------


def test_unlazy_promotes_data_lazy_src() -> None:
    html = (
        '<img src="data:image/svg+xml,%3Csvg%3E%3C/svg%3E" '
        'data-lazy-src="/logo.png" alt="logo">'
    )
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert f'src="/archive/r/{TS}/https://example.com/logo.png"' in out
    assert "data:image/svg+xml" not in out
    assert "data-lazy-src" not in out


def test_unlazy_promotes_srcset_and_sizes() -> None:
    html = (
        '<img src="data:image/svg+xml,x" '
        'data-lazy-src="/logo.png" '
        'data-lazy-srcset="/logo.png 1x, /logo-2x.png 2x" '
        'data-lazy-sizes="(max-width: 516px) 100vw, 516px">'
    )
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert (
        f'srcset="/archive/r/{TS}/https://example.com/logo.png 1x, '
        f'/archive/r/{TS}/https://example.com/logo-2x.png 2x"' in out
    )
    assert 'sizes="(max-width: 516px) 100vw, 516px"' in out


def test_unlazy_removes_lazy_attributes() -> None:
    html = '<img src="data:x" data-lazy-src="/a.png" data-lazy-srcset="/a.png 1x">'
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert "data-lazy-src" not in out
    assert "data-lazy-srcset" not in out


def test_unlazy_drops_the_paired_noscript() -> None:
    html = (
        '<img src="data:x" data-lazy-src="/a.png" alt="a">'
        '<noscript><img src="/a.png" alt="a"></noscript>'
    )
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert out.count("<img") == 1
    assert "<noscript>" not in out


def test_unlazy_keeps_unrelated_noscript() -> None:
    html = (
        '<noscript><iframe src="https://www.googletagmanager.com/ns.html?id=GTM-X">'
        "</iframe></noscript>"
        '<noscript><style id="rocket-lazyload-nojs-css">'
        "[data-lazy-src]{display:none!important}</style></noscript>"
    )
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert out.count("<noscript>") == 2
    assert "googletagmanager.com/ns.html" in out
    assert "rocket-lazyload-nojs-css" in out


def test_unlazy_keeps_noscript_not_adjacent_to_a_lazy_image() -> None:
    html = '<p>text</p><noscript><img src="/c.png"></noscript>'
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert "<noscript>" in out
    assert f'src="/archive/r/{TS}/https://example.com/c.png"' in out


def test_unlazy_never_touches_script_bodies() -> None:
    html = (
        '<script>window.lazyLoadOptions = [{elements_selector: '
        '"img[data-lazy-src],.rocket-lazyload,iframe[data-lazy-src]", '
        'data_src: "lazy-src"}];</script>'
    )
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert 'elements_selector: "img[data-lazy-src],.rocket-lazyload,iframe[data-lazy-src]"' in out
    assert "/archive/r/" not in out


def test_unlazy_is_a_noop_without_lazy_markup() -> None:
    html = '<img src="/normal.jpg" alt="normal">'
    out = rewrite_html(html, base_url=BASE, ts=TS)
    assert out == f'<img src="/archive/r/{TS}/https://example.com/normal.jpg" alt="normal">'


def test_unlazy_can_be_disabled() -> None:
    html = '<img src="data:x" data-lazy-src="/a.png">'
    out = rewrite_html(html, base_url=BASE, ts=TS, unlazy=False)
    assert 'src="data:x"' in out
    assert f'data-lazy-src="/archive/r/{TS}/https://example.com/a.png"' in out


def test_unlazy_handles_self_closing_and_bare_img() -> None:
    self_closing = rewrite_html('<img data-lazy-src="/a.png" />', base_url=BASE, ts=TS)
    bare = rewrite_html('<img data-lazy-src="/a.png">', base_url=BASE, ts=TS)
    assert f'/archive/r/{TS}/https://example.com/a.png' in self_closing
    assert f'/archive/r/{TS}/https://example.com/a.png' in bare
    assert self_closing.endswith("/>")
    assert bare.endswith(">") and not bare.endswith("/>")
