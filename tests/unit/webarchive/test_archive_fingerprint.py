"""Content fingerprint (task-59 §0.2/§0.3) — the single highest-value test
in this module: a wrong answer here costs either 8x storage (false
"changed") or a silently lost real edit (false "unchanged").

The noise patterns below are not hypothetical — each one is a live
measurement from a real client site (task-59 plan §0.2/§0.3): Cloudflare
Zaraz's per-render `nonce`, a WP Fastest Cache build-timestamp HTML comment,
an Elementor rating widget minting a fresh random SVG gradient `id` on every
render, and ordinary whitespace/formatting drift.
"""

from __future__ import annotations

from app.webarchive.fingerprint import content_fingerprint

BASE_HTML = """<!doctype html>
<html>
<head>
<title>Alpha Style — Home</title>
<meta name="description" content="Shop our latest collection.">
<script data-cfasync="false" nonce="9792f9d2-17ff-468a-8bd5-6efaa8fde52a">
console.log("nonce-varies-per-load", Math.random());
</script>
</head>
<body>
<!-- WP Fastest Cache file was created in 1.688 seconds, on September 2, 2026 @ 9:26 am -->
<div class="elementor-19186 elementor-element elementor-element-4b8d0dd">
  <svg><linearGradient id="asrg5-9347"></linearGradient><path fill="url(#asrg5-9347)"/></svg>
  <a href="/products/hoodie">Hoodie</a>
  <img src="/img/hoodie.jpg" alt="Hoodie">
</div>
</body>
</html>"""


def _mutate(html: str, needle: str, replacement: str) -> str:
    assert needle in html, f"fixture no longer contains {needle!r}"
    return html.replace(needle, replacement, 1)


class TestNoiseIsIgnored:
    def test_nonce_changes(self) -> None:
        b = _mutate(
            BASE_HTML,
            "9792f9d2-17ff-468a-8bd5-6efaa8fde52a",
            "00000000-0000-0000-0000-000000000000",
        )
        assert content_fingerprint(BASE_HTML) == content_fingerprint(b)

    def test_html_comment_changes(self) -> None:
        b = _mutate(
            BASE_HTML,
            "created in 1.688 seconds, on September 2, 2026 @ 9:26 am",
            "created in 9.999 seconds, on January 1, 2099 @ 1:01 am",
        )
        assert content_fingerprint(BASE_HTML) == content_fingerprint(b)

    def test_random_svg_gradient_id_changes(self) -> None:
        b = _mutate(BASE_HTML, "asrg5-9347", "asrg5-2713")
        assert content_fingerprint(BASE_HTML) == content_fingerprint(b)

    def test_inline_script_body_changes(self) -> None:
        b = _mutate(
            BASE_HTML,
            'console.log("nonce-varies-per-load", Math.random());',
            'console.log("a completely different script body");',
        )
        assert content_fingerprint(BASE_HTML) == content_fingerprint(b)

    def test_whitespace_only_changes(self) -> None:
        b = BASE_HTML.replace("\n", "\n  ")
        assert content_fingerprint(BASE_HTML) == content_fingerprint(b)


class TestRealEditsAreCaught:
    def test_title_changes(self) -> None:
        b = _mutate(BASE_HTML, "Alpha Style — Home", "Alpha Style — New Arrivals")
        assert content_fingerprint(BASE_HTML) != content_fingerprint(b)

    def test_visible_text_changes(self) -> None:
        b = _mutate(BASE_HTML, ">Hoodie<", ">Hoodie XL<")
        assert content_fingerprint(BASE_HTML) != content_fingerprint(b)

    def test_meta_description_changes(self) -> None:
        b = _mutate(BASE_HTML, "Shop our latest collection.", "Shop our WINTER collection.")
        assert content_fingerprint(BASE_HTML) != content_fingerprint(b)

    def test_img_src_changes(self) -> None:
        b = _mutate(BASE_HTML, "/img/hoodie.jpg", "/img/hoodie-v2.jpg")
        assert content_fingerprint(BASE_HTML) != content_fingerprint(b)

    def test_new_link_added(self) -> None:
        b = BASE_HTML.replace("</body>", '<a href="/products/new-item">New item</a></body>')
        assert content_fingerprint(BASE_HTML) != content_fingerprint(b)
