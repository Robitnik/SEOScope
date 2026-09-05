"""Type-ahead suggestions for the header URL search (task-61 follow-up).

Two stages, matching how an operator actually types a URL:

1. Before a client resolves (the host itself is still incomplete/typed) —
   suggest client HOMEPAGES whose domain or name contains what's typed so
   far, so the very first keystrokes already narrow to a real site.
2. Once the typed text resolves to a real client — suggest that client's
   own pages, sourced from `monitor_pages` **filtered to `in_sitemap`**
   (task-35's own sitemap ingestion), i.e. genuine "суggestions from the
   sitemap" rather than every URL the monitor has ever seen (gone pages,
   discovered-via-link pages, etc.).

Read-only, no persistence — this never becomes a 7th history table (same
reasoning `app/urlinspector/live.py` gives for not persisting the live
check): "recently searched" is the browser's own job (`localStorage` in
`frontend/js/router.js`), this endpoint only ever answers "what pages does
this site have".
"""

from __future__ import annotations

from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Client, MonitorPage
from app.db.repositories.clients import ClientRepository
from app.urlinspector.resolve import parse_input_url_loose, resolve_client_for_url

_MAX_SUGGESTIONS = 8
# Upper bound on how many of a client's sitemap pages get pulled before the
# path-fragment filter runs in Python — generous against the real portfolio
# (busiest client's monitor_pages is in the low thousands, task-35 §0), and
# matching against `urlsplit(url).path` (not the raw URL string) is what a
# SQL `LIKE` can't do: a naive `url ILIKE '%/a%'` also matches "://a" inside
# the scheme/host of every URL on the site (caught in this module's own test).
_CANDIDATE_CAP = 2000


def _domain_suggestions(clients: list[Client], q: str) -> list[str]:
    needle = q.lower().removeprefix("https://").removeprefix("http://").removeprefix("www.")
    if not needle:
        return []
    matches = [
        c for c in clients if needle in (c.domain or "").lower() or needle in (c.name or "").lower()
    ]
    return [f"https://{c.domain}/" for c in matches[:_MAX_SUGGESTIONS]]


def _page_suggestions(session: Session, client: Client, path_fragment: str) -> list[str]:
    stmt = (
        select(MonitorPage.url)
        .where(MonitorPage.client_id == client.id, MonitorPage.in_sitemap.is_(True))
        .order_by(MonitorPage.url)
        .limit(_CANDIDATE_CAP)
    )
    candidates = session.scalars(stmt)

    # Strip the leading "/" before matching — the operator is typing a
    # fragment of the path, not necessarily one that starts right at a
    # slash boundary (caught live: "/locations" typed against a real page
    # path of "/gentlemens-club-locations/..." matched nothing, because the
    # slash in the typed fragment doesn't line up with a slash in the URL).
    needle = path_fragment.strip("/").lower()
    matches = [url for url in candidates if not needle or needle in urlsplit(url).path.lower()]
    return matches[:_MAX_SUGGESTIONS]


def suggest_urls(session: Session, *, q: str) -> list[str]:
    q = (q or "").strip()
    if len(q) < 2:
        return []

    clients = ClientRepository(session).list_all()
    parsed = parse_input_url_loose(q)
    if parsed is None:
        return _domain_suggestions(clients, q)

    resolved = resolve_client_for_url(parsed.url, clients)
    if resolved.client is None:
        return _domain_suggestions(clients, q)

    return _page_suggestions(session, resolved.client, parsed.path)
