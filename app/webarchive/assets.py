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

_STYLESHEET_MARKERS = ("stylesheet",)
_ICON_MARKERS = ("icon", "manifest", "apple-touch-icon", "mask-icon")
_FONT_EXTENSIONS = frozenset({"woff", "woff2", "ttf", "otf", "eot"})


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
