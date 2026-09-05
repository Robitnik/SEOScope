"""client dev/test marker: is_dev

Постійний прапорець на `clients` для рядків, що не є реальними (платними)
клієнтами — sandbox/dev-домени, тестові записи. На відміну від `enabled`
(технічна видимість рядка на дашборді) і `status` (комерційний active/paused),
`is_dev` не ховає рядок і не впливає на масові операції — це лише позначка
для оператора («DEV»-бейдж в UI).

Revision ID: 0e4f3471de13
Revises: 40d3a23f853d
Create Date: 2026-09-01 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0e4f3471de13"
down_revision: str | Sequence[str] | None = "40d3a23f853d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # НАВМИСНО без `batch_alter_table`: той у SQLite для будь-якої зміни
    # пересоздає таблицю (create-copy-**DROP**-rename), а `DROP TABLE clients`
    # під `PRAGMA foreign_keys=ON` — це неявний каскадний DELETE по кожному
    # FK `ondelete="CASCADE"` на `clients.id` (`client_integrations` і все,
    # що далі каскадом висить на ній: metric_values, sync_jobs, report_runs,
    # job_events...). Перевірено на копії бекапу: recreate-варіант цієї ж
    # міграції стирав `client_integrations`/`metric_values` дощенту, лишаючи
    # сам `clients` цілим — саме так і сталось на проді 2026-09-01.
    # Просте `ADD COLUMN` з константним дефолтом SQLite підтримує нативно,
    # без rebuild і без цього ризику.
    op.add_column(
        "clients",
        sa.Column("is_dev", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # Явний backfill замість опори на `server_default` (той самий підхід, що
    # й у c9f45a1b3e72_client_status.py): міграція має лишати таблицю в
    # коректному стані сама по собі.
    op.execute(sa.text("UPDATE clients SET is_dev = 0 WHERE is_dev IS NULL"))


def downgrade() -> None:
    # Так само без batch/recreate — сучасний SQLite (>=3.35) підтримує
    # `DROP COLUMN` нативно.
    op.drop_column("clients", "is_dev")
