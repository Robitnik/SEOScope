"""Група C — Site Monitor (план §5): «що змінилось на сайті і чи це погано».

`list_site_changes` закриває сценарій «що взагалі мінялось за тиждень» без
перегляду стрічки на 200 подій очима; `diff_monitored_page` — поглиблення
в конкретну зміну зі стрічки.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.mcp.apiclient import ApiClient, get_api_client
from app.mcp.errors import ApiError
from app.mcp.models import PageDiff, SiteChangeItem, SiteChangesFeed
from app.mcp.resolve import resolve_client
from app.mcp.server import mcp

_MAX_ITEMS = 50


def _within_days(detected_at: str, days: int) -> bool:
    detected = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
    return detected >= datetime.now(UTC) - timedelta(days=days)


async def _client_names(api: Any) -> dict[str, str]:
    # `MonitorChangeDTO` carries `client_id` but no `client_name` (unlike
    # the archive's change feed) — needed only for the portfolio-wide case
    # below, where a change without its client's name attached is useless.
    rows = await api.get("/api/v1/clients", params={"limit": 500})
    return {row["id"]: row["name"] for row in rows}


@mcp.tool()
async def list_site_changes(
    client: str | None = None,
    days: int = 7,
    severity: str | None = None,
) -> SiteChangesFeed:
    """List site changes the monitor detected: page content, robots.txt,
    sitemap, and DNS — the answer to "what changed on this site recently".

    Returns at most 50 items, newest first; `total` is the true count
    after filtering, so you can tell "50 shown, 50 total" from "50 shown,
    340 total" — the latter means a busy client, ask for a shorter
    `days` window or a specific `severity` instead of assuming completeness.

    Args:
        client: Optionally narrow to one client (name or domain). Omit
            for every client.
        days: How many days back to look.
        severity: Optionally filter by "critical", "warning", or "info".
            Omit for every severity.
    """
    api = get_api_client()
    params: dict[str, Any] = {"state": "open"}
    if severity:
        params["severity"] = severity

    if client:
        resolved = await resolve_client(api, client)
        rows = await api.get(
            f"/api/v1/clients/{resolved.client_id}/monitor/changes", params=params
        )
        names = {resolved.client_id: resolved.name}
    else:
        params["limit"] = 200
        rows = await api.get("/api/v1/monitor/changes", params=params)
        names = await _client_names(api)

    # No server-side date filter exists on either endpoint (both cap by
    # `limit`/newest-first, not by age) — filtered here against `detected_at`.
    # The CLIENT-scoped endpoint has no `limit` at all — a busy client with
    # hundreds of open changes would otherwise dump every one of them into
    # the model's context (план §7.2: "обрізати агресивно"). `total` stays
    # the true post-filter count; only `items` is capped, newest first.
    recent = sorted(
        (r for r in rows if _within_days(r["detected_at"], days)),
        key=lambda r: r["detected_at"],
        reverse=True,
    )
    items = [
        SiteChangeItem(
            client_name=names.get(r["client_id"], "?"),
            severity=r["severity"],
            change_type=r["change_type"],
            subject=r["subject"],
            old_value=r.get("old_value"),
            new_value=r.get("new_value"),
            detected_at=r["detected_at"],
        )
        for r in recent[:_MAX_ITEMS]
    ]
    return SiteChangesFeed(total=len(recent), items=items)


async def _resolve_page_id(api: ApiClient, client_id: str, page: str) -> str:
    result = await api.get(
        f"/api/v1/clients/{client_id}/monitor/pages", params={"q": page, "limit": 20}
    )
    pages = result.get("pages", [])
    matches = [p for p in pages if page.casefold() in p["url"].casefold()]
    if len(matches) == 1:
        return str(matches[0]["id"])
    if not matches:
        raise ApiError(f'No monitored page matches "{page}" for this client.')
    urls = ", ".join(m["url"] for m in matches[:10])
    raise ApiError(f'"{page}" matches multiple monitored pages: {urls}. Be more specific.')


@mcp.tool()
async def diff_monitored_page(
    client: str,
    page: str,
) -> PageDiff:
    """Diff a monitored page's most recent version against the one before
    it: which fields changed, plus a text diff of the page body.

    Args:
        client: The client's name or domain.
        page: The page URL, or a distinctive fragment of it (e.g.
            "/pricing").
    """
    api = get_api_client()
    resolved = await resolve_client(api, client)
    page_id = await _resolve_page_id(api, resolved.client_id, page)
    diff = await api.get(
        f"/api/v1/clients/{resolved.client_id}/monitor/pages/{page_id}/diff"
    )
    return PageDiff(
        page_url=page,
        from_version_id=diff["from_version_id"],
        to_version_id=diff["to_version_id"],
        changed_fields=diff.get("changed_fields", []),
        text_diff=diff.get("text_diff", []),
    )
