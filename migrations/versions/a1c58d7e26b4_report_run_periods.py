"""report_runs.periods_json — per-period progress as data (task-45)

One additive nullable JSON column. Progress used to exist only as three
counters (`period_count`/`periods_done`/`periods_failed`), so the UI had to
reconstruct per-period state by parsing log message text (task-45 §1.1);
this column holds the real thing: one row per period with its label, exact
date range, state (`waiting|running|done|cached|failed|skipped`), start/finish
timestamps and duration.

Nullable, no server default: rows created before this migration simply have
no per-period detail (`NULL`), which the API maps to an empty list — never to
a fabricated "all waiting" list for a run that finished long ago.

Revision ID: a1c58d7e26b4
Revises: 83dac485b9c1
Create Date: 2026-08-17 16:20:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c58d7e26b4"
down_revision: str | Sequence[str] | None = "83dac485b9c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("report_runs", sa.Column("periods_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("report_runs", "periods_json")
