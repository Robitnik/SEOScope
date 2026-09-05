"""`monitor_pages` + stored HTML blobs -> `InventoryRow` (task-57 §1.5/§1.6).

Read-only consumer of `app.monitor.storage`/`app.db.repositories.monitor` —
never writes to `monitor_*` tables and never touches `build_signals()`
(task-57 §2.2: a new signal field would change every page's
`signals_sha256` and fire thousands of false `page_changed` alerts).
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MonitorPage, MonitorPageVersion
from app.monitor.storage import resolve_blob_path
from app.pageinventory.extract import ExtractedPage, extract_page, page_label

PageStatusLabel = str  # "live" | "broken" | "redirect" | "noindex" | "not_checked"


@dataclass(frozen=True)
class InventoryRow:
    """One row of the inventory — one canonical URL. `title`/`description`
    follow the model.py convention used across the codebase for a page that
    HAS been checked: `""` means the tag is present-but-empty OR absent from
    a fetched page (both are real defects on a live site — task-57 §1.6,
    the deliberate inversion of `app.contentqa.model.ContentItem`'s "`None`
    = field not in source" rule, which is about hand-authored documents, not
    crawled pages). `None` is reserved for "this page was never checked" —
    `status IS NULL` — where no field has an opinion at all.
    """

    url: str
    canonical_key: str
    duplicate_urls: tuple[str, ...]
    page_label: str
    breadcrumbs: tuple[tuple[str, str | None], ...]  # (name, url)
    breadcrumb_source: str
    title: str | None
    description: str | None
    keywords: tuple[str, ...]
    keywords_source: str
    h1: str | None
    status: int | None
    status_label: PageStatusLabel
    indexable: bool | None
    canonical_missing: bool
    checked: bool  # False only for `status IS NULL` — never checked yet


@dataclass(frozen=True)
class InventoryBuildResult:
    rows: tuple[InventoryRow, ...]
    pages_total: int  # rows in `monitor_pages` before dedup
    pages_unique: int
    pages_with_breadcrumbs: int
    pages_with_keywords: int
    breadcrumb_source_summary: str  # "json_ld" | "dom" | "mixed" | "none"
    not_checked_count: int


def _normalize(url: str) -> str:
    return url.rstrip("/")


def _label_from_url(url: str) -> str:
    """Fallback `page_label` for a row with NO real breadcrumb trail — used
    when the monitor never stored a body for this page at all (no blob to
    extract from — live gap: some clients have 0% blob coverage because
    their pages were first checked before `monitor_store_page_bodies`
    reliably captured the first-ever version, and an unchanged page never
    gets a body afterwards either) or when a stored body's HTML simply has
    no breadcrumb markup.

    `""`/root path -> "Home" (a fact derivable from the URL alone, not a
    fabrication). Anything else -> the last path segment, humanized. Never
    reuses `extract.page_label()`'s "Home" default for an EMPTY breadcrumb
    tuple — that default conflates "this trail IS just Home" with "there is
    no trail at all", which is exactly the bug that made every row of a
    zero-blob-coverage client render as "Home" (task-57, live report
    2026-09-04, client GOREPORT: 228/228 pages, 0 stored bodies)."""
    path = urlsplit(url).path.strip("/")
    if not path:
        return "Home"
    segment = path.rsplit("/", 1)[-1]
    segment = unquote(segment).replace("-", " ").replace("_", " ").strip()
    if not segment:
        return "Home"
    # Title-cased per word (slugs are lowercase-with-hyphens almost always —
    # this mirrors how the real breadcrumb labels in stored HTML are cased,
    # e.g. "Vici Printed Sweat Shorts"), first letter of each word only, so
    # an existing acronym in the slug isn't lowercased.
    return " ".join(word[:1].upper() + word[1:] for word in segment.split(" ") if word)


def _status_label(page: MonitorPage) -> PageStatusLabel:
    if page.status is None:
        return "not_checked"
    if page.status in (301, 302, 303, 307, 308):
        return "redirect"
    if page.status >= 400:
        return "broken"
    if page.meta_robots and "noindex" in page.meta_robots.lower():
        return "noindex"
    if page.indexable is False:
        return "noindex"
    return "live"


def _latest_blob_sha(session: Session, page_id: str) -> str | None:
    stmt = (
        select(MonitorPageVersion.body_blob_sha256)
        .where(
            MonitorPageVersion.page_id == page_id,
            MonitorPageVersion.body_blob_sha256.is_not(None),
        )
        .order_by(MonitorPageVersion.observed_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def _read_blob_text(artifacts_dir: Path, sha256: str) -> str | None:
    try:
        raw = resolve_blob_path(artifacts_dir, sha256).read_bytes()
    except (FileNotFoundError, PermissionError):
        return None
    try:
        return gzip.decompress(raw).decode("utf-8", errors="replace")
    except OSError:
        return None


def build_inventory(
    session: Session, client_id: str, *, artifacts_dir: Path
) -> InventoryBuildResult:
    pages = list(
        session.scalars(select(MonitorPage).where(MonitorPage.client_id == client_id))
    )
    pages_total = len(pages)

    # Dedup by canonical (task-57 §1.5): first-seen wins the row, later
    # duplicates of the same canonical only contribute their URL to
    # `duplicate_urls`. `canonical` empty/missing -> the page's own
    # (normalized) URL is its own key, and the row is flagged.
    order: list[str] = []
    grouped: dict[str, list[MonitorPage]] = {}
    for page in sorted(pages, key=lambda p: p.url):
        key = _normalize(page.canonical) if page.canonical else _normalize(page.url)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(page)

    rows: list[InventoryRow] = []
    bc_sources: set[str] = set()
    with_breadcrumbs = 0
    with_keywords = 0
    not_checked = 0

    for key in order:
        members = grouped[key]
        primary = members[0]
        duplicates = tuple(m.url for m in members[1:])

        checked = primary.status is not None
        if not checked:
            not_checked += 1
            rows.append(
                InventoryRow(
                    url=primary.url,
                    canonical_key=key,
                    duplicate_urls=duplicates,
                    page_label=_label_from_url(primary.url),
                    breadcrumbs=(),
                    breadcrumb_source="none",
                    title=None,
                    description=None,
                    keywords=(),
                    keywords_source="none",
                    h1=None,
                    status=None,
                    status_label="not_checked",
                    indexable=None,
                    canonical_missing=not bool(primary.canonical),
                    checked=False,
                )
            )
            continue

        extracted: ExtractedPage | None = None
        blob_sha = _latest_blob_sha(session, primary.id)
        if blob_sha:
            html_text = _read_blob_text(artifacts_dir, blob_sha)
            if html_text is not None:
                extracted = extract_page(html_text)

        breadcrumbs: tuple[tuple[str, str | None], ...] = ()
        bc_source = "none"
        keywords: tuple[str, ...] = ()
        kw_source = "none"
        if extracted is not None:
            breadcrumbs = tuple((n.name, n.url) for n in extracted.breadcrumbs)
            bc_source = extracted.breadcrumb_source
            keywords = extracted.keywords
            kw_source = extracted.keywords_source
        # `page_label()` only ever sees a REAL trail here — an empty one
        # (no blob stored at all, or a stored page with no breadcrumb
        # markup) falls back to a URL-derived label instead, never to
        # `page_label`'s own "Home" default for an empty tuple (that
        # default is only correct for a trail that IS just `["Home"]`).
        label = (
            page_label(extracted.breadcrumbs)
            if extracted is not None and extracted.breadcrumbs
            else _label_from_url(primary.url)
        )
        bc_sources.add(bc_source)
        if breadcrumbs:
            with_breadcrumbs += 1
        if keywords:
            with_keywords += 1

        rows.append(
            InventoryRow(
                url=primary.url,
                canonical_key=key,
                duplicate_urls=duplicates,
                page_label=label,
                breadcrumbs=breadcrumbs,
                breadcrumb_source=bc_source,
                # `""` for a fetched-but-missing tag, never `None` (task-57 §1.6).
                title=primary.title if primary.title is not None else "",
                description=primary.description if primary.description is not None else "",
                keywords=keywords,
                keywords_source=kw_source,
                h1=primary.h1,
                status=primary.status,
                status_label=_status_label(primary),
                indexable=primary.indexable,
                canonical_missing=not bool(primary.canonical),
                checked=True,
            )
        )

    if not bc_sources or bc_sources == {"none"}:
        summary = "none"
    elif len(bc_sources - {"none"}) == 1:
        summary = next(iter(bc_sources - {"none"}))
    else:
        summary = "mixed"

    return InventoryBuildResult(
        rows=tuple(rows),
        pages_total=pages_total,
        pages_unique=len(rows),
        pages_with_breadcrumbs=with_breadcrumbs,
        pages_with_keywords=with_keywords,
        breadcrumb_source_summary=summary,
        not_checked_count=not_checked,
    )
