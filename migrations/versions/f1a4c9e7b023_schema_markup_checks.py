"""schema markup checks (task-56)

Один рядок = один прогін Schema Markup Checker: URL, вердикт, знайдені
schema.org-типи, знахідки трьох рівнів валідації (L0/L1/L2) і згенерований
JSON-LD-сніпет (якщо генератор щось запропонував). Той самий підхід, що
`content_qa_reports`/`redirect_checks` — без дитячої таблиці, весь звіт один
незмінний JSON-рядок; додаткова структура (per-finding статус тощо) тут не
потрібна, бо findings не мають окремого життєвого циклу.

Revision ID: f1a4c9e7b023
Revises: 0e4f3471de13
Create Date: 2026-09-04 05:30:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Custom column types (GUID/UTCDateTime) render with a fully qualified name
# below; alembic's autogenerate import-detection doesn't always pick up this
# module, so it's imported unconditionally (same pattern as other migrations).
import app.db.base

# revision identifiers, used by Alembic.
revision: str = "f1a4c9e7b023"
down_revision: str | Sequence[str] | None = "0e4f3471de13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "schema_markup_checks",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        # Nullable: перевіряють і чужі/проспектні URL, не лише клієнтів дашборду.
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=True),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("final_url", sa.String(length=2048), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("page_kind", sa.String(length=32), nullable=True),
        sa.Column("found_types_json", sa.JSON(), nullable=False),
        sa.Column("findings_json", sa.JSON(), nullable=False),
        sa.Column("generated_json", sa.JSON(), nullable=True),
        # Сирі JSON-LD/microdata блоки як реально знайдені на сторінці (UI "Raw
        # markup") — окремо від `found_types_json`/`findings_json`, які лише
        # підсумовують. `server_default` тримає ADD-семантику лайтовою (SQLite
        # ADD COLUMN без DEFAULT NULL-заповнить старі рядки без recreate таблиці).
        sa.Column(
            "raw_markup_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")
        ),
        sa.Column("vocab_version", sa.String(length=32), nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_schema_markup_checks_client_id_clients"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schema_markup_checks")),
    )
    op.create_index(
        "ix_schema_markup_checks_created_at",
        "schema_markup_checks",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_schema_markup_checks_created_at", table_name="schema_markup_checks")
    op.drop_table("schema_markup_checks")
