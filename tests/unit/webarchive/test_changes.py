from __future__ import annotations

from app.webarchive.changes import diff_fields, snapshot_fields

OLD_HTML = """<html><head><title>Old Title</title>
<meta name="description" content="Old desc"></head>
<body><h1>Old H1</h1><p>Some text here.</p>
<a href="https://example.com/a">A</a><img src="https://example.com/img1.jpg"></body></html>"""

NEW_HTML = """<html><head><title>New Title</title>
<meta name="description" content="Old desc"></head>
<body><h1>Old H1</h1><p>Some longer text here now with more words added.</p>
<a href="https://example.com/b">B</a><img src="https://example.com/img1.jpg">
<img src="https://example.com/img2.jpg"></body></html>"""


def _fields(html: str, *, status: int = 200, final_url: str = "https://example.com/"):
    return snapshot_fields(
        html, base_url="https://example.com/", http_status=status, final_url=final_url
    )


def test_diff_detects_title_text_links_images() -> None:
    diff = diff_fields(_fields(OLD_HTML), _fields(NEW_HTML))
    assert diff is not None
    assert set(diff.change_kinds) >= {"title", "text", "links", "images"}
    assert diff.summary["links"]["added_count"] == 1
    assert diff.summary["links"]["removed_count"] == 1
    assert diff.summary["images"]["added_count"] == 1
    assert diff.summary["images"]["removed_count"] == 0
    assert diff.text_delta_words > 0


def test_no_diff_when_nothing_changed() -> None:
    fields = _fields(OLD_HTML)
    assert diff_fields(fields, fields) is None


def test_status_and_redirect_change_detected() -> None:
    old = _fields(OLD_HTML, status=200, final_url="https://example.com/")
    new = _fields(OLD_HTML, status=301, final_url="https://example.com/new/")
    diff = diff_fields(old, new)
    assert diff is not None
    assert "status" in diff.change_kinds
    assert "redirect" in diff.change_kinds
    assert "title" not in diff.change_kinds  # nothing else actually differs


def test_noindex_transition_detected() -> None:
    old = _fields('<html><head><title>T</title></head><body>x</body></html>')
    new_html = (
        '<html><head><title>T</title><meta name="robots" content="noindex"></head>'
        "<body>x</body></html>"
    )
    new = snapshot_fields(
        new_html, base_url="https://example.com/", http_status=200, final_url="https://example.com/"
    )
    diff = diff_fields(old, new)
    assert diff is not None
    assert "noindex" in diff.change_kinds
    assert diff.summary["noindex"] == {"old": False, "new": True}
