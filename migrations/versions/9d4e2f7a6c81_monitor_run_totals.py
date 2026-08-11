"""monitor run totals + per-run change counters backfill (task-37 F2/F4)

`urls_total` (nullable, `None` for pre-existing rows — never `0`) lets the UI
render a real "Checked N of M" progress bar instead of animating a made-up
percentage (frontend/AGENTS.md §9). Runner sets it once, before the crawl
loop starts, from `len(targets)`.

Backfills `changes_critical/warning/info` on every existing `monitor_runs`
row from `monitor_changes.run_id` — those columns were being overwritten with
the client's CURRENT open-count total instead of what that specific run
found (task-37 F4), so every historical row shows the same wrong numbers
until this runs once.

Revision ID: 9d4e2f7a6c81
Revises: 6d9419c9493c
Create Date: 2026-08-11 15:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9d4e2f7a6c81"
down_revision: str | Sequence[str] | None = "6d9419c9493c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("monitor_runs", sa.Column("urls_total", sa.Integer(), nullable=True))

    for severity, column in (
        ("critical", "changes_critical"),
        ("warning", "changes_warning"),
        ("info", "changes_info"),
    ):
        op.execute(
            sa.text(
                f"""
                UPDATE monitor_runs
                SET {column} = (
                    SELECT COUNT(*) FROM monitor_changes
                    WHERE monitor_changes.run_id = monitor_runs.id
                    AND monitor_changes.severity = :severity
                )
                """
            ).bindparams(severity=severity)
        )


def downgrade() -> None:
    op.drop_column("monitor_runs", "urls_total")
