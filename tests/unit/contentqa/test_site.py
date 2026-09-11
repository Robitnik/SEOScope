"""contentqa/site.py — live fetch + H2-H4/section extraction (task-90 §4.4)."""

from __future__ import annotations

import pytest

from app.contentqa import site as site_module
from app.contentqa.site import LivePage, fetch_page
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


# ── outgoing links (task-91 §1.3/§2.4) ────────────────────────────────────


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
        "Our menu → /menu/", "Catering → /catering/", "Locations → /locations/",
    )
    assert page.external_links == ("Facebook → /example",)


def test_relative_links_are_absolutized_against_final_url(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b'<html><body><a href="../about">About</a></body></html>'

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
    assert page.internal_links == ("About → /blog/about",)


def test_fragment_javascript_and_mailto_links_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b"""
    <html><body>
      <a href="#section">Jump</a>
      <a href="javascript:void(0)">Do nothing</a>
      <a href="mailto:hi@example.com">Email us</a>
      <a href="tel:+15551234567">Call us</a>
      <a href="/real-page/">Real page</a>
    </body></html>
    """
    page = _run(monkeypatch, html)
    assert page.internal_links == ("Real page → /real-page/",)
    assert page.external_links == ()


def test_duplicate_link_targets_are_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b"""
    <html><body>
      <a href="/menu/">Menu</a>
      <a href="/menu/">See our menu</a>
      <a href="/menu/#specials">Specials</a>
    </body></html>
    """
    page = _run(monkeypatch, html)
    assert page.internal_links == ("Menu → /menu/",)


def test_nav_and_footer_links_are_included_unlike_headings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deliberately the OPPOSITE rule from headings (task-91 §3.3, a real
    bug found and fixed by live verification on SHB): a Framework claiming
    "links to /locations/" was reported as "missing from the live site"
    even though the link was genuinely present, only inside a `<nav>`
    location-switcher widget. A clickable link is a real link regardless
    of which tag wraps it — only headings represent "authored page
    structure" and stay excluded."""
    html = b"""
    <html><body>
      <nav><a href="/about/">About</a></nav>
      <p><a href="/real-content-link/">Real link</a></p>
      <footer><a href="/privacy/">Privacy</a></footer>
    </body></html>
    """
    page = _run(monkeypatch, html)
    assert page.internal_links == (
        "About → /about/", "Real link → /real-content-link/", "Privacy → /privacy/",
    )


def test_link_with_no_anchor_text_falls_back_to_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    html = b'<html><body><a href="/icon-only/"><svg></svg></a></body></html>'
    page = _run(monkeypatch, html)
    assert page.internal_links == ("https://example.com/icon-only/",)


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
