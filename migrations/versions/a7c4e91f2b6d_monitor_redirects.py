"""monitor: redirect rule monitoring (task-51)

Four additive columns on `monitor_settings`:

- `redirects_enabled` (default True) — per-client kill switch.
- `redirect_source` ("auto" | "redirection" | "probe" | "off", default
  "auto") — "auto" self-detects via `/wp-json/` namespaces and caches the
  verdict in `redirect_source_json`.
- `redirect_source_json` (default `{}`) — cached `{"source": ..., "checked_at": ...}`.
- `redirects_last_checked_at` (nullable) — last successful redirect
  collection for this client; `NULL` gates the baseline (nothing to diff
  against yet) and is the lower bound ("appeared between X and Y") for the
  very first rules ever seen — the plugin itself stores neither a creation
  date nor an author (task-51 §1.3), so this is the only source of that date.

One new table, `monitor_redirects` — current state per rule, no separate
version-chain table: the whole-list history already lives in
`MonitorResource(kind="redirects")` + `MonitorResourceVersion` (same
content-addressed blob mechanism robots.txt/sitemaps already use), and a
per-rule change already carries old_value/new_value on `MonitorChange` the
moment it is detected (task-51 §3.1).

All four `monitor_settings` columns default to "off/unknown" for every
existing row — zero behavior change until an operator opts a client's
redirect source in.

Revision ID: a7c4e91f2b6d
Revises: 658f2eeb029e
Create Date: 2026-08-19 20:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Custom column types (GUID/UTCDateTime) render with a fully qualified name
# below; alembic's autogenerate import-detection doesn't always pick up this
# module, so it's imported unconditionally (matches 6d9419c9493c/658f2eeb029e).
import app.db.base

revision: str = "a7c4e91f2b6d"
down_revision: str | Sequence[str] | None = "658f2eeb029e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite не вміє ALTER COLUMN — усі зміни схеми йдуть через batch-режим.
    with op.batch_alter_table("monitor_settings") as batch:
        batch.add_column(
            sa.Column(
                "redirects_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
            )
        )
        batch.add_column(
            sa.Column("redirect_source", sa.String(length=16), nullable=False, server_default="auto")
        )
        batch.add_column(
            sa.Column("redirect_source_json", sa.JSON(), nullable=False, server_default="{}")
        )
        batch.add_column(
            sa.Column("redirects_last_checked_at", app.db.base.UTCDateTime(), nullable=True)
        )

    op.create_table(
        "monitor_redirects",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("rule_key", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.Integer(), nullable=True),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("target_url", sa.String(length=2048), nullable=True),
        sa.Column("http_code", sa.Integer(), nullable=True),
        sa.Column("match_type", sa.String(length=32), nullable=False),
        sa.Column("regex", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("group_name", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("rule_sha256", sa.String(length=64), nullable=False),
        sa.Column("first_seen_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("first_seen_window_start", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("last_seen_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("removed_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("version_seq", sa.Integer(), nullable=False),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_monitor_redirects_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitor_redirects")),
        sa.UniqueConstraint(
            "client_id", "source", "rule_key", name="uq_monitor_redirects_client_source_key"
        ),
    )
    op.create_index(
        op.f("ix_monitor_redirects_client_id"), "monitor_redirects", ["client_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_monitor_redirects_client_id"), table_name="monitor_redirects")
    op.drop_table("monitor_redirects")
    with op.batch_alter_table("monitor_settings") as batch:
        batch.drop_column("redirects_last_checked_at")
        batch.drop_column("redirect_source_json")
        batch.drop_column("redirect_source")
        batch.drop_column("redirects_enabled")
