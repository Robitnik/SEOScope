"""Meta + copy QA source for the URL Inspector (task-61 §2.3).

Two tiers, most-authoritative first:

1. A persisted `PageInventoryItem` (task-57) — its `findings_json` is
   already the full, uncalibrated QA result for that exact row, so it's
   read as-is (`meta_source="page_inventory"`).
2. A `MonitorPage` with no inventory row for it (e.g. the client has never
   run a Page Inventory build) — `app.urlinspector.metaqa.qa_findings` runs
   fresh against the monitor's own title/description/h1
   (`meta_source="monitor"`).

`PageInventoryRepository`/`MonitorRepository`'s own docstrings reserve their
tables to themselves — both are used through their public methods here,
never a raw query, and the single per-client `list_items` fetch (bounded:
task-61 §0.1 measured <=510 rows on the busiest client) is filtered to one
URL in Python rather than adding a variant-lookup method to task-57's repo.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import PageInventoryItem
from app.db.repositories.monitor import MonitorRepository
from app.db.repositories.page_inventory import PageInventoryRepository
from app.schemas.url_inspector import UrlFindingDTO, UrlMetaDTO, UrlSourceStateDTO
from app.urlinspector.metaqa import qa_findings
from app.urlinspector.urlkey import inspector_key, lookup_variants


def _stored_finding_to_dto(entry: dict[str, object]) -> UrlFindingDTO:
    """Mirrors `app.pageinventory.service._finding_from_stored`'s shape
    (`{check, field, severity, detail, value}` — task-57 §model docstring)
    without importing that private helper."""
    return UrlFindingDTO(
        check=str(entry.get("check") or ""),
        field=str(entry.get("field") or "page"),
        severity=str(entry.get("severity") or "info"),
        message=str(entry.get("detail") or ""),
    )


def _from_inventory_item(item: PageInventoryItem) -> UrlMetaDTO:
    return UrlMetaDTO(
        state=UrlSourceStateDTO(source="page_inventory", state="has_data"),
        meta_source="page_inventory",
        title=item.title,
        description=item.description,
        h1=item.h1,
        keywords=list(item.keywords_json or []),
        findings=[_stored_finding_to_dto(f) for f in (item.findings_json or [])],
    )


def collect_meta(session: Session, *, client_id: str, url: str) -> UrlMetaDTO:
    inventory_repo = PageInventoryRepository(session)
    snapshot = inventory_repo.get_snapshot(client_id)
    if snapshot is not None:
        target_key = inspector_key(url)
        for item in inventory_repo.list_items(snapshot.id):
            if inspector_key(item.url) == target_key:
                return _from_inventory_item(item)

    monitor_page = MonitorRepository(session).get_page_by_url_variants(
        client_id, lookup_variants(url)
    )
    if monitor_page is not None and monitor_page.status is not None:
        return UrlMetaDTO(
            state=UrlSourceStateDTO(source="page_inventory", state="has_data"),
            meta_source="monitor",
            title=monitor_page.title,
            description=monitor_page.description,
            h1=monitor_page.h1,
            keywords=[],
            findings=qa_findings(
                monitor_page.url,
                title=monitor_page.title,
                description=monitor_page.description,
                h1=monitor_page.h1,
                status=monitor_page.status,
            ),
        )

    return UrlMetaDTO(
        state=UrlSourceStateDTO(
            source="page_inventory", state="no_data", detail="No meta/copy data for this URL yet."
        )
    )
