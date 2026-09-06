"""cloud_objects: outbox ledger for the B2 cloud-storage sweeper (task-52)

One additive table, no changes to any existing one. `cloud_objects` is the
"what's already in the cloud" ledger the sweeper (`app/cloud/sweeper.py`)
reads on every tick: it LEFT JOINs `monitor_blobs`/`report_files` against
this table to find rows with no entry yet (or a `failed` one) and uploads
just those. `UNIQUE (kind, source_ref)` is what makes a re-run of the
sweeper idempotent instead of creating duplicate uploads.

`kind="db_backup"` rows use `source_ref` values like `"daily/2026-08-19"` /
`"weekly/2026-08-17"` / `"monthly/2026-08-01"` — the tier is encoded in the
ref rather than a separate column, so GFS pruning is a simple prefix filter
and no schema change is needed if a new tier is ever added.

`kind="watermark"` rows are a deliberate reuse of the same table for the
"only archive blobs seen after this timestamp" cutover marker (task-52 §3.4)
instead of a second table for a single row.

Revision ID: 08824e25f060
Revises: a7c4e91f2b6d
Create Date: 2026-08-19 22:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Custom column types (GUID/UTCDateTime) render with a fully qualified name
# below; alembic's autogenerate import-detection doesn't always pick up this
# module, so it's imported unconditionally (matches every prior migration
# that uses them).
import app.db.base

revision: str = "08824e25f060"
down_revision: str | Sequence[str] | None = "a7c4e91f2b6d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cloud_objects",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=200), nullable=False),
        sa.Column("key", sa.String(length=500), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("uploaded_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cloud_objects")),
        sa.UniqueConstraint("kind", "source_ref", name="uq_cloud_objects_kind_source_ref"),
    )
    op.create_index(
        "ix_cloud_objects_kind_status", "cloud_objects", ["kind", "status"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_cloud_objects_kind_status", table_name="cloud_objects")
    op.drop_table("cloud_objects")
