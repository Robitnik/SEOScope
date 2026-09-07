"""archive capture force column (task-80)

Revision ID: c3a91f7e2b6d
Revises: d6d668fe8d53
Create Date: 2026-09-07 10:50:00.000000

"Recapture" — примусова нова копія всього сайту клієнта, навіть коли жодна
сторінка не змінилась (`app/webarchive/capture.py::capture_url`). Прапорець
живе на самому рядку `archive_captures`, не в запиті: обхід робить воркер
уже ПІСЛЯ того, як HTTP-запит завершився — той самий інваріант, що й
`scope`/`archive_url_id` (task-62 §2.7). Плоский `op.add_column` з
константним дефолтом, без `batch_alter_table` — на Postgres це метаданна
операція, і застереження про `batch_alter_table`/SQLite тут не діє
(task-75 вже перевела прод на Postgres).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3a91f7e2b6d"
down_revision: str | Sequence[str] | None = "d6d668fe8d53"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "archive_captures",
        sa.Column("force", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("archive_captures", "force", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("archive_captures", "force")
