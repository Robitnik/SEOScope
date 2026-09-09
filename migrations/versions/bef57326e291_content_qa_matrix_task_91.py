"""add content_qa_packages.matrix_json (task-91)

Revision ID: bef57326e291
Revises: b63ac07bcbc3
Create Date: 2026-09-09 12:26:41.401098

Nullable `JSON` column carrying the Strategy/Framework/Site comparison
matrix (task-91 §2.2) for a Strategy Package Check run. `NULL` for every
row written before this migration — the DTO reads that as `[]`
(`app/api/tools_strategy_package.py`), same pattern as `c50ce678bee1`
(`content_qa_reports.sheets_json`, task-87): absence of data is never
guessed backwards.

Direct `op.add_column`/`op.drop_column`, no `batch_alter_table` (prod is
on Postgres since task-75 — adding a nullable column with no default is a
catalog-only change there, CLAUDE.md). Backup taken before running this:
`docker compose exec -T db pg_dump -U yovth -Fc yovth >
app_data/backups/pre_task91_matrix_2026-09-09.dump`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bef57326e291"
down_revision: str | Sequence[str] | None = "b63ac07bcbc3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("content_qa_packages", sa.Column("matrix_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("content_qa_packages", "matrix_json")
