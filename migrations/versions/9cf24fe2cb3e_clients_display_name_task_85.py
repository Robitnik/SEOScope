"""add clients.display_name (task-85)

Revision ID: 9cf24fe2cb3e
Revises: b7d3f9a24e18
Create Date: 2026-09-07 20:00:00.000000

Nullable `String(200)` for the client's full name on client-facing surfaces
(task-85 §2.1) — `Client.name` is the agency's internal short code
(`SAPHNY`, `SHB`, `3XE`) and it was leaking straight into the client
document's `<h1>`. `NULL` means "fall back to `name`", so this column starts
empty for every existing row and changes nothing until an operator fills it
in.

Direct `op.add_column`/`op.drop_column`, no `batch_alter_table` (CLAUDE.md —
the SQLite-recreate hazard doesn't apply here, prod is on Postgres since
task-75, and adding a nullable column with no default is a catalog-only
change there: no rewrite, no row scan). Backup taken before running this
(`pg_dump`, see the task-85 completion report) — the stale
`scripts/backup_sqlite.py`/`cloud-backup` CLI still assume a SQLite
`DATABASE_URL` and hard-fail against Postgres (a pre-existing gap, out of
this task's owned paths).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9cf24fe2cb3e"
down_revision: str | Sequence[str] | None = "b7d3f9a24e18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("clients", sa.Column("display_name", sa.String(200), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("clients", "display_name")
