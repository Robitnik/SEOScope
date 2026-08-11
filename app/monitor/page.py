"""HTML signal extraction (task-35 §3.5): stdlib `html.parser` only.

Pulls exactly the signals the severity table cares about (task-35 §3.4):
title/description/meta-robots/canonical/h1/hreflang/JSON-LD types/GA4+GTM
ids/Google Search Console verification tokens — plus normalized visible text
for the second fingerprint tier (`app/monitor/fingerprint.py`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

_GA4_ID_RE = re.compile(r"\bG-[A-Z0-9]{6,10}\b")
_GTM_ID_RE = re.compile(r"\bGTM-[A-Z0-9]{4,8}\b")
_WHITESPACE_RE = re.compile(r"\s+")
_CHARSET_RE = re.compile(rb"charset=[\"']?([a-zA-Z0-9_\-]+)", re.IGNORECASE)

# Tags whose text content is never part of the visible-text fingerprint or
# the analytics-id scan target for `<title>`/`<h1>` capture, but IS still
# scanned for GA4/GTM ids (inline `gtag(...)`/GTM snippets live in <script>).
_SKIP_TEXT_TAGS = frozenset({"script", "style", "noscript", "template"})


@dataclass
class PageSignals:
    title: str | None = None
    description: str | None = None
    meta_robots: str | None = None
    canonical: str | None = None
    h1: str | None = None
    hreflang: list[tuple[str, str]] = field(default_factory=list)
    json_ld_types: list[str] = field(default_factory=list)
    google_site_verifications: list[str] = field(default_factory=list)
    analytics_ids: list[str] = field(default_factory=list)
    word_count: int = 0
    visible_text: str = ""


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self.description: str | None = None
        self.meta_robots_general: str | None = None
        self.meta_robots_googlebot: str | None = None
        self.canonical: str | None = None
        self.h1: str | None = None
        self.hreflang: list[tuple[str, str]] = []
        self.google_site_verifications: list[str] = []
        self.json_ld_blobs: list[str] = []
        self._text_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._in_h1 = False
        self._h1_captured = False
        self._in_ld_json = False
        self._ld_json_buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        if tag in _SKIP_TEXT_TAGS:
            self._skip_depth += 1
            if tag == "script" and attrs_dict.get("type", "").lower() == "application/ld+json":
                self._in_ld_json = True
                self._ld_json_buffer = []
            return
        if tag == "title":
            self._in_title = True
        elif tag == "h1" and not self._h1_captured:
            self._in_h1 = True
        elif tag == "meta":
            name = attrs_dict.get("name", "").lower()
            content = attrs_dict.get("content", "")
            if name == "description" and self.description is None:
                self.description = content
            elif name == "robots":
                self.meta_robots_general = content
            elif name == "googlebot":
                self.meta_robots_googlebot = content
            elif name == "google-site-verification" and content:
                self.google_site_verifications.append(content)
        elif tag == "link":
            rel = attrs_dict.get("rel", "").lower()
            href = attrs_dict.get("href", "")
            if rel == "canonical" and href:
                self.canonical = href
            elif rel == "alternate" and attrs_dict.get("hreflang") and href:
                self.hreflang.append((attrs_dict["hreflang"], href))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TEXT_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            if tag == "script" and self._in_ld_json:
                self.json_ld_blobs.append("".join(self._ld_json_buffer))
                self._in_ld_json = False
            return
        if tag == "title":
            self._in_title = False
        elif tag == "h1" and self._in_h1:
            self._in_h1 = False
            self._h1_captured = True

    def handle_data(self, data: str) -> None:
        if self._in_ld_json:
            self._ld_json_buffer.append(data)
            return
        if self._in_title and self.title is None:
            self.title = data
        if self._in_h1 and not self._h1_captured:
            self.h1 = (self.h1 or "") + data
        if self._skip_depth == 0:
            stripped = data.strip()
            if stripped:
                self._text_parts.append(stripped)

    @property
    def visible_text(self) -> str:
        return " ".join(self._text_parts)

    @property
    def effective_meta_robots(self) -> str | None:
        # A Googlebot-specific directive overrides the general one for our
        # purposes: we care how Google specifically treats the page.
        return self.meta_robots_googlebot or self.meta_robots_general


def normalize_text(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _extract_json_ld_types(blob: str) -> list[str]:
    try:
        data = json.loads(blob)
    except (ValueError, TypeError):
        return []
    found: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            type_value = node.get("@type")
            if isinstance(type_value, str):
                found.append(type_value)
            elif isinstance(type_value, list):
                found.extend(item for item in type_value if isinstance(item, str))
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    return found


def decode_body(raw: bytes, *, content_type: str | None) -> str:
    charset = None
    if content_type:
        header_match = re.search(r"charset=([a-zA-Z0-9_\-]+)", content_type, re.IGNORECASE)
        if header_match:
            charset = header_match.group(1)
    if charset is None:
        body_match = _CHARSET_RE.search(raw[:2048])
        if body_match:
            charset = body_match.group(1).decode("ascii", errors="ignore")
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def extract_signals(html: str) -> PageSignals:
    parser = _PageParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup: keep whatever parsed so far
        pass

    json_ld_types: list[str] = []
    for blob in parser.json_ld_blobs:
        json_ld_types.extend(_extract_json_ld_types(blob))

    analytics_ids = sorted(set(_GA4_ID_RE.findall(html)) | set(_GTM_ID_RE.findall(html)))
    text = normalize_text(parser.visible_text)

    return PageSignals(
        title=normalize_text(parser.title) if parser.title else None,
        description=parser.description or None,
        meta_robots=parser.effective_meta_robots,
        canonical=parser.canonical,
        h1=normalize_text(parser.h1) if parser.h1 else None,
        hreflang=parser.hreflang,
        json_ld_types=sorted(set(json_ld_types)),
        google_site_verifications=sorted(set(parser.google_site_verifications)),
        analytics_ids=analytics_ids,
        word_count=len(text.split()),
        visible_text=text,
    )


def is_indexable(
    *, meta_robots: str | None, x_robots_tag: str | None, status: int | None
) -> bool | None:
    """`None` = unknown (never checked or the request failed) — PLAN §12,
    never confused with "known not indexable".
    """
    if status is None:
        return None
    if not (200 <= status < 300):
        return False
    combined = f"{meta_robots or ''} {x_robots_tag or ''}".lower()
    return "noindex" not in combined
