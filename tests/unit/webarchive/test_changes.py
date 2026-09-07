from __future__ import annotations

from app.webarchive.changes import diff_fields, extract_images, extract_links, snapshot_fields

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


# --- task-82 §1: WordPress escapes `&` in href/src; resolve the real URL ---


def test_extract_links_unescapes_named_and_numeric_entities() -> None:
    html = (
        '<a href="/x/?a=1&amp;b=2">A</a>'
        '<a href="/y/?taxonomy=product_shipping_class&#038;term=123">B</a>'
        '<a href="/z/?a=1&b=2">C</a>'  # already a bare `&` — must not double-escape
    )
    links = extract_links(html, "https://example.com/")
    assert links == {
        "https://example.com/x/?a=1&b=2",
        "https://example.com/y/?taxonomy=product_shipping_class&term=123",
        "https://example.com/z/?a=1&b=2",
    }


def test_extract_images_unescapes_entities() -> None:
    html = '<img src="/img.php?w=100&amp;h=200">'
    images = extract_images(html, "https://example.com/")
    assert images == {"https://example.com/img.php?w=100&h=200"}
