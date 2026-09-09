"""content_qa_packages table (task-90)

Revision ID: b63ac07bcbc3
Revises: c50ce678bee1
Create Date: 2026-09-09 12:30:00.000000

Strategy Package Check (`app/db/models.py::ContentQaPackage`): one strategy
plus N frameworks per run. **One row for the whole package**, not a child
`ContentQaReport` row per framework (design correction made before the API
layer was written, not after — the package report's UI (task-90 §7) shows
tabs aggregated across ALL frameworks at once, so the read path always
needs every framework in one query anyway; a child-row FK would only add a
JOIN for a per-document navigation nobody uses). `frameworks_json` holds
each framework's full outcome (state, findings, tab titles) as a JSON list.

Backup taken before applying, per CLAUDE.md: `docker compose exec db
pg_dump -U yovth -Fc -d yovth -f /tmp/pre_task90_backup.dump`, copied to
`app_data/backups/pre_task90_2026-09-09.dump`.

Plain `op.create_table`, no `batch_alter_table` — prod is on Postgres since
task-75, a new table is a catalog-only change there (same reasoning as
`c50ce678bee1`/`ea2070f4c343`).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Custom column types (GUID/UTCDateTime) render with a fully qualified name
# below; alembic's autogenerate import-detection doesn't always pick up this
# module, so it's imported unconditionally (same note as ea2070f4c343).
import app.db.base

# revision identifiers, used by Alembic.
revision: str = "b63ac07bcbc3"
down_revision: str | Sequence[str] | None = "c50ce678bee1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "content_qa_packages",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=True),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("source_file_id", sa.String(length=128), nullable=True),
        sa.Column("source_name", sa.String(length=255), nullable=False),
        sa.Column("profile_json", sa.JSON(), nullable=False),
        sa.Column("framework_count", sa.Integer(), nullable=False),
        sa.Column("strategy_findings_json", sa.JSON(), nullable=False),
        sa.Column("aggregate_findings_json", sa.JSON(), nullable=False),
        sa.Column("frameworks_json", sa.JSON(), nullable=False),
        sa.Column("severity_counts_json", sa.JSON(), nullable=False),
        sa.Column("sheets_json", sa.JSON(), nullable=False),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_content_qa_packages_client_id_clients"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_qa_packages")),
    )
    op.create_index(
        op.f("ix_content_qa_packages_created_at"), "content_qa_packages", ["created_at"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_content_qa_packages_created_at"), table_name="content_qa_packages")
    op.drop_table("content_qa_packages")
