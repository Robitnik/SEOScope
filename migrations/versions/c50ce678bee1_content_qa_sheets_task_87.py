"""add content_qa_reports.sheets_json (task-87)

Revision ID: c50ce678bee1
Revises: 9cf24fe2cb3e
Create Date: 2026-09-08 21:00:00.000000

Nullable `JSON` column carrying per-sheet state (`checked` / `not_selected` /
`no_recognized_columns` + `item_count`) for a Strategy Check run (task-87
§1.3). `NULL` for every row written before this migration — the DTO reads
that as `[]` and the UI falls back to the pre-existing `skipped_sheets`
list, so no backfill is needed and nothing about an old report is
"guessed" (CLAUDE.md invariant: absence of data is never inferred).

Direct `op.add_column`/`op.drop_column`, no `batch_alter_table` (same
reasoning as `9cf24fe2cb3e` — prod is on Postgres since task-75, adding a
nullable column with no default is a catalog-only change there). Backup
taken before running this: `docker compose exec db pg_dump -U yovth -Fc -d
yovth -f /tmp/pre_task87_backup.dump`, copied to the host afterwards.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c50ce678bee1"
down_revision: str | Sequence[str] | None = "9cf24fe2cb3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("content_qa_reports", sa.Column("sheets_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("content_qa_reports", "sheets_json")
