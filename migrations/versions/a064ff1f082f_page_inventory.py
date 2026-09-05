"""page inventory (task-57)

Two new, additive tables — `page_inventory_snapshots` (one active snapshot
per client) and `page_inventory_items` (its rows, `ondelete="CASCADE"` off
the snapshot). Straight `op.create_table`, no `batch_alter_table` (CLAUDE.md:
`batch_alter_table` on SQLite recreates the table, which is unrelated risk
for a brand-new table but the project convention is to never reach for it
unless SQLite actually forces a recreate).

Revision ID: a064ff1f082f
Revises: f1a4c9e7b023
Create Date: 2026-09-04 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Custom column types (GUID/UTCDateTime) render with a fully qualified name
# below; alembic's autogenerate import-detection doesn't always pick up this
# module, so it's imported unconditionally (same convention as other
# migrations in this tree, e.g. 84699ec3400d).
import app.db.base

# revision identifiers, used by Alembic.
revision: str = "a064ff1f082f"
down_revision: str | Sequence[str] | None = "f1a4c9e7b023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "page_inventory_snapshots",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("built_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("source_collected_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("pages_total", sa.Integer(), nullable=False),
        sa.Column("pages_unique", sa.Integer(), nullable=False),
        sa.Column("pages_with_breadcrumbs", sa.Integer(), nullable=False),
        sa.Column("pages_with_keywords", sa.Integer(), nullable=False),
        sa.Column("breadcrumb_source", sa.String(length=16), nullable=False),
        sa.Column("warnings_json", sa.JSON(), nullable=False),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_page_inventory_snapshots_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_page_inventory_snapshots")),
        sa.UniqueConstraint("client_id", name=op.f("uq_page_inventory_snapshots_client_id")),
    )
    op.create_table(
        "page_inventory_items",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("snapshot_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("canonical_key", sa.String(length=2048), nullable=False),
        sa.Column("duplicate_urls_json", sa.JSON(), nullable=False),
        sa.Column("page_label", sa.String(length=500), nullable=False),
        sa.Column("breadcrumbs_json", sa.JSON(), nullable=False),
        sa.Column("breadcrumb_source", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("keywords_json", sa.JSON(), nullable=False),
        sa.Column("keywords_source", sa.String(length=8), nullable=False),
        sa.Column("h1", sa.String(length=500), nullable=True),
        sa.Column("status", sa.Integer(), nullable=True),
        sa.Column("status_label", sa.String(length=16), nullable=False),
        sa.Column("indexable", sa.Boolean(), nullable=True),
        sa.Column("canonical_missing", sa.Boolean(), nullable=False),
        sa.Column("checked", sa.Boolean(), nullable=False),
        sa.Column("findings_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["page_inventory_snapshots.id"],
            name=op.f("fk_page_inventory_items_snapshot_id_page_inventory_snapshots"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_page_inventory_items_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_page_inventory_items")),
    )
    op.create_index(
        "ix_page_inventory_items_snapshot_status",
        "page_inventory_items",
        ["snapshot_id", "status_label"],
        unique=False,
    )
    op.create_index(
        op.f("ix_page_inventory_items_snapshot_id"),
        "page_inventory_items",
        ["snapshot_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_page_inventory_items_snapshot_id"), table_name="page_inventory_items")
    op.drop_index("ix_page_inventory_items_snapshot_status", table_name="page_inventory_items")
    op.drop_table("page_inventory_items")
    op.drop_table("page_inventory_snapshots")
