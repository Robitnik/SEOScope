"""monitor: antibot fetch lane (task-50)

Two additive, nullable-or-defaulted columns on `monitor_settings`:

- `fetch_mode` ("auto" | "http" | "browser", default "auto") — which
  transport a client's page fetches use. "http" is also the emergency kill
  switch for a client already escalated to the slow browser lane.
- `blocked_detected_at` (nullable) — set by the runner the moment a run
  confirms the site is blocking automated requests while `fetch_mode ==
  "auto"`; read back to decide whether the NEXT run still needs the slow
  lane (a client stays escalated for `monitor_antibot_sticky_days` after the
  last confirmed block, not just the one run that found it).

Both default to "not yet escalated, plain HTTP" for every existing row —
zero behavior change for the 11 of 12 portfolio clients that were never
blocked to begin with (task-50 §0.10).

Revision ID: 658f2eeb029e
Revises: d8f42b9e6c17
Create Date: 2026-08-19 16:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base

revision: str = "658f2eeb029e"
down_revision: str | Sequence[str] | None = "d8f42b9e6c17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite не вміє ALTER COLUMN — усі зміни схеми йдуть через batch-режим.
    with op.batch_alter_table("monitor_settings") as batch:
        batch.add_column(
            sa.Column("fetch_mode", sa.String(length=16), nullable=False, server_default="auto")
        )
        batch.add_column(
            sa.Column("blocked_detected_at", app.db.base.UTCDateTime(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("monitor_settings") as batch:
        batch.drop_column("blocked_detected_at")
        batch.drop_column("fetch_mode")
