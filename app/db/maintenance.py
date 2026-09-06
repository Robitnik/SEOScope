"""`ANALYZE` + `wal_checkpoint(TRUNCATE)` + `PRAGMA optimize` (task-66 §1.4,
scheduled by task-68 §1).

`sqlite_stat1` was empty in production — `ANALYZE` had never run on a
761MB database — so the query planner picked indexes blind. Live-measured
2026-09-06 on the exact query `GET /api/v1/dashboard` depends on:

    without stats: SEARCH ... USING INDEX ix_analytics_dataset_rows_provider
                   (provider=?)                              -> 40ms, 0 rows
    after ANALYZE: SEARCH ... USING INDEX ix_analytics_dataset_client_key_period
                   (client_id=? AND dataset_key=?)            -> 0.01ms

A ~4000x difference on that one query, and the whole endpoint 1215ms ->
640ms in the same A/B. `ANALYZE` itself costs ~500ms on the full database
— cheap next to what it buys, and this module exists so it is a
Docker-only single call, this scheduled way and manually via the CLI never
diverging.

Extracted out of `app/cli.py`'s `db-maintenance` command (task-66) into its
own module so `DbMaintenanceScheduler` (task-68) can call the exact same
code instead of duplicating it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MaintenanceResult:
    checkpoint_busy: int
    checkpoint_pages: int
    checkpoint_total_pages: int


def run_maintenance(session: Session) -> MaintenanceResult:
    """Direct DBAPI cursor via `text()`, not the ORM — `ANALYZE`/
    `PRAGMA optimize` are not table-mapped statements, and
    `wal_checkpoint` returns a row this caller wants to report, not
    something a plain `Session` exposes.

    Commits directly on `session`, the same way the pre-existing CLI
    command always did — NOT routed through `DbWriteGate` (task-67).
    `ANALYZE`'s own write (to `sqlite_stat1`) is not seen by pysqlite as
    DML, so `session_has_pending_writes` would report it as read-only and
    `worker_session_scope` would commit it directly anyway; and
    `wal_checkpoint(TRUNCATE)` competing with the gate's own idle
    checkpointer is harmless by design — one of the two simply gets
    `busy=1` and returns without reclaiming, exactly the same "give up
    instantly" contract the gate's checkpointer itself relies on
    (`app/db/writegate.py`).

    task-75: `wal_checkpoint`/`PRAGMA optimize` are SQLite-only syntax and
    error outright on Postgres, which has no WAL-checkpoint concept in
    this sense to begin with. Postgres's own `autovacuum` already runs
    `ANALYZE` continuously and adaptively (task-75 §6.4) — the one thing
    still worth doing here is a plain `ANALYZE`, real and valid there too,
    harmless to repeat once a day on top of what autovacuum already does.
    """
    dialect = session.get_bind().dialect.name
    if dialect != "sqlite":
        session.execute(text("ANALYZE"))
        session.commit()
        return MaintenanceResult(checkpoint_busy=0, checkpoint_pages=0, checkpoint_total_pages=0)
    session.execute(text("ANALYZE"))
    busy, checkpointed, total = session.execute(
        text("PRAGMA wal_checkpoint(TRUNCATE)")
    ).one()
    session.execute(text("PRAGMA optimize"))
    session.commit()
    return MaintenanceResult(
        checkpoint_busy=busy, checkpoint_pages=checkpointed, checkpoint_total_pages=total
    )
