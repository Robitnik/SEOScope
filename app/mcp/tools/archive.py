"""Група B — Веб-архів (план §5): «як ця сторінка виглядала тоді».

`compare_url_versions` — ключовий tool плану: закриває сценарій «коли ми
переписали цю сторінку і що змінилось» за один виклик замість відкривання
архіву, перебору дат і звірки двох версій очима.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.mcp.apiclient import get_api_client
from app.mcp.errors import ApiError
from app.mcp.models import (
    ArchiveChangeItem,
    ArchiveChangesFeed,
    ArchivedUrlMatch,
    ArchivedUrlSearchResult,
    SnapshotSummary,
    UrlSnapshotTimeline,
    UrlVersionComparison,
)
from app.mcp.resolve import resolve_client
from app.mcp.server import mcp


@mcp.tool()
async def find_archived_url(
    query: str,
    client: str | None = None,
) -> ArchivedUrlSearchResult:
    """Search the web archive for a URL by (part of) its address.

    Returns each match's `url_id` — pass that to `get_url_snapshot_timeline`
    or `compare_url_versions` next.

    Args:
        query: Part of the URL to search for, e.g. "/pricing" or
            "example.com/blog".
        client: Optionally narrow the search to one client's archive
            (name or domain). Omit to search across every client.
    """
    api = get_api_client()
    params: dict[str, Any] = {"q": query, "page_size": 20}
    if client:
        resolved = await resolve_client(api, client)
        params["client_id"] = resolved.client_id

    result = await api.get("/api/v1/archive/urls", params=params)
    matches = [
        ArchivedUrlMatch(
            url_id=item["id"],
            client_name=item["client_name"],
            url=item["url"],
            snapshot_count=item["snapshot_count"],
            last_captured_at=item.get("last_captured_at"),
        )
        for item in result.get("items", [])
    ]
    return ArchivedUrlSearchResult(query=query, matches=matches)


@mcp.tool()
async def get_url_snapshot_timeline(url_id: str) -> UrlSnapshotTimeline:
    """Get every archived snapshot of one URL, newest first: dates,
    HTTP status, and whether each snapshot changed from the one before it.

    Args:
        url_id: A `url_id` from `find_archived_url`.
    """
    api = get_api_client()
    result = await api.get(f"/api/v1/archive/urls/{url_id}/snapshots")
    return UrlSnapshotTimeline(
        url=result["url"]["url"],
        client_name=result["url"]["client_name"],
        snapshots=[
            SnapshotSummary(
                snapshot_id=s["id"],
                observed_at=s["observed_at"],
                http_status=s.get("http_status"),
                title=s.get("title"),
                has_change=s["has_change"],
            )
            for s in result.get("snapshots", [])
        ],
    )


def _parse_date(value: str) -> date:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ApiError(
            f'"{value}" is not a valid date. Use ISO format, e.g. "2026-08-15".'
        ) from exc


def _pick_target(snapshots: list[dict[str, Any]], target_date: str | None) -> dict[str, Any]:
    if not snapshots:
        raise ApiError("This URL has no snapshots in the archive yet.")
    if target_date is None:
        return snapshots[0]  # newest first
    wanted = _parse_date(target_date)
    return min(
        snapshots,
        key=lambda s: abs((_parse_date(s["observed_at"]) - wanted).days),
    )


@mcp.tool()
async def compare_url_versions(
    url_id: str,
    target_date: str | None = None,
) -> UrlVersionComparison:
    """Diff a URL's archived content against its immediately preceding
    snapshot — the answer to "what changed on this page and when".

    This compares CONSECUTIVE snapshots, not two arbitrary dates: pick the
    snapshot closest to `target_date` (or the newest one, if omitted), and
    it is diffed against whichever snapshot came right before it. The
    result's `from_date`/`to_date` say exactly which two versions were
    actually compared — check them if you asked for a specific date.

    Args:
        url_id: A `url_id` from `find_archived_url`.
        target_date: The date of the version you want to inspect (ISO
            format, e.g. "2026-08-15"). Omit to compare the two most
            recent snapshots.
    """
    api = get_api_client()
    timeline = await api.get(f"/api/v1/archive/urls/{url_id}/snapshots")
    snapshots = timeline.get("snapshots", [])
    target = _pick_target(snapshots, target_date)

    if target is snapshots[-1]:
        available = ", ".join(s["observed_at"][:10] for s in reversed(snapshots[-5:]))
        raise ApiError(
            f"Snapshot from {target['observed_at'][:10]} is the oldest one archived for this "
            f"URL — there is nothing before it to diff against. Earliest available dates: "
            f"{available}."
        )

    diff = await api.get(f"/api/v1/archive/snapshots/{target['id']}/diff")
    change = diff["change"]
    by_id = {s["id"]: s["observed_at"] for s in snapshots}
    return UrlVersionComparison(
        url=timeline["url"]["url"],
        from_date=by_id.get(change["prev_snapshot_id"], "unknown")[:10],
        to_date=by_id.get(change["snapshot_id"], target["observed_at"])[:10],
        change_kinds=change.get("change_kinds", []),
        text_delta_words=change.get("text_delta_words", 0),
        diff_lines=diff.get("diff_lines", []),
    )


@mcp.tool()
async def list_archive_changes(
    client: str | None = None,
    days: int = 30,
    kind: str | None = None,
) -> ArchiveChangesFeed:
    """List recent changes detected across the web archive — "what has
    moved" without already knowing which URL to look at.

    Args:
        client: Optionally narrow to one client (name or domain). Omit
            for every client.
        days: How many days back to look.
        kind: Optionally filter by change type (e.g. "content", "title").
            Omit for every kind.
    """
    api = get_api_client()
    params: dict[str, Any] = {"days": days, "page_size": 50}
    if client:
        resolved = await resolve_client(api, client)
        params["client_id"] = resolved.client_id
    if kind:
        params["kind"] = kind

    result = await api.get("/api/v1/archive/changes", params=params)
    items = [
        ArchiveChangeItem(
            client_name=c["client_name"],
            url=c["url"],
            detected_at=c["detected_at"],
            change_kinds=c.get("change_kinds", []),
            text_delta_words=c.get("text_delta_words", 0),
        )
        for c in result.get("items", [])
    ]
    return ArchiveChangesFeed(total=result.get("total", len(items)), items=items)
