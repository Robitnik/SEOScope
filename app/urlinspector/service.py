"""URL Inspector orchestrator (task-61 §2.6) — the one entry point
`app/api/url_inspector.py` calls.

Every source is wrapped so an exception in ONE panel becomes that panel's
own `failed` state instead of a 500 for the whole page (PLAN §9.1's cell
isolation, applied to a read path instead of a collection lane) — a live
antibot block or a malformed stored row in one table must never hide the
five other panels that are fine.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.repositories.clients import ClientRepository
from app.schemas.url_inspector import (
    Granularity,
    ResolvedClientDTO,
    UrlArchiveDTO,
    UrlInspectorDTO,
    UrlKeywordsDTO,
    UrlMetaDTO,
    UrlMonitorDTO,
    UrlScreenshotsStateDTO,
    UrlSearchAnalyticsDTO,
    UrlSourceStateDTO,
    UrlTechAuditDTO,
)
from app.urlinspector.resolve import resolve_client_for_url
from app.urlinspector.sources.archive import collect_archive
from app.urlinspector.sources.ga4 import collect_ga4
from app.urlinspector.sources.gsc import collect_gsc
from app.urlinspector.sources.inventory import collect_meta
from app.urlinspector.sources.keywords import collect_keywords
from app.urlinspector.sources.monitor import collect_monitor
from app.urlinspector.sources.screenshots import collect_screenshots
from app.urlinspector.sources.techaudit import collect_tech_audit
from app.urlinspector.urlkey import inspector_key

logger = logging.getLogger(__name__)


def _failed_state(source: str, exc: Exception) -> UrlSourceStateDTO:
    logger.exception("url-inspector: %s source failed", source)
    return UrlSourceStateDTO(source=source, state="failed", detail=f"{type(exc).__name__}: {exc}")


def inspect_url(
    session: Session, *, url: str, granularity: Granularity = "week"
) -> UrlInspectorDTO:
    clients = ClientRepository(session).list_all()
    resolved = resolve_client_for_url(url, clients)
    generated_at = datetime.now(UTC)

    if resolved.client is None:
        unknown_state = UrlSourceStateDTO(
            source="client", state="unknown_client", detail="No client matches this URL's host."
        )
        return UrlInspectorDTO(
            input_url=url,
            inspector_key=inspector_key(url),
            client=None,
            client_match="unknown_domain",
            coverage=[unknown_state],
            search_analytics=UrlSearchAnalyticsDTO(
                gsc=unknown_state, reporting_ninja=unknown_state
            ),
            archive=_empty_archive(unknown_state),
            monitor=_empty_monitor(unknown_state),
            meta=_empty_meta(unknown_state),
            tech_audit=_empty_tech_audit(unknown_state),
            keywords=_empty_keywords(unknown_state),
            screenshots=UrlScreenshotsStateDTO(state=unknown_state),
            generated_at=generated_at,
        )

    client = resolved.client
    client_id = client.id

    try:
        gsc_state, gsc_series = collect_gsc(
            session, client_id=client_id, url=url, granularity=granularity
        )
    except Exception as exc:  # noqa: BLE001 — isolated per source, see module docstring
        gsc_state, gsc_series = _failed_state("gsc", exc), {}

    try:
        ga4_state, ga4_series = collect_ga4(
            session, client_id=client_id, url=url, granularity=granularity
        )
    except Exception as exc:  # noqa: BLE001
        ga4_state, ga4_series = _failed_state("reporting_ninja", exc), {}

    try:
        archive = collect_archive(session, client_id=client_id, url=url)
    except Exception as exc:  # noqa: BLE001
        archive = _empty_archive(_failed_state("archive", exc))

    try:
        monitor = collect_monitor(session, client_id=client_id, url=url)
    except Exception as exc:  # noqa: BLE001
        monitor = _empty_monitor(_failed_state("monitor", exc))

    try:
        meta = collect_meta(session, client_id=client_id, url=url)
    except Exception as exc:  # noqa: BLE001
        meta = _empty_meta(_failed_state("page_inventory", exc))

    try:
        tech_audit = collect_tech_audit(session, client_id=client_id, url=url)
    except Exception as exc:  # noqa: BLE001
        tech_audit = _empty_tech_audit(_failed_state("tech_audit", exc))

    try:
        keywords = collect_keywords(
            session, client_id=client_id, url=url, meta_keywords=meta.keywords
        )
    except Exception as exc:  # noqa: BLE001
        keywords = _empty_keywords(_failed_state("keywords", exc))

    try:
        screenshots = collect_screenshots(session, client_id=client_id, url=url)
    except Exception as exc:  # noqa: BLE001
        screenshots = UrlScreenshotsStateDTO(state=_failed_state("screenshots", exc))

    coverage = [
        gsc_state,
        ga4_state,
        monitor.state,
        archive.state,
        meta.state,
        tech_audit.state,
        keywords.state,
        keywords.queries_state,
        screenshots.state,
    ]

    return UrlInspectorDTO(
        input_url=url,
        inspector_key=inspector_key(url),
        client=ResolvedClientDTO(
            id=client.id,
            name=client.name,
            slug=client.slug,
            domain=client.domain,
            # `client.logo_path` is a raw storage-relative path (e.g.
            # `logos/<id>.png`), never servable directly — every other DTO
            # that carries a logo (`app/api/dashboard.py`, `app/api/clients.py`)
            # rewrites it to this same served route; caught live (404) when
            # this one didn't.
            logo_path=(f"/api/v1/clients/{client.id}/logo" if client.logo_path else None),
            match=resolved.match,  # type: ignore[arg-type]
        ),
        client_match=resolved.match,  # type: ignore[arg-type]
        coverage=coverage,
        search_analytics=UrlSearchAnalyticsDTO(
            gsc=gsc_state, reporting_ninja=ga4_state, gsc_series=gsc_series, ga4_series=ga4_series
        ),
        archive=archive,
        monitor=monitor,
        meta=meta,
        tech_audit=tech_audit,
        keywords=keywords,
        screenshots=screenshots,
        generated_at=generated_at,
    )


def _empty_archive(state: UrlSourceStateDTO) -> UrlArchiveDTO:
    return UrlArchiveDTO(state=state)


def _empty_monitor(state: UrlSourceStateDTO) -> UrlMonitorDTO:
    return UrlMonitorDTO(state=state)


def _empty_meta(state: UrlSourceStateDTO) -> UrlMetaDTO:
    return UrlMetaDTO(state=state)


def _empty_tech_audit(state: UrlSourceStateDTO) -> UrlTechAuditDTO:
    return UrlTechAuditDTO(state=state)


def _empty_keywords(state: UrlSourceStateDTO) -> UrlKeywordsDTO:
    return UrlKeywordsDTO(state=state, queries_state=state)
