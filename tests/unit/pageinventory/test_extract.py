"""`app.pageinventory.extract` — breadcrumb/keyword extraction from raw
stored HTML (task-57 §1.2/§1.3)."""

from __future__ import annotations

from app.pageinventory.extract import extract_page, page_label

_LD_JSON_GRAPH = """
<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [
  {"@type": "WebPage", "name": "Some page"},
  {"@type": "BreadcrumbList", "itemListElement": [
    {"@type": "ListItem", "position": 2, "name": "Shop All", "item": "https://x.test/shop/"},
    {"@type": "ListItem", "position": 1, "name": "Home", "item": "https://x.test/"},
    {"@type": "ListItem", "position": 3, "name": "Widget"}
  ]}
]}
</script>
</head><body></body></html>
"""


def test_breadcrumbs_from_ld_json_graph_sorted_by_position() -> None:
    result = extract_page(_LD_JSON_GRAPH)
    assert result.breadcrumb_source == "json_ld"
    assert [n.name for n in result.breadcrumbs] == ["Home", "Shop All", "Widget"]
    assert result.breadcrumbs[1].url == "https://x.test/shop/"
    assert result.breadcrumbs[2].url is None


def test_page_label_drops_leading_home_crumb() -> None:
    result = extract_page(_LD_JSON_GRAPH)
    assert page_label(result.breadcrumbs) == "Shop All > Widget"


def test_page_label_of_homepage_trail_is_home() -> None:
    html = """
    <script type="application/ld+json">
    {"@type": "BreadcrumbList", "itemListElement": [
      {"@type": "ListItem", "position": 1, "name": "Home"}
    ]}
    </script>
    """
    result = extract_page(html)
    assert page_label(result.breadcrumbs) == "Home"


def test_breadcrumb_names_strip_soft_hyphen_and_decode_entities() -> None:
    html = """
    <script type="application/ld+json">
    {"@type": "BreadcrumbList", "itemListElement": [
      {"@type": "ListItem", "position": 1, "name": "Street&shy;wear &amp; Co"}
    ]}
    </script>
    """
    result = extract_page(html)
    assert result.breadcrumbs[0].name == "Streetwear & Co"


def test_bare_object_not_wrapped_in_graph_still_parses() -> None:
    html = """
    <script type="application/ld+json">
    {"@type": "BreadcrumbList", "itemListElement": [
      {"@type": "ListItem", "position": 1, "name": "Home"},
      {"@type": "ListItem", "position": 2, "name": "About"}
    ]}
    </script>
    """
    result = extract_page(html)
    assert [n.name for n in result.breadcrumbs] == ["Home", "About"]


def test_broken_json_ld_does_not_raise_and_falls_through_to_dom() -> None:
    # Live markup shape (task-57 §1.2 evidence): each crumb is its own
    # element, not raw text with a literal separator character.
    html = """
    <script type="application/ld+json">{not valid json</script>
    <div id="breadcrumbs"><a href="/">Home</a><a href="/contact/">Contact</a></div>
    """
    result = extract_page(html)
    assert result.breadcrumb_source == "dom"
    assert [n.name for n in result.breadcrumbs] == ["Home", "Contact"]


def test_dom_fallback_when_no_json_ld_present() -> None:
    # Realistic Yoast-style markup (task-57 §1.2 live evidence): every
    # crumb AND separator is its own element, never raw dangling text.
    html = """
    <p id="breadcrumbs">
      <span><a href="/">Home</a></span>
      <span class="separator">/</span>
      <span><a href="/blog/">Blog</a></span>
      <span class="separator">/</span>
      <span>Post</span>
    </p>
    """
    result = extract_page(html)
    assert result.breadcrumb_source == "dom"
    assert [n.name for n in result.breadcrumbs] == ["Home", "Blog", "Post"]


def test_no_breadcrumbs_anywhere_is_an_honest_none_not_a_crash() -> None:
    html = "<html><body><p>Nothing here.</p></body></html>"
    result = extract_page(html)
    assert result.breadcrumb_source == "none"
    assert result.breadcrumbs == ()


def test_meta_keywords_extracted_when_present() -> None:
    html = '<meta name="keywords" content="hiking boots, trail shoes, outdoor gear">'
    result = extract_page(html)
    assert result.keywords_source == "meta"
    assert result.keywords == ("hiking boots", "trail shoes", "outdoor gear")


def test_meta_keywords_absent_is_the_honest_none_state() -> None:
    html = "<html><head><title>No keywords tag</title></head></html>"
    result = extract_page(html)
    assert result.keywords_source == "none"
    assert result.keywords == ()


def test_meta_keywords_empty_content_is_still_the_none_state() -> None:
    html = '<meta name="keywords" content="">'
    result = extract_page(html)
    assert result.keywords_source == "none"
    assert result.keywords == ()
