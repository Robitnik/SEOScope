"""audit events (task-72)

Одна нова append-only таблиця, жодного дотику до наявних — тільки
`op.create_table`, без `batch_alter_table` (CLAUDE.md: на SQLite той
пересоздає таблицю, і саме так одна ADD COLUMN-міграція стерла прод).

Індекси підібрані під реальні запити адмінки: стрічка за часом, «що робила
ця людина», «що робили з цим клієнтом», «усі входи».

Revision ID: b83e5c17a942
Revises: a1c72f4b9d30
Create Date: 2026-09-06 13:00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base

# revision identifiers, used by Alembic.
revision: str = "b83e5c17a942"
down_revision: str | Sequence[str] | None = "a1c72f4b9d30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "audit_events",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("ts", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=True),
        sa.Column("actor_email", sa.String(length=320), nullable=True),
        sa.Column("actor_name", sa.String(length=200), nullable=True),
        sa.Column("actor_role", sa.String(length=16), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=256), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("resource_type", sa.String(length=40), nullable=True),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("resource_label", sa.String(length=200), nullable=True),
        sa.Column("method", sa.String(length=10), nullable=True),
        sa.Column("path", sa.String(length=500), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    # Немає FK на `users`: журнал переживає видалення чого завгодно, а актор
    # у ньому денормалізований саме для цього (див. модель).
    op.create_index(op.f("ix_audit_events_action"), "audit_events", ["action"])
    op.create_index("ix_audit_events_ts", "audit_events", ["ts"])
    op.create_index("ix_audit_events_actor_id_ts", "audit_events", ["actor_id", "ts"])
    op.create_index("ix_audit_events_action_ts", "audit_events", ["action", "ts"])
    op.create_index(
        "ix_audit_events_resource", "audit_events", ["resource_type", "resource_id", "ts"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_audit_events_resource", table_name="audit_events")
    op.drop_index("ix_audit_events_action_ts", table_name="audit_events")
    op.drop_index("ix_audit_events_actor_id_ts", table_name="audit_events")
    op.drop_index("ix_audit_events_ts", table_name="audit_events")
    op.drop_index(op.f("ix_audit_events_action"), table_name="audit_events")
    op.drop_table("audit_events")
