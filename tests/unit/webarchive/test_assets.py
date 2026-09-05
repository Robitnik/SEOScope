from __future__ import annotations

from app.webarchive.assets import (
    discover_css_assets,
    discover_html_assets,
    discover_js_string_assets,
    inline_script_bodies,
)

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


# --- discover_js_string_assets (task-63 §3.3) -----------------------------
#
# Live root cause: WP Rocket's own LazyLoad library is loaded via a custom
# deferred-load bootstrap (`load_js('/wp-content/plugins/rocket-lazy-load/
# assets/js/16.1/lazyload.min.js')`) — never named ANYWHERE in the page's
# static HTML, so `discover_html_assets` cannot see it at all.

def test_discover_js_string_assets_finds_a_same_host_js_reference() -> None:
    js = "load_js('/wp-content/plugins/rocket-lazy-load/assets/js/16.1/lazyload.min.js');"
    refs = discover_js_string_assets(js, BASE)
    urls = {ref.url: ref.kind for ref in refs}
    lazyload_url = (
        "https://example.com/wp-content/plugins/rocket-lazy-load/assets/js/16.1/lazyload.min.js"
    )
    assert urls[lazyload_url] == "js"


def test_discover_js_string_assets_drops_a_cache_buster_query() -> None:
    js = "load_js(`/wp-content/uploads/custom-css-js/13950.js?v=${timestamp}`);"
    refs = discover_js_string_assets(js, BASE)
    urls = [ref.url for ref in refs]
    assert urls == ["https://example.com/wp-content/uploads/custom-css-js/13950.js"]


def test_discover_js_string_assets_skips_interpolation_inside_the_path() -> None:
    # The PATH itself is unresolvable — guessing would fabricate a filename.
    js = "load_js(`/api/${name}.js`);"
    assert discover_js_string_assets(js, BASE) == []


def test_discover_js_string_assets_ignores_third_party_host_literals() -> None:
    js = "load_js('https://cdn.other.com/lib.js');"
    assert discover_js_string_assets(js, BASE) == []


def test_discover_js_string_assets_ignores_non_asset_string_literals() -> None:
    js = "console.log('hello.world'); var x = 'a.b.c';"
    assert discover_js_string_assets(js, BASE) == []


def test_discover_js_string_assets_respects_the_limit() -> None:
    js = "\n".join(f"load_js('/a{i}.js');" for i in range(10))
    refs = discover_js_string_assets(js, BASE, limit=3)
    assert len(refs) == 3


def test_discover_js_string_assets_classifies_css_and_font_extensions() -> None:
    js = "load_css('/style.css'); loadFont('/fonts/acme.woff2');"
    refs = discover_js_string_assets(js, BASE)
    kinds = {ref.url: ref.kind for ref in refs}
    assert kinds["https://example.com/style.css"] == "css"
    assert kinds["https://example.com/fonts/acme.woff2"] == "font"


# --- inline_script_bodies --------------------------------------------------

def test_inline_script_bodies_skips_a_script_with_a_src_attribute() -> None:
    html = '<script src="/a.js">ignored body</script><script>real body</script>'
    assert inline_script_bodies(html) == ["real body"]


def test_inline_script_bodies_skips_json_ld_and_template_types() -> None:
    html = (
        '<script type="application/ld+json">{"a": 1}</script>'
        '<script type="text/template">not js</script>'
        '<script type="text/javascript">actual code</script>'
    )
    assert inline_script_bodies(html) == ["actual code"]
