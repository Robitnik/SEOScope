from __future__ import annotations

from app.webarchive.rewrite import replay_url, rewrite_css, rewrite_html

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
