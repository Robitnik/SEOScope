"""Web Archive replay-viewer shell (task-63 §1.2), `GET
/archive/view/snapshot/<id>/<ts>/<url>`.

Replaces the operator-facing `#/archive/view?snapshot=...&url=...` hash
with a real server route: the URL alone now identifies the resource, so a
direct visit, a bookmark, or an F5 no longer depends on the SPA's own JS
resolving query params client-side first. The old hash form still works
(`router.js` redirects it here) — only NEW links use this shape.

Deliberately NOT a full server-side render of the viewer's own banner: that
DTO (`ArchiveSnapshotDetailDTO`) already has exactly one renderer
(`archive-viewer.js`, driven by `GET /api/v1/archive/snapshots/{id}`).
Building a second renderer here would be the exact "same data, two
renderers" drift PLAN §12 warns against — a future field added to one
would silently not show up in the other. This route only personalizes the
shell's `<title>`/`<link rel=canonical>` (one lightweight read), then hands
back the SAME `index.html` every other route serves; the SPA's own router
(matched against `location.pathname` for this one path shape) takes it
from there exactly as it does today for the hash form.

Deliberately OUTSIDE `/api/v1`, same as `archive_replay.py` — this is a
page a human opens directly, not a JSON endpoint.
"""

from __future__ import annotations

from html import escape as html_escape

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from app.api import register_router
from app.core.config import get_settings
from app.db.repositories.web_archive import WebArchiveRepository
from app.db.session import worker_session_scope
from app.webarchive.replay import format_ts

router = APIRouter(tags=["web-archive-view"])

_DEFAULT_TITLE_TAG = "<title>Client Pulse — The Yovth</title>"


def _full_requested_url(url_path: str, raw_query: str) -> str:
    """Same reassembly `archive_replay.py`'s own replay route needs: a `?`
    on the incoming request belongs to the ORIGINAL archived URL (some,
    e.g. LAXTON's `?taxonomy=...&term=123`, are real distinct pages), never
    to this route itself — this route never takes query params of its own.
    """
    return f"{url_path}?{raw_query}" if raw_query else url_path


def _snapshot_title_and_canonical(
    snapshot_id: str, ts: str, url: str
) -> tuple[str | None, str | None, int]:
    with worker_session_scope() as session:
        repo = WebArchiveRepository(session)
        snapshot = repo.get_snapshot(snapshot_id)
        if snapshot is None:
            return None, None, 404
        archive_url = repo.get_url(snapshot.archive_url_id)
        page_url = archive_url.url if archive_url is not None else url
        title = f"{page_url} — {format_ts(snapshot.observed_at)} — Web Archive"
        canonical = f"/archive/view/snapshot/{snapshot_id}/{ts}/{page_url}"
        return title, canonical, 200


def _render_shell(*, title: str | None, canonical: str | None) -> str:
    settings = get_settings()
    html = (settings.frontend_dir / "index.html").read_text(encoding="utf-8")
    if title:
        html = html.replace(_DEFAULT_TITLE_TAG, f"<title>{html_escape(title)}</title>", 1)
    if canonical:
        html = html.replace(
            "</head>", f'    <link rel="canonical" href="{html_escape(canonical)}">\n  </head>', 1
        )
    return html


@router.get("/archive/view/snapshot/{snapshot_id}/{ts}/{url:path}")
async def archive_view_shell(snapshot_id: str, ts: str, url: str, request: Request) -> HTMLResponse:
    full_url = _full_requested_url(url, request.url.query)
    title, canonical, status_code = await run_in_threadpool(
        _snapshot_title_and_canonical, snapshot_id, ts, full_url
    )
    html = _render_shell(title=title, canonical=canonical)
    return HTMLResponse(
        content=html,
        status_code=status_code,
        headers={"Cache-Control": "no-cache, max-age=0, must-revalidate"},
    )


register_router(router)
