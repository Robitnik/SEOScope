"""Page Inventory repository (task-57): the only place that touches
`page_inventory_snapshots`/`page_inventory_items` directly.

Same conventions as `app/db/repositories/client_monthly.py`: constructed
directly with a plain `Session`, mutating methods `flush()` and leave
`commit()` to the caller's transaction boundary.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import PageInventoryItem, PageInventorySnapshot


class PageInventoryRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def commit(self) -> None:
        self._session.commit()

    def get_snapshot(self, client_id: str) -> PageInventorySnapshot | None:
        stmt = select(PageInventorySnapshot).where(PageInventorySnapshot.client_id == client_id)
        return self._session.scalars(stmt).first()

    def list_items(self, snapshot_id: str) -> list[PageInventoryItem]:
        stmt = (
            select(PageInventoryItem)
            .where(PageInventoryItem.snapshot_id == snapshot_id)
            .order_by(PageInventoryItem.url)
        )
        return list(self._session.scalars(stmt))

    def replace_snapshot(
        self,
        client_id: str,
        *,
        snapshot_fields: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> PageInventorySnapshot:
        """One client keeps one active snapshot (task-57 §3) — deleting the
        old row cascades its items (`ondelete="CASCADE"`), then a fresh
        snapshot + item set is inserted. Both halves happen inside the
        caller's transaction, so a rebuild never leaves a client with zero
        rows mid-write."""
        existing = self.get_snapshot(client_id)
        if existing is not None:
            self._session.execute(
                delete(PageInventorySnapshot).where(PageInventorySnapshot.id == existing.id)
            )
            self._session.flush()

        snapshot = PageInventorySnapshot(client_id=client_id, **snapshot_fields)
        self._session.add(snapshot)
        self._session.flush()

        for item_fields in items:
            self._session.add(
                PageInventoryItem(snapshot_id=snapshot.id, client_id=client_id, **item_fields)
            )
        self._session.flush()
        return snapshot
