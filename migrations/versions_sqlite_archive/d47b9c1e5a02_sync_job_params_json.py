"""sync job params_json (task-25 tech audit)

Carries non-secret, per-run job parameters that the period fields cannot express.
Used by the machine-only Technical SEO Audit: a `tech_audit_recheck` job stores the
source run dir plus the exact `(slug, url)` findings to re-verify, so the provider
adapter — which by contract never touches the database — receives them through
`CollectionContext.job_params`.

NULL for every ordinary metric-provider sync; no backfill required.

Revision ID: d47b9c1e5a02
Revises: c36f2a7d8e14
Create Date: 2026-07-23 17:10:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d47b9c1e5a02"
down_revision: str | Sequence[str] | None = "c36f2a7d8e14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sync_jobs", sa.Column("params_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_jobs", "params_json")
