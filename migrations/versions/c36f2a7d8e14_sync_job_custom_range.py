"""sync job custom range

Revision ID: c36f2a7d8e14
Revises: b6e13a8c9f42
Create Date: 2026-07-23 16:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c36f2a7d8e14"
down_revision: str | Sequence[str] | None = "b6e13a8c9f42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sync_jobs", sa.Column("custom_start", sa.Date(), nullable=True))
    op.add_column("sync_jobs", sa.Column("custom_end", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_jobs", "custom_end")
    op.drop_column("sync_jobs", "custom_start")
