"""Breadcrumbs + `<meta name="keywords">` extraction from a stored monitor
blob (task-57 §1.2/§1.3). stdlib `html.parser`/`json`/`re` only, mirroring
`app/monitor/fingerprint.py`'s own no-dependency convention.

Breadcrumbs come from JSON-LD `BreadcrumbList` (Yoast et al.) — measured
100% coverage on live data (task-57 §1.2). A DOM `#breadcrumbs`/
`.breadcrumbs` fallback covers sites without structured data; when neither
is present the page simply has no breadcrumb trail, which is a fact, not an
extraction failure.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser

_LD_JSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I
)
_KEYWORDS_META_RE = re.compile(
    r'<meta\s+[^>]*name=["\']keywords["\'][^>]*content=["\']([^"\']*)["\']', re.I
)
_KEYWORDS_META_REV_RE = re.compile(
    r'<meta\s+[^>]*content=["\']([^"\']*)["\'][^>]*name=["\']keywords["\']', re.I
)
# Soft hyphen / zero-width chars sometimes injected into breadcrumb labels
# for line-break hints (e.g. `Street&shy;wear`) — invisible in a rendered
# page but ugly verbatim in an exported XLSX cell (task-57 §1.2).
_INVISIBLE_RE = re.compile("[­​‌‍﻿]")


@dataclass(frozen=True)
class BreadcrumbNode:
    name: str
    url: str | None


def _clean_label(raw: str) -> str:
    text = html.unescape(raw or "")
    text = _INVISIBLE_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _breadcrumbs_from_ld_json(html_text: str) -> list[BreadcrumbNode] | None:
    for match in _LD_JSON_RE.finditer(html_text):
        raw = html.unescape(match.group(1))
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        nodes: list[object]
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            nodes = data["@graph"]
        elif isinstance(data, list):
            nodes = data
        elif isinstance(data, dict):
            nodes = [data]
        else:
            continue
        for node in nodes:
            if not isinstance(node, dict) or node.get("@type") != "BreadcrumbList":
                continue
            items = node.get("itemListElement")
            if not isinstance(items, list) or not items:
                continue
            ordered = sorted(
                (item for item in items if isinstance(item, dict)),
                key=lambda item: item.get("position") or 0,
            )
            trail = [
                BreadcrumbNode(
                    name=_clean_label(str(item.get("name") or "")),
                    url=item.get("item") if isinstance(item.get("item"), str) else None,
                )
                for item in ordered
            ]
            trail = [node for node in trail if node.name]
            if trail:
                return trail
    return None


class _BreadcrumbDomParser(HTMLParser):
    """Fallback for sites without JSON-LD: reads visible text inside the
    first element whose `id`/`class` mentions "breadcrumb" (WordPress themes
    almost universally use one of these two conventions)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth: int | None = None
        self._current_depth = 0
        self._parts: list[str] = []
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._current_depth += 1
        if self._depth is not None:
            return
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        blob = f"{attrs_dict.get('id', '')} {attrs_dict.get('class', '')}".lower()
        if "breadcrumb" in blob:
            self._depth = self._current_depth
            self.found = True

    def handle_endtag(self, tag: str) -> None:
        if self._depth is not None and self._current_depth <= self._depth:
            self._depth = None
        self._current_depth = max(0, self._current_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._depth is not None:
            text = data.strip()
            if text:
                self._parts.append(text)

    def trail(self) -> list[BreadcrumbNode]:
        # A themed breadcrumb bar renders separators (">", "/", "»") as their
        # own text nodes — dropped here so they never become a fake crumb.
        names = [p for p in self._parts if p not in {">", "/", "»", "›", "|", "-", "•"}]
        return [BreadcrumbNode(name=_clean_label(n), url=None) for n in names if _clean_label(n)]


def _breadcrumbs_from_dom(html_text: str) -> list[BreadcrumbNode] | None:
    parser = _BreadcrumbDomParser()
    try:
        parser.feed(html_text)
    except Exception:
        return None
    if not parser.found:
        return None
    trail = parser.trail()
    return trail or None


@dataclass(frozen=True)
class ExtractedPage:
    breadcrumbs: tuple[BreadcrumbNode, ...]
    breadcrumb_source: str  # "json_ld" | "dom" | "none"
    keywords: tuple[str, ...]
    keywords_source: str  # "meta" | "none"


def extract_page(html_text: str) -> ExtractedPage:
    trail = _breadcrumbs_from_ld_json(html_text)
    source = "json_ld"
    if trail is None:
        trail = _breadcrumbs_from_dom(html_text)
        source = "dom"
    if trail is None:
        trail = []
        source = "none"

    kw_match = _KEYWORDS_META_RE.search(html_text) or _KEYWORDS_META_REV_RE.search(html_text)
    keywords: tuple[str, ...] = ()
    keywords_source = "none"
    if kw_match:
        raw = html.unescape(kw_match.group(1)).strip()
        if raw:
            keywords = tuple(k.strip() for k in raw.split(",") if k.strip())
            keywords_source = "meta"

    return ExtractedPage(
        breadcrumbs=tuple(trail),
        breadcrumb_source=source,
        keywords=keywords,
        keywords_source=keywords_source,
    )


def page_label(breadcrumbs: tuple[BreadcrumbNode, ...]) -> str:
    """`Pages` column value (task-57 screenshot): the trail without the
    leading "Home" crumb, joined with " > "; the homepage itself (trail of
    exactly ["Home"], or no trail at all) reads as "Home"."""
    tail = [node.name for node in breadcrumbs if node.name.lower() != "home"]
    return " > ".join(tail) if tail else "Home"
