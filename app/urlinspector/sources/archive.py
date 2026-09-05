"""Web Archive source for the URL Inspector (task-61 §2.3).

Uses only `WebArchiveRepository`'s existing public methods — no changes to
`app/webarchive/**` or `app/db/repositories/web_archive.py` (owned by
task-59/60, actively worked on in parallel). `archive_urls.url_key` is
`app.webarchive.urls.normalize_url(url)` with no trailing-slash folding, so
this tries the (at most two) candidate keys — with and without a trailing
slash — via the repository's own `find_url`, rather than adding a
variant-lookup method to that module.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.repositories.web_archive import WebArchiveRepository
from app.schemas.url_inspector import UrlArchiveDTO, UrlArchiveSnapshotDTO, UrlSourceStateDTO
from app.webarchive.urls import normalize_url

_SNAPSHOTS_LIMIT = 10


def _candidate_keys(url: str) -> list[str]:
    key = normalize_url(url)
    host, _, rest = key.partition("/")
    path_and_query = f"/{rest}"
    path, _, query = path_and_query.partition("?")
    alt_path = path[:-1] if path.endswith("/") and len(path) > 1 else f"{path}/"
    alt = f"{host}{alt_path}" + (f"?{query}" if query else "")
    return [key] if alt == key else [key, alt]


def collect_archive(session: Session, *, client_id: str, url: str) -> UrlArchiveDTO:
    repo = WebArchiveRepository(session)
    settings = repo.get_settings(client_id)
    if settings is None or not settings.enabled:
        state = UrlSourceStateDTO(
            source="archive",
            state="not_configured",
            detail="Web Archive isn't enabled for this client.",
        )
        return UrlArchiveDTO(state=state)

    archive_url = None
    for key in _candidate_keys(url):
        archive_url = repo.find_url(client_id, key)
        if archive_url is not None:
            break

    if archive_url is None:
        state = UrlSourceStateDTO(
            source="archive", state="no_data", detail="This URL has no archived snapshots."
        )
        return UrlArchiveDTO(state=state)

    snapshots = repo.list_snapshots_for_url(archive_url.id)
    dtos = [
        UrlArchiveSnapshotDTO(
            id=s.id,
            observed_at=s.observed_at,
            http_status=s.http_status,
            final_url=s.final_url,
            store_reason=s.store_reason,
            title=s.title,
            has_change=repo.has_change_for_snapshot(s.id),
        )
        for s in snapshots[:_SNAPSHOTS_LIMIT]
    ]
    return UrlArchiveDTO(
        state=UrlSourceStateDTO(source="archive", state="has_data"),
        archive_url_id=archive_url.id,
        snapshots=dtos,
        snapshot_count=len(snapshots),
    )
