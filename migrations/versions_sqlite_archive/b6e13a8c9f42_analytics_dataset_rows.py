"""analytics dataset rows

Revision ID: b6e13a8c9f42
Revises: 7147a40f57b7
Create Date: 2026-07-23 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base

revision: str = "b6e13a8c9f42"
down_revision: str | Sequence[str] | None = "7147a40f57b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analytics_dataset_rows",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("snapshot_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column(
            "provider",
            sa.Enum("ahrefs", "semrush", "gsc", "reporting_ninja", name="provider", length=32),
            nullable=False,
        ),
        sa.Column("dataset_key", sa.String(length=80), nullable=False),
        sa.Column("dimension_key", sa.String(length=80), nullable=False),
        sa.Column("dimension_value", sa.String(length=2048), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column(
            "quality",
            sa.Enum(
                "complete", "partial", "truncated", "stale", "unavailable",
                name="quality", length=32,
            ),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"],
            name=op.f("fk_analytics_dataset_rows_client_id_clients"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["provider_snapshots.id"],
            name=op.f("fk_analytics_dataset_rows_snapshot_id_provider_snapshots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analytics_dataset_rows")),
        sa.UniqueConstraint(
            "snapshot_id", "dataset_key", "dimension_value",
            name="uq_analytics_dataset_snapshot_value",
        ),
    )
    op.create_index(
        "ix_analytics_dataset_client_key_period",
        "analytics_dataset_rows",
        ["client_id", "dataset_key", "period_start", "period_end"],
        unique=False,
    )
    op.create_index(
        op.f("ix_analytics_dataset_rows_client_id"),
        "analytics_dataset_rows", ["client_id"], unique=False,
    )
    op.create_index(
        op.f("ix_analytics_dataset_rows_provider"),
        "analytics_dataset_rows", ["provider"], unique=False,
    )
    op.create_index(
        op.f("ix_analytics_dataset_rows_snapshot_id"),
        "analytics_dataset_rows", ["snapshot_id"], unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_analytics_dataset_rows_snapshot_id"), table_name="analytics_dataset_rows"
    )
    op.drop_index(op.f("ix_analytics_dataset_rows_provider"), table_name="analytics_dataset_rows")
    op.drop_index(op.f("ix_analytics_dataset_rows_client_id"), table_name="analytics_dataset_rows")
    op.drop_index("ix_analytics_dataset_client_key_period", table_name="analytics_dataset_rows")
    op.drop_table("analytics_dataset_rows")
