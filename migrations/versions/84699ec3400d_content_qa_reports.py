"""content qa reports (task-53)

Одна таблиця на обидва тули — Strategy Check і Framework Check: `doc_type`
розрізняє. Без дитячої таблиці для findings: на відміну від `alt_text_items`
тут немає per-finding мутабельного стану, весь звіт — один незмінний рядок
(`findings_json`), той самий підхід, що `config_json`/`params_json`/
`metrics_json` в інших моделях.

Revision ID: 84699ec3400d
Revises: 08824e25f060
Create Date: 2026-08-21 06:06:21.240566

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
revision: str = "84699ec3400d"
down_revision: str | Sequence[str] | None = "08824e25f060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "content_qa_reports",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        # Nullable: перевірку роблять і для проспекта, якого ще нема в клієнтах.
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=True),
        sa.Column("doc_type", sa.String(length=32), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=False),
        sa.Column("profile_json", sa.JSON(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("skipped_sheets_json", sa.JSON(), nullable=False),
        sa.Column("findings_json", sa.JSON(), nullable=False),
        sa.Column("severity_counts_json", sa.JSON(), nullable=False),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_content_qa_reports_client_id_clients"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_qa_reports")),
    )
    op.create_index(
        "ix_content_qa_reports_doc_type_created_at",
        "content_qa_reports",
        ["doc_type", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_content_qa_reports_doc_type_created_at", table_name="content_qa_reports")
    op.drop_table("content_qa_reports")
