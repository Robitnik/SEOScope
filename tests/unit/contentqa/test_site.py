"""contentqa/site.py — live fetch + H2-H4/section extraction (task-90 §4.4)."""

from __future__ import annotations

import pytest

from app.contentqa import site as site_module
from app.contentqa.site import LivePage, SiteLink, fetch_page
from app.monitor.fetcher import FetchResult, FetchStats
from app.tools.http_safety import HostThrottle

URL = "https://example.com/some-page"


def _fake_fetch(*, status: int | None = 200, error: str | None = None, body: bytes | None = b""):
    def fetch(url, *, throttle, stats, timeout):
        stats.requests_made += 1
        return FetchResult(
            requested_url=url, final_url=url, status=status, hop_count=0,
            headers={"Content-Type": "text/html; charset=utf-8"}, body=body,
            truncated_body=False, elapsed_ms=5, error=error,
        )
    return fetch


def _run(monkeypatch: pytest.MonkeyPatch, html: bytes, *, status: int = 200) -> LivePage:
    monkeypatch.setattr(site_module, "fetch", _fake_fetch(status=status, body=html))
    return fetch_page(URL, throttle=HostThrottle(), stats=FetchStats())


# ── error mapping — honest states, never a false "clean" ────────────────────


def test_blocked_fetch_maps_to_not_ok_with_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(site_module, "fetch", _fake_fetch(status=403, error="blocked", body=b""))
    page = fetch_page(URL, throttle=HostThrottle(), stats=FetchStats())
    assert page.ok is False
    assert page.error == "blocked"
    assert page.signals is None
    assert page.headings == ()
    assert page.sections == ()


def test_connection_error_maps_to_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        site_module, "fetch", _fake_fetch(status=None, error="connection_error", body=None)
    )
    page = fetch_page(URL, throttle=HostThrottle(), stats=FetchStats())
    assert page.ok is False
    assert page.error == "connection_error"


def test_successful_fetch_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b"<html><head><title>T</title></head><body><h1>Hi</h1></body></html>"
    page = _run(monkeypatch, html)
    assert page.ok is True
    assert page.error is None
    assert page.status == 200
    assert page.signals is not None


# ── heading + section extraction ──────────────────────────────────────────


def test_headings_extracted_in_document_order(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b"""
    <html><body>
      <h1>Main Title</h1>
      <p>Intro text.</p>
      <h2>First Section</h2>
      <p>First body.</p>
      <h3>Sub Section</h3>
      <p>Sub body.</p>
      <h2>Second Section</h2>
      <p>Second body.</p>
    </body></html>
    """
    page = _run(monkeypatch, html)
    assert page.headings == (
        ("H1", "Main Title"), ("H2", "First Section"),
        ("H3", "Sub Section"), ("H2", "Second Section"),
    )


def test_sections_group_prose_by_preceding_heading(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b"""
    <html><body>
      <h1>Title</h1>
      <p>Intro.</p>
      <h2>Section A</h2>
      <p>Body A.</p>
    </body></html>
    """
    page = _run(monkeypatch, html)
    assert len(page.sections) == 2
    assert page.sections[0].heading == "Title"
    assert page.sections[0].text == "Intro."
    assert page.sections[1].heading == "Section A"
    assert page.sections[1].text == "Body A."


def test_nav_and_footer_headings_are_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A theme's sidebar/nav "Related Posts"/"Categories" H2/H3 is not part
    of the page's authored content structure — counting it would produce a
    false "heading missing from Frame" finding."""
    html = b"""
    <html><body>
      <nav><h2>Categories</h2></nav>
      <h1>Real Title</h1>
      <p>Real body.</p>
      <footer><h3>Related Posts</h3></footer>
    </body></html>
    """
    page = _run(monkeypatch, html)
    assert page.headings == (("H1", "Real Title"),)


def test_script_content_inside_heading_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b"<html><body><h2>Real<script>var x=1;</script> Heading</h2></body></html>"
    page = _run(monkeypatch, html)
    assert page.headings == (("H2", "Real Heading"),)


def test_no_headings_gives_empty_tuples(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b"<html><body><p>Just a plain page with no headings.</p></body></html>"
    page = _run(monkeypatch, html)
    assert page.headings == ()
    assert page.sections == ()


def test_malformed_html_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b"<html><body><h1>Unclosed <h2>Nested badly</body>"
    page = _run(monkeypatch, html)
    assert page.ok is True  # parser failures are swallowed, not fatal


# ── outgoing links (task-95 §2.2 — CONTENT-only, жива поправка до task-91) ──
#
# task-95 §1.1: the pre-task-95 rule deliberately INCLUDED nav/header/
# footer links (task-91 §3.3) — live-measured to be wrong: it made the
# link-audit verdict say "all claimed links found" on 3/4 EFFYS pages even
# though 0/3 prescribed Framework links were actually IN THE COPY, only in
# the global nav. These tests assert the corrected, content-only rule.


def test_links_split_internal_vs_external(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b"""
    <html><body>
      <p><a href="/menu/">Our menu</a></p>
      <p><a href="https://example.com/catering/">Catering</a></p>
      <p><a href="https://www.example.com/locations/">Locations</a></p>
      <p><a href="https://facebook.com/example">Facebook</a></p>
    </body></html>
    """
    page = _run(monkeypatch, html)
    assert page.internal_links == (
        SiteLink(url="https://example.com/menu/", text="Our menu"),
        SiteLink(url="https://example.com/catering/", text="Catering"),
        SiteLink(url="https://www.example.com/locations/", text="Locations"),
    )
    assert page.external_links == (SiteLink(url="https://facebook.com/example", text="Facebook"),)


def test_relative_links_are_absolutized_against_final_url(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b'<html><body><p><a href="../about">About</a></p></body></html>'

    # final_url differs from requested (redirect) — resolution must use final_url.
    def fetch(url, *, throttle, stats, timeout):
        stats.requests_made += 1
        return FetchResult(
            requested_url=url, final_url="https://example.com/blog/post/",
            status=200, hop_count=1,
            headers={"Content-Type": "text/html; charset=utf-8"}, body=html,
            truncated_body=False, elapsed_ms=5, error=None,
        )
    monkeypatch.setattr(site_module, "fetch", fetch)
    page = fetch_page(URL, throttle=HostThrottle(), stats=FetchStats())
    assert page.internal_links == (SiteLink(url="https://example.com/blog/about", text="About"),)


def test_fragment_javascript_and_mailto_links_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b"""
    <html><body>
      <p><a href="#section">Jump</a></p>
      <p><a href="javascript:void(0)">Do nothing</a></p>
      <p><a href="mailto:hi@example.com">Email us</a></p>
      <p><a href="tel:+15551234567">Call us</a></p>
      <p><a href="/real-page/">Real page</a></p>
    </body></html>
    """
    page = _run(monkeypatch, html)
    assert page.internal_links == (
        SiteLink(url="https://example.com/real-page/", text="Real page"),
    )
    assert page.external_links == ()


def test_duplicate_link_targets_are_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b"""
    <html><body>
      <p><a href="/menu/">Menu</a></p>
      <p><a href="/menu/">See our menu</a></p>
      <p><a href="/menu/#specials">Specials</a></p>
    </body></html>
    """
    page = _run(monkeypatch, html)
    assert page.internal_links == (SiteLink(url="https://example.com/menu/", text="Menu"),)


def test_nav_and_footer_links_are_excluded_now_unlike_task_91(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """task-95 §1.1/§1.3, correcting task-91 §3.3: the OLD rule kept
    nav/footer links precisely so a Framework's "link to /locations/"
    would not be falsely reported "missing from the site" — but that made
    the NEW copy<->site link audit say "found" for links that were never
    actually IN THE COPY, just in the global menu (live-measured: 3/4
    EFFYS pages). A live page's own outgoing-link inventory now excludes
    the navigational chrome; only content prose/CTA links are kept."""
    html = b"""
    <html><body>
      <nav><a href="/about/">About</a></nav>
      <p><a href="/real-content-link/">Real link</a></p>
      <footer><a href="/privacy/">Privacy</a></footer>
    </body></html>
    """
    page = _run(monkeypatch, html)
    assert page.internal_links == (
        SiteLink(url="https://example.com/real-content-link/", text="Real link"),
    )


def test_breadcrumb_link_is_excluded_even_without_a_nav_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """task-95 §1.3: EFFYS wraps its breadcrumb in a bare `<p id=
    "breadcrumbs">`, no `<nav>` at all — the word "breadcrumb" in `id`/
    `class` is the only reliable signal there, live-measured."""
    html = b"""
    <html><body>
      <p id="breadcrumbs"><a href="/">Home</a></p>
      <p><a href="/real-content-link/">Real link</a></p>
    </body></html>
    """
    page = _run(monkeypatch, html)
    assert page.internal_links == (
        SiteLink(url="https://example.com/real-content-link/", text="Real link"),
    )


def test_elementor_widget_class_does_not_make_a_link_chrome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """task-95 §1.3, live pitfall found and fixed while building the rule:
    `elementor-widget`/`elementor-element` classes sit on EVERY element of
    an Elementor page, including genuine prose — a naive "class contains
    a chrome-ish word" heuristic swallowed 100% of content links on the
    first draft. Only semantic tags/roles/"breadcrumb" may exclude."""
    html = b"""
    <html><body>
      <div class="elementor-element elementor-widget">
        <p><a href="/real-content-link/">Real link</a></p>
      </div>
    </body></html>
    """
    page = _run(monkeypatch, html)
    assert page.internal_links == (
        SiteLink(url="https://example.com/real-content-link/", text="Real link"),
    )


def test_cta_button_outside_prose_is_included(monkeypatch: pytest.MonkeyPatch) -> None:
    """task-95 §1.7 пастка 2: EFFYS's "Order Now"/"Kosher Certified" CTAs
    live directly in a `<div>`, not wrapped in `<p>` — a "prose ancestor
    only" rule would have missed them entirely."""
    html = b"""
    <html><body>
      <div class="elementor-element">
        <a class="elementor-button" href="/order/">Order Now</a>
      </div>
    </body></html>
    """
    page = _run(monkeypatch, html)
    assert page.internal_links == (SiteLink(url="https://example.com/order/", text="Order Now"),)


def test_woocommerce_cart_and_sort_links_are_excluded_as_self_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """task-95 §1.7 пастка 3: admitting CTA buttons re-admits WooCommerce
    "add to cart"/sort links, which are buttons too — they are the SAME
    page as the one being fetched, differing only by query string, and
    get filtered as self-links (query-stripped key match)."""
    html = b"""
    <html><body>
      <p><a class="button" href="/some-page/?add-to-cart=817533">Add to cart</a></p>
      <p><a class="button" href="/some-page/?orderby=price">Sort by price</a></p>
      <p><a class="button" href="/other-page/">Related product</a></p>
    </body></html>
    """
    page = _run(monkeypatch, html)
    assert page.internal_links == (
        SiteLink(url="https://example.com/other-page/", text="Related product"),
    )


def test_gallery_image_link_is_excluded_but_pdf_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    """task-95 §1.2/§1.7: lightbox `.jpg` links are UI chrome (34 on one
    live LAXTON page); a PDF certificate link stays IN — that is exactly
    how a stale-certificate content drift gets caught (EFFYS, §1.7)."""
    html = b"""
    <html><body>
      <p><a href="/gallery/photo.jpg">Photo</a></p>
      <p><a href="/cert-2027.pdf">Certificate</a></p>
    </body></html>
    """
    page = _run(monkeypatch, html)
    assert page.internal_links == (
        SiteLink(url="https://example.com/cert-2027.pdf", text="Certificate"),
    )


def test_link_with_no_anchor_text_falls_back_to_empty_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = b'<html><body><p><a href="/icon-only/"><svg></svg></a></p></body></html>'
    page = _run(monkeypatch, html)
    assert page.internal_links == (SiteLink(url="https://example.com/icon-only/", text=""),)


def test_blocked_fetch_gives_empty_links(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(site_module, "fetch", _fake_fetch(status=403, error="blocked", body=b""))
    page = fetch_page(URL, throttle=HostThrottle(), stats=FetchStats())
    assert page.internal_links == ()
    assert page.external_links == ()


# ── task-92 §1.4/§2.2: block-level potok + lang, independent second pass ────


def test_blocks_keep_paragraph_boundaries_headings_and_sections_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`blocks` must NOT collapse into `headings`/`sections`' run-on
    string (task-92 DoD #8: lanes I/J and task-91's matrix must see the
    exact same `headings`/`sections` as before)."""
    html = b"""
    <html><body>
      <h1>Title</h1>
      <p>First paragraph.</p>
      <p>Second paragraph.</p>
    </body></html>
    """
    page = _run(monkeypatch, html)
    assert [b.text for b in page.blocks] == ["Title", "First paragraph.", "Second paragraph."]
    assert page.blocks[0].kind == "heading" and page.blocks[0].level == "H1"
    assert page.blocks[1].kind == "para"
    # Unaffected legacy fields — same shape as before this task.
    assert page.headings == (("H1", "Title"),)
    assert len(page.sections) == 1
    assert page.sections[0].text == "First paragraph. Second paragraph."


def test_blocks_split_bullet_list_items_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live defect this fixes: nine `<li>` bullets used to collapse into
    ONE `sections` string; `blocks` keeps each `<li>` as its own item."""
    html = b"""
    <html><body>
      <h2>Specifications</h2>
      <ul><li>Real-time EVP scanning</li><li>Built-in speaker</li></ul>
    </body></html>
    """
    page = _run(monkeypatch, html)
    items = [b for b in page.blocks if b.kind == "item"]
    assert [b.text for b in items] == ["Real-time EVP scanning", "Built-in speaker"]


def test_blocks_br_tag_is_a_block_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b"<html><body><p>Line one<br>Line two</p></body></html>"
    page = _run(monkeypatch, html)
    assert [b.text for b in page.blocks] == ["Line one", "Line two"]


def test_blocks_exclude_head_title_and_nav(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live defect this fixes: without excluding `<title>`, the first row
    of the sentence-level comparison table was
    `"... Café UWS Skip to content"` (EFFYS, every page)."""
    html = b"""
    <html><head><title>Site Title Skip to content</title></head>
      <body><nav><p>Nav text</p></nav><p>Real body text.</p></body>
    </html>
    """
    page = _run(monkeypatch, html)
    assert [b.text for b in page.blocks] == ["Real body text."]


def test_blocks_empty_on_unreachable_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(site_module, "fetch", _fake_fetch(status=403, error="blocked", body=b""))
    page = fetch_page(URL, throttle=HostThrottle(), stats=FetchStats())
    assert page.blocks == ()
    assert page.lang is None


def test_lang_is_extracted_from_html_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b'<html lang="sv-SE"><body><h1>Hej</h1></body></html>'
    page = _run(monkeypatch, html)
    assert page.lang == "sv-SE"


def test_lang_is_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b"<html><body><h1>Hi</h1></body></html>"
    page = _run(monkeypatch, html)
    assert page.lang is None
