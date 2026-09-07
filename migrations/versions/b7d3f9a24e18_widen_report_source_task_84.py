"""widen report source columns for the google_api value (task-84)

Revision ID: b7d3f9a24e18
Revises: c3a91f7e2b6d
Create Date: 2026-09-07 17:06:00.000000

`report_runs.source` та `report_batches.source` були `String(8)` — рівно під
`api`/`pdf`, як їх завів task-42. task-84 додає третє джерело, `google_api`
(10 символів), яке `VARCHAR(8)` обрізає ще на INSERT
(`StringDataRightTruncation` на Postgres).

План task-84 казав «міграція не потрібна» — він перевірив шар Python-`Literal`
(де нове значення справді не потребує DDL), але не ширину колонки в бойовій
Postgres-схемі. Розширюємо обидві до `String(16)`.

Postgres трактує збільшення ліміту `varchar(n)` як зміну лише в каталозі: без
перезапису таблиці, жоден рядок не читається, наявні `api`/`pdf` не зачеплені.
Застереження CLAUDE.md про `batch_alter_table`/SQLite тут не діє (task-75 вже
перевела прод на Postgres). Бекап зроблено перед прогоном
(`app_data/backups/pre-task84-*.dump`).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d3f9a24e18"
down_revision: str | Sequence[str] | None = "c3a91f7e2b6d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        "report_runs", "source",
        existing_type=sa.String(8), type_=sa.String(16), existing_nullable=False,
    )
    op.alter_column(
        "report_batches", "source",
        existing_type=sa.String(8), type_=sa.String(16), existing_nullable=False,
    )


def downgrade() -> None:
    """Downgrade schema.

    Safe only while no row holds a `source` longer than 8 chars — i.e. before
    any `google_api` run/batch exists. A shortening `varchar` change DOES scan
    every row on Postgres; that is acceptable here (the table is small and a
    downgrade is a deliberate, out-of-band action).
    """
    op.alter_column(
        "report_batches", "source",
        existing_type=sa.String(16), type_=sa.String(8), existing_nullable=False,
    )
    op.alter_column(
        "report_runs", "source",
        existing_type=sa.String(16), type_=sa.String(8), existing_nullable=False,
    )
