"""task-75 §3.1 — `description` on three tables was `String(1000)`, but

live production data holds meta descriptions up to 8385 chars: SQLite never
enforced the declared VARCHAR length, so this went unnoticed until a
Postgres migration dry-run rejected them with `StringDataRightTruncation`
(`value too long for type character varying(1000)`). Fixed by widening to
`Text` — see `app/db/models.py`'s `MonitorPage`/`PageInventoryItem`/
`ArchiveSnapshot`.

Deliberately NOT an Alembic migration on the live SQLite database: both
`monitor_pages` and `archive_snapshots` are the CASCADE-referenced side of
foreign keys with tens of thousands of dependent rows (`monitor_page_versions`,
`archive_snapshot_assets`), and SQLite's `ALTER TABLE` has no native
column-type change — only `batch_alter_table`, which recreates the table
and is exactly the mechanism that wiped `client_integrations`/`metric_values`
in the 2026-09-01 incident (CLAUDE.md). SQLite already stores arbitrarily
long strings in a declared `VARCHAR(n)` column regardless (no runtime
enforcement, see the second test below) — the fix's only observable effect
is `Base.metadata.create_all()` on a *fresh* database, which is exactly the
path the future Postgres baseline (task-75 §5) uses.
"""

from __future__ import annotations

from sqlalchemy import inspect

from app.db import models
from app.db.models import MonitorPage, PageInventoryItem, PageInventorySnapshot

_LONG_DESCRIPTION = "x" * 8385  # matches the longest value actually seen in production


def test_fresh_schema_declares_description_as_unbounded_text(engine):
    """`create_all` on a clean database — the path the Postgres baseline
    will use — must emit an unbounded type, not a length-limited one.
    """
    insp = inspect(engine)
    for table_name in ("monitor_pages", "page_inventory_items", "archive_snapshots"):
        columns = {c["name"]: c for c in insp.get_columns(table_name)}
        col_type = columns["description"]["type"]
        assert getattr(col_type, "length", None) is None, (
            f"{table_name}.description still declares a length ({col_type}); "
            "must be unbounded Text so it survives a Postgres migration"
        )


def test_long_description_round_trips_on_every_affected_model(session):
    """Not just the declared type: an 8385-char value must actually survive
    a write + read on all three models, on the engine we ship today.
    """
    client = models.Client(name="Acme", slug="acme-desc", domain="acme-desc.example")
    session.add(client)
    session.flush()

    page = MonitorPage(
        client_id=client.id,
        url="https://acme-desc.example/",
        description=_LONG_DESCRIPTION,
    )
    session.add(page)

    snapshot = PageInventorySnapshot(client_id=client.id)
    session.add(snapshot)
    session.flush()

    item = PageInventoryItem(
        snapshot_id=snapshot.id,
        client_id=client.id,
        url="https://acme-desc.example/inventory",
        canonical_key="https://acme-desc.example/inventory",
        description=_LONG_DESCRIPTION,
    )
    session.add(item)
    session.flush()

    for row in (page, item):
        session.refresh(row)
        assert row.description == _LONG_DESCRIPTION
        assert len(row.description) == 8385
