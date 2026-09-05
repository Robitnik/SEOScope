"""Subresource discovery for the Web Archive (task-59 §0.4/§1.3).

Regex-based over already-fetched HTML/CSS text — not a browser, not a full
HTML parse tree. Live-measured against 1,734 stored client pages (task-59
plan §0.4): this pattern set found ~57K unique asset URLs across the
portfolio at p50 367 references per page, matching what a browser would
actually load closely enough for an SEO/marketing archive (this is not a
pixel-perfect visual regression tool).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.webarchive.urls import resolve

_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_REL_RE = re.compile(r"""rel\s*=\s*["']?([a-z \-]+)["']?""", re.IGNORECASE)
_SCRIPT_SRC_RE = re.compile(r"""<script[^>]+src\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_IMG_SRC_RE = re.compile(r"""<img[^>]+src\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_SRCSET_RE = re.compile(r"""srcset\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_MEDIA_SRC_RE = re.compile(
    r"""<(?:source|video|audio)[^>]+src\s*=\s*["']([^"']+)["']""", re.IGNORECASE
)
_STYLE_ATTR_URL_RE = re.compile(
    r"""style\s*=\s*["'][^"']*?url\(\s*(['"]?)(?!data:)([^)'"]+)\1""", re.IGNORECASE
)
_STYLE_TAG_RE = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.IGNORECASE | re.DOTALL)
_CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)(?!data:)([^)'"]+)\1\s*\)""", re.IGNORECASE)
_AS_ATTR_RE = re.compile(r"""\bas\s*=\s*["']?([a-z]+)["']?""", re.IGNORECASE)
_SCRIPT_TAG_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_SCRIPT_SRC_ATTR_RE = re.compile(r"""\bsrc\s*=\s*["']""", re.IGNORECASE)
_NON_JS_SCRIPT_TYPE_RE = re.compile(
    r"""\btype\s*=\s*["'](?:application/(?:json|ld\+json)|text/(?:template|html))["']""",
    re.IGNORECASE,
)
# Any of the three JS string-literal forms. Content is NOT unescaped (a
# real `\'`/`\n` inside a path is vanishingly rare and, if present, simply
# fails the later extension/host checks harmlessly) — this only needs to
# find candidate literals, not execute them.
_JS_STRING_RE = re.compile(r"""(['"`])((?:\\.|(?!\1)[^\\\n])*)\1""", re.DOTALL)

_STYLESHEET_MARKERS = ("stylesheet",)
_ICON_MARKERS = ("icon", "manifest", "apple-touch-icon", "mask-icon")
_FONT_EXTENSIONS = frozenset({"woff", "woff2", "ttf", "otf", "eot"})
_IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "webp", "avif", "gif", "ico", "svg"})
_MEDIA_EXTENSIONS = frozenset({"mp4", "webm"})
# task-63 §3.3: deliberately narrow — a literal must end (before any `?`)
# in one of these to even be considered. Anything else (a CSS selector
# string, a font-family name, a translated UI label) is left alone.
_JS_ASSET_EXTENSIONS = (
    _FONT_EXTENSIONS | _IMAGE_EXTENSIONS | _MEDIA_EXTENSIONS | frozenset({"js", "css", "json"})
)


@dataclass(frozen=True)
class AssetRef:
    kind: str  # "css" | "js" | "img" | "font" | "media" | "other"
    url: str


def _extension_of(url: str) -> str:
    path = urlsplit(url).path
    return path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""


def _looks_like_font(url: str) -> bool:
    return _extension_of(url) in _FONT_EXTENSIONS


def discover_html_assets(html: str, base_url: str) -> list[AssetRef]:
    """Every subresource an ordinary browser would fetch for this page —
    stylesheets/preloaded/icon `<link>`s, `<script src>`, `<img src>` and
    `srcset`, `<source>`/`<video>`/`<audio> src`, `url(...)` inside an
    inline `style="..."` attribute, AND `url(...)` inside inline
    `<style>...</style>` block bodies (task-60 §1.1 — live-root-caused: a
    real client's `.elementor-motion-effects-layer{background-image:
    url(...)}` rule sat in exactly this kind of block and was never
    discovered, so the image was never captured and 404'd at replay).
    First-seen kind wins on a URL collision.
    """
    found: dict[str, str] = {}

    def _add(kind: str, raw: str) -> None:
        absolute = resolve(base_url, raw)
        if absolute is not None:
            found.setdefault(absolute, kind)

    for tag in _LINK_TAG_RE.findall(html):
        href_match = _HREF_RE.search(tag)
        if not href_match:
            continue
        rel_match = _REL_RE.search(tag)
        rel = (rel_match.group(1).lower() if rel_match else "").strip()
        href = href_match.group(1)
        if any(marker in rel for marker in _STYLESHEET_MARKERS):
            _add("css", href)
        elif any(marker in rel for marker in _ICON_MARKERS):
            _add("img", href)
        elif "preload" in rel or "prefetch" in rel:
            # A preload/prefetch hint's actual kind lives in `as=`, not `rel=`
            # (`<link rel="preload" as="font" href="...">`) — `rel` alone
            # only ever says "load this early", never what it is.
            as_match = _AS_ATTR_RE.search(tag)
            as_value = as_match.group(1).lower() if as_match else ""
            kind = {"font": "font", "style": "css", "script": "js", "image": "img"}.get(
                as_value, "font" if _looks_like_font(href) else "other"
            )
            _add(kind, href)

    for match in _SCRIPT_SRC_RE.findall(html):
        _add("js", match)
    for match in _IMG_SRC_RE.findall(html):
        _add("img", match)
    for match in _SRCSET_RE.findall(html):
        for candidate in match.split(","):
            url_part = candidate.strip().split(" ")[0]
            if url_part:
                _add("img", url_part)
    for match in _MEDIA_SRC_RE.findall(html):
        _add("media", match)
    for _quote, match in _STYLE_ATTR_URL_RE.findall(html):
        _add("font" if _looks_like_font(match) else "img", match)
    for style_body in _STYLE_TAG_RE.findall(html):
        for ref in discover_css_assets(style_body, base_url):
            found.setdefault(ref.url, ref.kind)

    return [AssetRef(kind=kind, url=url) for url, kind in found.items()]


def inline_script_bodies(html: str) -> list[str]:
    """Bodies of `<script>` tags with no `src` and a JS (or unspecified)
    `type` — text a page's own bootstrap logic actually EXECUTES, as
    opposed to a JSON-LD payload or a client-side template that merely
    looks like a script tag. Feeds `discover_js_string_assets` below: a
    lazy-load bootstrap that calls `load_js('/wp-content/...')` from an
    inline block (not a `<script src>` file) would otherwise never be
    scanned at all (task-63 §3.3).
    """
    bodies = []
    for attrs, body in _SCRIPT_TAG_RE.findall(html):
        if _SCRIPT_SRC_ATTR_RE.search(attrs) or _NON_JS_SCRIPT_TYPE_RE.search(attrs):
            continue
        bodies.append(body)
    return bodies


def discover_js_string_assets(
    js_text: str, base_url: str, *, limit: int | None = None
) -> list[AssetRef]:
    """Heuristic recovery for an asset referenced ONLY as a JS string
    literal (task-63 §3.3, live-measured on a real client site: WP
    Rocket's own LazyLoad library and a cache-busted stylesheet are both
    loaded this way — via a custom deferred-load bootstrap — and never
    named anywhere in the page's static HTML, so `discover_html_assets`
    cannot see either).

    Deliberately conservative, in three ways:
    - a candidate must resolve to the SAME HOST as `base_url` — a font
      family name, a CDN library id, or any other third-party-looking
      literal is left alone; this is for the page's OWN assets only.
    - the path portion (everything before an optional `?`) must end in a
      HARD extension allowlist — a CSS selector string, a UI label, or any
      other prose literal that merely contains a dot never qualifies.
    - a template literal with `${...}` INSIDE the path is skipped
      outright rather than guessed at (a wrong guess would put a
      fabricated file in the archive under a real-looking name); `${...}`
      confined to the QUERY (a cache-buster: `` `/a/b.js?v=${t}` ``) is
      accepted with the query simply dropped — the same file, just at
      request time under a timestamp this archive never controls anyway.
    """
    base_host = urlsplit(base_url).hostname
    found: dict[str, str] = {}
    for _quote, raw in _JS_STRING_RE.findall(js_text):
        path_part = raw.split("?", 1)[0]
        if "${" in path_part or " " in path_part or not path_part:
            continue
        extension = _extension_of(path_part)
        if extension not in _JS_ASSET_EXTENSIONS:
            continue
        absolute = resolve(base_url, path_part)
        if absolute is None or urlsplit(absolute).hostname != base_host:
            continue
        kind = (
            "css" if extension == "css"
            else "js" if extension == "js"
            else "font" if extension in _FONT_EXTENSIONS
            else "media" if extension in _MEDIA_EXTENSIONS
            else "img" if extension in _IMAGE_EXTENSIONS
            else "other"
        )
        found.setdefault(absolute, kind)
        if limit is not None and len(found) >= limit:
            break
    return [AssetRef(kind=kind, url=url) for url, kind in found.items()]


def discover_css_assets(css_text: str, base_url: str) -> list[AssetRef]:
    """`url(...)` references inside a fetched CSS FILE — background images,
    `@font-face` sources, `@import`s. Runs on the CSS asset's OWN body, one
    level deep only (a CSS file importing another CSS file that itself
    references assets is not followed further — task-59 plan's accepted
    limit, matching a plain regex-based archive rather than a full browser).
    """
    found: dict[str, str] = {}
    for _quote, raw in _CSS_URL_RE.findall(css_text):
        absolute = resolve(base_url, raw)
        if absolute is not None:
            found.setdefault(absolute, "font" if _looks_like_font(absolute) else "img")
    return [AssetRef(kind=kind, url=url) for url, kind in found.items()]
