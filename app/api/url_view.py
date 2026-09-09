"""URL Inspector shell on a real address (task-88 follow-up), `GET /url/<url>`.

Replaces the operator-facing `#/url?u=…` hash with a server route, so the
address alone identifies the page being inspected: a bookmark, a link pasted
into a chat, an F5, or a browser-history entry no longer depend on the SPA's
own JS resolving a query param client-side first — and the tab finally carries
the inspected URL in its `<title>` instead of the app's generic name.

Same shape and the same three rules as `archive_view.py` (task-63 §1.2),
deliberately — a second, differently-shaped page route would be a second thing
to remember:

1. **The URL sits raw in the path**, not percent-encoded:
   `/url/https://www.example.com/a/b/`. Verified live that both forms survive
   to the handler at the origin **and** through the Cloudflare tunnel, so the
   choice is free — and raw is what this app already writes for
   `/archive/view/...`, so there is one spelling, not two.

2. **A `?` on the incoming request belongs to the INSPECTED url**, never to
   this route. Some clients' real pages are query-string pages (LAXTON's
   `?taxonomy=…&term=…`, called out in `webarchive/urls.py`), and losing that
   would silently inspect a different page. The inspector's own two view
   options (`g` granularity, `w` window) therefore live in the **fragment**
   (`#g=week&w=90d`) — view state, never sent to the server, and impossible to
   confuse with the target's own query.

3. **No server-side render of the page's data.** Everything on `#/url` already
   has exactly one renderer (`url-inspector.js`, driven by the
   `/api/v1/url-inspector*` endpoints). Rendering any of it a second time here
   is the "same data, two renderers" drift PLAN §12 warns about. This route
   personalizes `<title>`/`<link rel=canonical>` and hands back the SAME
   `index.html` every other route serves.

Always `200`: unlike the archive viewer, there is no "not captured" state to
report — any URL is a legitimate thing to ask about, and "no client matches
this host" is a state the page itself already shows honestly.

Deliberately OUTSIDE `/api/v1` — this is a page a human opens, not a JSON
endpoint. `AuthMiddleware`'s default-deny covers it like any other page.
"""

from __future__ import annotations

from html import escape as html_escape

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from app.api import register_router
from app.core.config import get_settings
from app.db.repositories.clients import ClientRepository
from app.db.session import worker_session_scope
from app.urlinspector.resolve import UrlValidationError, parse_input_url, resolve_client_for_url

router = APIRouter(tags=["url-inspector-view"])

_DEFAULT_TITLE_TAG = "<title>Client Pulse — The Yovth</title>"


def _full_requested_url(url_path: str, raw_query: str) -> str:
    """Rule 2 of the module docstring, in one place."""
    return f"{url_path}?{raw_query}" if raw_query else url_path


def _page_title(url: str) -> str:
    """`<path> — <CLIENT> — URL Inspector`, degrading gracefully.

    The client lookup is the same `resolve_client_for_url` the page itself
    uses, so the tab can never name a different client than the page shows.
    One `list_all()` over ~15 clients — the same cost `archive_view.py`
    already pays for its own title.
    """
    label = url
    try:
        parsed = parse_input_url(url)
        from urllib.parse import urlsplit

        parts = urlsplit(parsed)
        label = f"{parts.path or '/'}" if parts.path not in ("", "/") else parts.netloc
    except UrlValidationError:
        return f"{url} — URL Inspector"

    with worker_session_scope() as session:
        resolved = resolve_client_for_url(parsed, ClientRepository(session).list_all())
        client = resolved.client
    if client is None:
        return f"{label} — URL Inspector"
    return f"{label} — {client.name} — URL Inspector"


def _render_shell(*, title: str, canonical: str) -> str:
    settings = get_settings()
    html = (settings.frontend_dir / "index.html").read_text(encoding="utf-8")
    html = html.replace(_DEFAULT_TITLE_TAG, f"<title>{html_escape(title)}</title>", 1)
    return html.replace(
        "</head>", f'    <link rel="canonical" href="{html_escape(canonical)}">\n  </head>', 1
    )


@router.get("/url/{url:path}")
async def url_inspector_shell(url: str, request: Request) -> HTMLResponse:
    full_url = _full_requested_url(url, request.url.query)
    title = await run_in_threadpool(_page_title, full_url)
    return HTMLResponse(
        content=_render_shell(title=title, canonical=f"/url/{full_url}"),
        headers={"Cache-Control": "no-cache, max-age=0, must-revalidate"},
    )


register_router(router)
