"""parallel collection: which browser slot a cell is running in (task-31)

Nullable `slot` on `sync_job_providers`. Deliberately **not** required for the
scheduling itself — capacity is enforced by counting `running` cells per
`profile_key` inside the claim UPDATE, and the slot index is handed out by an
in-process pool. It is persisted only so the live progress panel can say
«Browser 2» truthfully, logs can be read per lane, and a cell interrupted by a
restart still shows which lane it died in.

`NULL` therefore means "not running in a numbered lane" — a queued/terminal
cell, an API-only provider, or a row from before this migration.

Revision ID: b7d21e9f4c83
Revises: a3f81c62d5b4
Create Date: 2026-07-31 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d21e9f4c83"
down_revision: str | Sequence[str] | None = "a3f81c62d5b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sync_job_providers", sa.Column("slot", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_job_providers", "slot")
