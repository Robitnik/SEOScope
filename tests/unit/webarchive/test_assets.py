from __future__ import annotations

from app.webarchive.assets import discover_css_assets, discover_html_assets

BASE = "https://example.com/page/"


def test_discover_html_assets_finds_background_image_inside_inline_style_block() -> None:
    """Root cause of the HWELL bug (task-60 plan §0.1): a real Elementor
    fragment references its background image ONLY from an inline
    `<style>` block, never from an `<img>`/`srcset` the pre-task-60
    discovery already covered.
    """
    html = (
        "<html><head><style>"
        ".elementor-motion-effects-layer{background-image:"
        'url("https://example.com/wp-content/uploads/2025/02/image-1.webp")}'
        "</style></head><body></body></html>"
    )
    refs = discover_html_assets(html, BASE)
    urls = {ref.url: ref.kind for ref in refs}
    assert urls["https://example.com/wp-content/uploads/2025/02/image-1.webp"] == "img"


def test_discover_html_assets_finds_font_face_inside_inline_style_block() -> None:
    html = (
        "<style>@font-face{font-family:'Acme';"
        "src:url('/fonts/acme.woff2') format('woff2');}</style>"
    )
    refs = discover_html_assets(html, BASE)
    urls = {ref.url: ref.kind for ref in refs}
    assert urls["https://example.com/fonts/acme.woff2"] == "font"


def test_discover_html_assets_resolves_inline_style_urls_relative_to_page() -> None:
    html = "<style>body{background:url(../shared/bg.jpg)}</style>"
    refs = discover_html_assets(html, "https://example.com/page/sub/")
    assert any(ref.url == "https://example.com/page/shared/bg.jpg" for ref in refs)


def test_discover_html_assets_ignores_empty_style_blocks() -> None:
    html = "<style></style><style>   </style>"
    assert discover_html_assets(html, BASE) == []


def test_discover_html_assets_does_not_duplicate_a_url_already_found_elsewhere() -> None:
    """First-seen kind wins on a collision — an `<img>` tag's own
    discovery (kind `img`) must not be overwritten by a later, redundant
    inline-`<style>` reference to the exact same URL.
    """
    html = (
        '<img src="/logo.png">'
        "<style>.logo{background:url(/logo.png)}</style>"
    )
    refs = discover_html_assets(html, BASE)
    matching = [ref for ref in refs if ref.url == "https://example.com/logo.png"]
    assert len(matching) == 1


def test_discover_css_assets_still_used_directly_for_fetched_css_files() -> None:
    """Unchanged existing behavior (task-59): a CSS FILE's own body is
    scanned the same way — task-60 only adds the inline-`<style>`-block
    source, it does not touch this path.
    """
    css = ".hero{background:url(bg.jpg)}"
    refs = discover_css_assets(css, BASE)
    assert refs and refs[0].url == "https://example.com/page/bg.jpg"
