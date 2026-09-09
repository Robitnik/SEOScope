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
