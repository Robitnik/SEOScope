"""Site Monitor source for the URL Inspector (task-61 §2.3).

Goes through `MonitorRepository` for every `monitor_*` read (that module's
own docstring: "the only place that touches `monitor_*` tables directly") —
the two variant-lookup methods it needed were added there as an additive,
documented touch (task-61 Completion report, "Contract changes"), not
duplicated here as a raw query.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import MonitorPage
from app.db.repositories.clients import ClientRepository
from app.db.repositories.monitor import MonitorRepository
from app.schemas.url_inspector import (
    UrlChangeDTO,
    UrlMonitorDTO,
    UrlMonitorPageVersionDTO,
    UrlSourceStateDTO,
)
from app.urlinspector.urlkey import lookup_variants

# First page of changes/versions shown inline (task-61 §0.8: one hot page
# on this portfolio carries 24 changes and 22 versions — a wall, not a
# panel, if rendered unbounded). The DTO's own `*_total` tells the UI
# whether there is more to page into.
_CHANGES_LIMIT = 10
_VERSIONS_LIMIT = 10
_VERSIONS_COUNT_CAP = 200


def collect_monitor(session: Session, *, client_id: str, url: str) -> UrlMonitorDTO:
    client = ClientRepository(session).get(client_id)
    monitor_settings = MonitorRepository(session).get_settings(client_id) if client else None
    if client is None or monitor_settings is None or not monitor_settings.enabled:
        state = UrlSourceStateDTO(
            source="monitor",
            state="not_configured",
            detail="Site Monitor isn't enabled for this client.",
        )
        return UrlMonitorDTO(state=state)

    variants = lookup_variants(url)
    repo = MonitorRepository(session)
    page: MonitorPage | None = repo.get_page_by_url_variants(client_id, variants)
    changes, changes_total = repo.changes_for_subjects(client_id, variants, limit=_CHANGES_LIMIT)

    if page is None and changes_total == 0:
        state = UrlSourceStateDTO(
            source="monitor", state="no_data", detail="This URL isn't tracked by Site Monitor."
        )
        return UrlMonitorDTO(state=state, changes=[], changes_total=0)

    state = UrlSourceStateDTO(source="monitor", state="has_data")
    change_dtos = [
        UrlChangeDTO(
            id=c.id,
            severity=c.severity,
            change_type=c.change_type,
            old_value=c.old_value,
            new_value=c.new_value,
            detected_at=c.detected_at,
            resolved_at=c.resolved_at,
            acknowledged_at=c.acknowledged_at,
        )
        for c in changes
    ]

    if page is None:
        return UrlMonitorDTO(state=state, changes=change_dtos, changes_total=changes_total)

    all_versions = repo.page_versions(page.id, limit=_VERSIONS_COUNT_CAP)
    version_dtos = [
        UrlMonitorPageVersionDTO(
            id=v.id,
            observed_at=v.observed_at,
            http_status=v.http_status,
            changed_fields=list(v.changed_fields_json or []),
        )
        for v in all_versions[:_VERSIONS_LIMIT]
    ]

    return UrlMonitorDTO(
        state=state,
        page_id=page.id,
        status=page.status,
        indexable=page.indexable,
        canonical=page.canonical,
        meta_robots=page.meta_robots,
        final_url=page.final_url,
        hop_count=page.hop_count,
        last_checked_at=page.last_checked_at,
        gone_since=page.gone_since,
        changes=change_dtos,
        changes_total=changes_total,
        versions=version_dtos,
        versions_total=len(all_versions),
    )
