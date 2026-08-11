"""Sitemap index/urlset parsing (task-35 §3.3/§3.5): pure parsing only.

Fetching and recursing into child sitemaps is `app/monitor/runner.py`'s job
(it needs the guarded fetcher and the URL/time caps) — this module just turns
bytes into a `SitemapDocument`, gzip-transparent, stdlib `xml.etree` only
(task-35 §1.6).
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from xml.etree import ElementTree


@dataclass(frozen=True)
class SitemapEntry:
    loc: str
    lastmod: str | None = None


@dataclass(frozen=True)
class SitemapDocument:
    kind: str  # "index" | "urlset" | "unknown"
    entries: tuple[SitemapEntry, ...]


def maybe_decompress(
    data: bytes, *, url: str | None = None, content_type: str | None = None
) -> bytes:
    is_gzip_magic = data[:2] == b"\x1f\x8b"
    looks_gz = (url or "").lower().endswith(".gz") or "gzip" in (content_type or "").lower()
    if is_gzip_magic or looks_gz:
        try:
            return gzip.decompress(data)
        except OSError:
            return data
    return data


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(data: bytes) -> SitemapDocument:
    try:
        root = ElementTree.fromstring(data)  # noqa: S314 - our own client's sitemap, stdlib-only (task-35 §1.6)
    except ElementTree.ParseError:
        return SitemapDocument(kind="unknown", entries=())

    root_tag = _local_tag(root.tag)
    kind = (
        "index" if root_tag == "sitemapindex" else "urlset" if root_tag == "urlset" else "unknown"
    )
    if kind == "unknown":
        return SitemapDocument(kind="unknown", entries=())

    child_tag = "sitemap" if kind == "index" else "url"
    entries: list[SitemapEntry] = []
    for child in root:
        if _local_tag(child.tag) != child_tag:
            continue
        loc = None
        lastmod = None
        for field in child:
            name = _local_tag(field.tag)
            if name == "loc" and field.text:
                loc = field.text.strip()
            elif name == "lastmod" and field.text:
                lastmod = field.text.strip()
        if loc:
            entries.append(SitemapEntry(loc=loc, lastmod=lastmod))

    return SitemapDocument(kind=kind, entries=tuple(entries))


def diff_url_sets(old: set[str], new: set[str]) -> tuple[set[str], set[str]]:
    """`(added, removed)`."""
    return new - old, old - new
