"""reports engine: runs/events/files (task-42)

Three additive tables for the client-report generator (task-42 §4). Reports
are deliberately NOT a `SyncJob`/provider (§2.2) — no `(client, provider)`
cell, no metrics catalog row, no browser-profile lock — so this migration
touches nothing existing, exactly like task-35's `monitor_*` tables.

`report_run_events.seq` is monotonic WITHIN a run (`UNIQUE(run_id, seq)`),
not a global autoincrement like `job_events.seq` — each run's own SSE replay
only ever compares against its own rows. `report_files.run_id` is
`ondelete=SET NULL`: deleting a run must never destroy a PDF another run has
since reused via the closed-period cache (§3.5), and that cache's lookup key
is exactly `ix_report_files_cache_lookup`.

`kind`/`source`/`status`/`cross_check_status` are plain `String`, not
`sa.Enum` — the same choice task-35 made for monitor's tool-level states.

Revision ID: 83dac485b9c1
Revises: 5c3b4a7bda87
Create Date: 2026-08-17 12:07:27.757344

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Custom column types (GUID/UTCDateTime/DecimalString) render with a fully
# qualified name below; alembic's autogenerate import-detection doesn't
# always pick up this module, so it's imported unconditionally.
import app.db.base

# revision identifiers, used by Alembic.
revision: str = "83dac485b9c1"
down_revision: str | Sequence[str] | None = "5c3b4a7bda87"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report_runs",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("params_json", sa.JSON(), nullable=False),
        sa.Column("period_count", sa.Integer(), nullable=False),
        sa.Column("periods_done", sa.Integer(), nullable=False),
        sa.Column("periods_failed", sa.Integer(), nullable=False),
        sa.Column("cross_check_status", sa.String(length=16), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("heartbeat_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("queued_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("started_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("finished_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_report_runs_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_runs")),
    )
    op.create_index(
        "ix_report_runs_client_created", "report_runs", ["client_id", "created_at"], unique=False
    )
    op.create_index(op.f("ix_report_runs_client_id"), "report_runs", ["client_id"], unique=False)
    op.create_index("ix_report_runs_status", "report_runs", ["status"], unique=False)
    op.create_table(
        "report_files",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("run_id", app.db.base.GUID(length=36), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("period_label", sa.String(length=64), nullable=True),
        sa.Column("date_from", sa.Date(), nullable=True),
        sa.Column("date_to", sa.Date(), nullable=True),
        sa.Column("is_partial_period", sa.Boolean(), nullable=False),
        sa.Column("source_report_name", sa.String(length=200), nullable=True),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("mime", sa.String(length=100), nullable=True),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_report_files_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["report_runs.id"],
            name=op.f("fk_report_files_run_id_report_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_files")),
    )
    op.create_index(
        "ix_report_files_cache_lookup",
        "report_files",
        ["client_id", "source_report_name", "date_from", "date_to"],
        unique=False,
    )
    op.create_index(op.f("ix_report_files_client_id"), "report_files", ["client_id"], unique=False)
    op.create_table(
        "report_run_events",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("run_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=True),
        sa.Column("message", sa.String(length=2000), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["report_runs.id"],
            name=op.f("fk_report_run_events_run_id_report_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_run_events")),
        sa.UniqueConstraint("run_id", "seq", name="uq_report_run_events_run_id_seq"),
    )
    op.create_index(
        op.f("ix_report_run_events_run_id"), "report_run_events", ["run_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_report_run_events_run_id"), table_name="report_run_events")
    op.drop_table("report_run_events")
    op.drop_index(op.f("ix_report_files_client_id"), table_name="report_files")
    op.drop_index("ix_report_files_cache_lookup", table_name="report_files")
    op.drop_table("report_files")
    op.drop_index("ix_report_runs_status", table_name="report_runs")
    op.drop_index(op.f("ix_report_runs_client_id"), table_name="report_runs")
    op.drop_index("ix_report_runs_client_created", table_name="report_runs")
    op.drop_table("report_runs")
