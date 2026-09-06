"""agent tokens (task-79)

Revision ID: ea2070f4c343
Revises: 9701790bda11
Create Date: 2026-09-06 17:13:17.852897

Personal access tokens (`app/db/models.py::AgentToken`) — "кожен видає собі
сам" у кабінеті (`#/account`), замість спільного `AGENT_DEV_TOKEN` (task-73),
що структурно мертвий у проді. Плоский `op.create_table`: перша реальна
таблиця поверх Postgres-бейзлайну (`9701790bda11`), жодного
`batch_alter_table` — застереження про нього більше не діє (task-75).
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
revision: str = "ea2070f4c343"
down_revision: str | Sequence[str] | None = "9701790bda11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "agent_tokens",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("user_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("token_sha256", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=8), nullable=False),
        sa.Column(
            "role",
            sa.Enum("admin", "member", "viewer", name="userrole", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("expires_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("last_used_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("revoked_at", app.db.base.UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_agent_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_tokens")),
        sa.UniqueConstraint("token_sha256", name="uq_agent_tokens_token_sha256"),
    )
    op.create_index(
        op.f("ix_agent_tokens_token_sha256"), "agent_tokens", ["token_sha256"], unique=True
    )
    op.create_index(op.f("ix_agent_tokens_user_id"), "agent_tokens", ["user_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_agent_tokens_user_id"), table_name="agent_tokens")
    op.drop_index(op.f("ix_agent_tokens_token_sha256"), table_name="agent_tokens")
    op.drop_table("agent_tokens")
