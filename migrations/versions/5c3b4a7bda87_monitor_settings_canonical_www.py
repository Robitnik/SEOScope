"""monitor: canonical www/non-www host per client (task-38 follow-up)

Nullable `canonical_www` on `monitor_settings`. `NULL` = not known which host
the site canonically redirects to — `_base_url` keeps reading `client.domain`
verbatim, exactly as before this column existed (PLAN §12 invariant: absence
of data is never coerced into a default). `True`/`False` lets an operator
record the answer once instead of every check silently eating one extra
redirect hop and the change feed reading as ambiguous "was this redirect
supposed to happen?" noise.

Revision ID: 5c3b4a7bda87
Revises: fbafa34716a9
Create Date: 2026-08-14 13:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5c3b4a7bda87"
down_revision: str | Sequence[str] | None = "fbafa34716a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite не вміє ALTER COLUMN — усі зміни схеми йдуть через batch-режим.
    with op.batch_alter_table("monitor_settings") as batch:
        batch.add_column(sa.Column("canonical_www", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("monitor_settings") as batch:
        batch.drop_column("canonical_www")
