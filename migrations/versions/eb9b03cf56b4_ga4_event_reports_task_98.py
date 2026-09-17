"""ga4 event month reports task 98

Revision ID: eb9b03cf56b4
Revises: 4644b24285b9
Create Date: 2026-09-17

Один рядок = один прогін тула «GA4 Events by Month» (план task-98 §2.3):
знімок відповіді GA4 по обраних місяцях, щоб збережене посилання показувало
ті самі числа, які бачив оператор (GA4 доуточнює дані ~48 год).

Чистий `op.create_table` — нової колонки на наявній таблиці немає, тож
`batch_alter_table` не потрібен узагалі (CLAUDE.md: на SQLite він пересоздає
таблицю й під `PRAGMA foreign_keys=ON` каскадно чистить залежні). Бекап перед
застосуванням: `docker compose exec -T db pg_dump -U yovth -Fc yovth >
app_data/backups/pre_task98_ga4_events_2026-09-17.dump`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base

# revision identifiers, used by Alembic.
revision: str = "eb9b03cf56b4"
down_revision: str | Sequence[str] | None = "4644b24285b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "ga4_event_month_reports",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=True),
        sa.Column("property_id", sa.String(length=64), nullable=False),
        sa.Column("months_json", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("rows_json", sa.JSON(), nullable=False),
        sa.Column("created_by_user_id", app.db.base.GUID(length=36), nullable=True),
        sa.Column("created_by_kind", sa.String(length=16), nullable=True),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_ga4_event_month_reports_client_id_clients"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_ga4_event_month_reports_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ga4_event_month_reports")),
    )
    op.create_index(
        "ix_ga4_event_month_reports_created_at",
        "ga4_event_month_reports",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_ga4_event_month_reports_created_at", table_name="ga4_event_month_reports"
    )
    op.drop_table("ga4_event_month_reports")
