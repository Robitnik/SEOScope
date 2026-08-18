"""report_batches + report_runs.batch_id (task-47)

One additive table for the portfolio-wide batch run (task-47 §2.2/§4). A
batch is a parent AGGREGATE over ordinary `report_runs` rows, not a new kind
of work — every client in it stays a perfectly normal `ReportRun` (periods,
`periods_json`, events, XLSX, cancellation, stale-run recovery all keep
working unchanged), it just carries a `batch_id` back to its parent.

`report_runs.batch_id` is `SET NULL`, not `CASCADE`: deleting a batch row
must never destroy a report a client has already been sent (mirrors
`report_files.run_id`'s own `SET NULL` rationale from task-42).

`report_files.kind` needed no migration at all for the new `"client_pdf"`
value — it is already a plain `String(16)` column (task-42), the same
reason task-42/45/47's `kind`/`status`/`source` columns are never `sa.Enum`.

Revision ID: d8f42b9e6c17
Revises: a1c58d7e26b4
Create Date: 2026-08-18 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Custom column types (GUID/UTCDateTime) render with a fully qualified name,
# same as every other migration touching them.
import app.db.base

revision: str = "d8f42b9e6c17"
down_revision: str | Sequence[str] | None = "a1c58d7e26b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report_batches",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("params_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("client_count", sa.Integer(), nullable=False),
        sa.Column("clients_done", sa.Integer(), nullable=False),
        sa.Column("clients_failed", sa.Integer(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("queued_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("started_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("finished_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_batches")),
    )
    op.create_index("ix_report_batches_status", "report_batches", ["status"], unique=False)

    # SQLite can't ALTER TABLE ADD COLUMN ... REFERENCES in one step — batch
    # mode is the standard way every prior migration in this repo does it.
    with op.batch_alter_table("report_runs") as batch:
        batch.add_column(sa.Column("batch_id", app.db.base.GUID(length=36), nullable=True))
        batch.create_foreign_key(
            op.f("fk_report_runs_batch_id_report_batches"),
            "report_batches",
            ["batch_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_report_runs_batch_id", "report_runs", ["batch_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_report_runs_batch_id", table_name="report_runs")
    with op.batch_alter_table("report_runs") as batch:
        batch.drop_constraint(
            op.f("fk_report_runs_batch_id_report_batches"), type_="foreignkey"
        )
        batch.drop_column("batch_id")

    op.drop_index("ix_report_batches_status", table_name="report_batches")
    op.drop_table("report_batches")
