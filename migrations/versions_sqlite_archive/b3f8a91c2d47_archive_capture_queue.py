"""archive capture queue (task-62)

Seven new columns on `archive_captures` and one new table
`archive_capture_events`, both additive:

- `queued_at`, `owner`, `heartbeat_at`, `cancel_requested`: a real FIFO
  queue (`ORDER BY queued_at`) and a liveness signal (`heartbeat_at`) that
  survives process boundaries, replacing `ArchiveScheduler._in_flight`
  in-process memory — a CLI-run capture, in its own OS process, was
  invisible to that set and got wrongly declared `interrupted` within a
  minute despite running to completion (live-observed, 2026-09-05).
- `scope`, `archive_url_id`: a capture can target one URL instead of a
  whole client (task-62 §2.7, the per-page "Refresh now" button).
- `progress_message`: the URL currently being processed, structured data
  rather than a log line to parse (task-62 §2.5).
- `archive_capture_events`: same shape as `report_run_events` — the
  per-capture log; `run_client_capture` wrote nothing anywhere before this
  (task-62 §0.5).

Straight `op.add_column`/`op.create_table` for six of the seven columns.
`archive_url_id` is the one exception: SQLite's `ALTER TABLE ADD COLUMN`
cannot add a foreign-key constraint outside of batch mode (verified on a
copy of the 2026-09-05 backup — plain `op.add_column` with a `ForeignKey`
raises `NotImplementedError: No support for ALTER of constraints in SQLite
dialect`), so that one column uses `batch_alter_table` — the sanctioned
case per CLAUDE.md ("без batch_alter_table, якщо тільки SQLite справді не
вимагає recreate"). Checked before running it: no table has an
`ondelete=CASCADE` foreign key onto `archive_captures` today (only
`archive_snapshots.capture_id`, which is `SET NULL`), so the recreate this
performs cannot cascade-delete anything.

All new columns are nullable or carry a constant default — every existing
row stays valid with no backfill.

Revision ID: b3f8a91c2d47
Revises: ce1c2680dfbe
Create Date: 2026-09-05 09:30:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base

revision: str = "b3f8a91c2d47"
down_revision: str | Sequence[str] | None = "ce1c2680dfbe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "archive_captures", sa.Column("queued_at", app.db.base.UTCDateTime(), nullable=True)
    )
    op.add_column("archive_captures", sa.Column("owner", sa.String(length=64), nullable=True))
    op.add_column(
        "archive_captures", sa.Column("heartbeat_at", app.db.base.UTCDateTime(), nullable=True)
    )
    op.add_column(
        "archive_captures",
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "archive_captures",
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="client"),
    )
    with op.batch_alter_table("archive_captures") as batch:
        batch.add_column(
            sa.Column("archive_url_id", app.db.base.GUID(length=36), nullable=True)
        )
        batch.create_foreign_key(
            op.f("fk_archive_captures_archive_url_id_archive_urls"),
            "archive_urls",
            ["archive_url_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.add_column(
        "archive_captures", sa.Column("progress_message", sa.String(length=500), nullable=True)
    )
    op.create_index(
        "ix_archive_captures_status_queued_at", "archive_captures", ["status", "queued_at"]
    )

    op.create_table(
        "archive_capture_events",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("capture_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=True),
        sa.Column("message", sa.String(length=2000), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["capture_id"], ["archive_captures.id"],
            name=op.f("fk_archive_capture_events_capture_id_archive_captures"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_archive_capture_events")),
        sa.UniqueConstraint(
            "capture_id", "seq", name=op.f("uq_archive_capture_events_capture_id_seq")
        ),
    )
    op.create_index(
        "ix_archive_capture_events_capture_id", "archive_capture_events", ["capture_id"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_archive_capture_events_capture_id", table_name="archive_capture_events")
    op.drop_table("archive_capture_events")
    op.drop_index("ix_archive_captures_status_queued_at", table_name="archive_captures")
    op.drop_column("archive_captures", "progress_message")
    with op.batch_alter_table("archive_captures") as batch:
        batch.drop_constraint(
            op.f("fk_archive_captures_archive_url_id_archive_urls"), type_="foreignkey"
        )
        batch.drop_column("archive_url_id")
    op.drop_column("archive_captures", "scope")
    op.drop_column("archive_captures", "cancel_requested")
    op.drop_column("archive_captures", "heartbeat_at")
    op.drop_column("archive_captures", "owner")
    op.drop_column("archive_captures", "queued_at")
