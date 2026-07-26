"""cell progress_message (task-26 follow-up: live progress count)

Carries a free-form, human-readable live-progress string for one
`(client, provider)` cell (e.g. "found 148 posts so far" while a blog crawl
paginates the WordPress REST API). `current_stage` is a static per-stage label
keyed off `STAGE_LABELS` on the frontend — it cannot express a running count.
`progress_message`, when set, is preferred over the stage label by the UI.

NULL for any adapter that never emits one; no backfill required.

Revision ID: e58b2f4d6a91
Revises: d47b9c1e5a02
Create Date: 2026-07-24 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e58b2f4d6a91"
down_revision: str | Sequence[str] | None = "d47b9c1e5a02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sync_job_providers", sa.Column("progress_message", sa.String(length=200), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("sync_job_providers", "progress_message")
