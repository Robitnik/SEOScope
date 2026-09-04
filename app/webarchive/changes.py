"""Change detection for the Web Archive (task-59 §5): a field-level and
text-level diff between two consecutive snapshots of the same URL.

Computed ONCE, at capture time, while both HTML bodies are already in
memory — re-deriving it later from stored blobs would cost the exact same
work for no benefit. `app.monitor.page.extract_signals` (read-only import,
same module Site Monitor itself uses) supplies title/description/h1/
meta_robots/canonical/normalized visible text; this module adds link/image
set diffing and turns all of it into the compact summary
`app.db.models.ArchiveChange` stores.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from app.monitor.page import extract_signals
from app.webarchive.urls import resolve

_ANCHOR_HREF_RE = re.compile(r"""<a[^>]+href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_IMG_SRC_RE = re.compile(r"""<img[^>]+src\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

# Cap how many added/removed URLs a single change row carries inline — the
# exact counts are always accurate; only the EXAMPLE list is capped, same
# aggregate-with-examples shape `MonitorChange.details_json` already uses
# for `sitemap_broken_urls` (task-35 §3.4.1).
_MAX_EXAMPLES = 20


def extract_links(html: str, base_url: str) -> set[str]:
    found: set[str] = set()
    for raw in _ANCHOR_HREF_RE.findall(html):
        absolute = resolve(base_url, raw)
        if absolute is not None:
            found.add(absolute)
    return found


def extract_images(html: str, base_url: str) -> set[str]:
    found: set[str] = set()
    for raw in _IMG_SRC_RE.findall(html):
        absolute = resolve(base_url, raw)
        if absolute is not None:
            found.add(absolute)
    return found


@dataclass(frozen=True)
class SnapshotFields:
    http_status: int | None
    final_url: str | None
    title: str | None
    description: str | None
    h1: str | None
    canonical: str | None
    meta_robots: str | None
    visible_text: str
    links: frozenset[str]
    images: frozenset[str]


def snapshot_fields(
    html: str, *, base_url: str, http_status: int | None, final_url: str | None
) -> SnapshotFields:
    signals = extract_signals(html)
    return SnapshotFields(
        http_status=http_status,
        final_url=final_url,
        title=signals.title,
        description=signals.description,
        h1=signals.h1,
        canonical=signals.canonical,
        meta_robots=signals.meta_robots,
        visible_text=signals.visible_text,  # already whitespace-normalized
        links=frozenset(extract_links(html, base_url)),
        images=frozenset(extract_images(html, base_url)),
    )


def _has_noindex(meta_robots: str | None) -> bool:
    return "noindex" in (meta_robots or "").lower()


@dataclass(frozen=True)
class ChangeSummary:
    change_kinds: list[str]
    summary: dict[str, object]
    text_delta_words: int


def diff_fields(old: SnapshotFields, new: SnapshotFields) -> ChangeSummary | None:
    """`None` when nothing an operator would call a change actually
    differs (the raw-byte noise this whole archive is built to ignore,
    task-59 §0.2, lives BELOW this — these fields already come from the
    parsed/normalized signal layer).
    """
    kinds: list[str] = []
    summary: dict[str, object] = {}

    if old.title != new.title:
        kinds.append("title")
        summary["title"] = {"old": old.title, "new": new.title}
    if old.description != new.description:
        kinds.append("description")
        summary["description"] = {"old": old.description, "new": new.description}
    if old.h1 != new.h1:
        kinds.append("h1")
        summary["h1"] = {"old": old.h1, "new": new.h1}
    if old.http_status != new.http_status:
        kinds.append("status")
        summary["status"] = {"old": old.http_status, "new": new.http_status}
    if (old.final_url or None) != (new.final_url or None):
        kinds.append("redirect")
        summary["redirect"] = {"old": old.final_url, "new": new.final_url}
    if _has_noindex(old.meta_robots) != _has_noindex(new.meta_robots):
        kinds.append("noindex")
        summary["noindex"] = {
            "old": _has_noindex(old.meta_robots), "new": _has_noindex(new.meta_robots)
        }

    old_words = old.visible_text.split()
    new_words = new.visible_text.split()
    delta_words = len(new_words) - len(old_words)
    if old.visible_text != new.visible_text:
        kinds.append("text")
        summary["text"] = {
            "delta_words": delta_words, "old_words": len(old_words), "new_words": len(new_words),
        }

    added_links, removed_links = new.links - old.links, old.links - new.links
    if added_links or removed_links:
        kinds.append("links")
        summary["links"] = {
            "added": sorted(added_links)[:_MAX_EXAMPLES],
            "removed": sorted(removed_links)[:_MAX_EXAMPLES],
            "added_count": len(added_links),
            "removed_count": len(removed_links),
        }

    added_images, removed_images = new.images - old.images, old.images - new.images
    if added_images or removed_images:
        kinds.append("images")
        summary["images"] = {
            "added": sorted(added_images)[:_MAX_EXAMPLES],
            "removed": sorted(removed_images)[:_MAX_EXAMPLES],
            "added_count": len(added_images),
            "removed_count": len(removed_images),
        }

    if not kinds:
        return None
    return ChangeSummary(change_kinds=kinds, summary=summary, text_delta_words=delta_words)


def unified_text_diff(old_text: str, new_text: str, *, context: int = 2) -> list[str]:
    """Rendered on demand (the `Compare` view), never stored — the two
    snapshots' own blobs are the source of truth.
    """
    return list(
        difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), lineterm="", n=context)
    )
