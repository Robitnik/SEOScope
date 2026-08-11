"""client lifecycle status: active / paused (task-34)

Комерційний стан співпраці на `clients`. `paused` = оплата припинена: клієнт
лишається видимим і зберігає всю історію, але випадає з масових операцій.
Технічна видимість рядка лишається за наявним `enabled` — це два різні поля,
і міграція жодного `enabled` не чіпає.

`status_changed_at` NULL для всіх наявних рядків: статус їм ніхто не міняв,
а підставляти сюди дату створення означало б показати в UI вигадану дату
«Active since …».

Revision ID: c9f45a1b3e72
Revises: b7d21e9f4c83
Create Date: 2026-08-05 10:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Кастомні типи колонок рендеряться повним іменем (як у решті ревізій).
import app.db.base

revision: str = "c9f45a1b3e72"
down_revision: str | Sequence[str] | None = "b7d21e9f4c83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite не вміє ALTER COLUMN — усі зміни схеми йдуть через batch-режим.
    with op.batch_alter_table("clients") as batch:
        batch.add_column(
            sa.Column("status", sa.String(length=16), nullable=False, server_default="active")
        )
        batch.add_column(
            sa.Column("status_changed_at", app.db.base.UTCDateTime(), nullable=True)
        )
        batch.add_column(sa.Column("status_note", sa.String(length=300), nullable=True))

    # Явний backfill замість опори на `server_default`: міграція має лишати
    # таблицю в коректному стані сама по собі, а не залежати від того, як
    # конкретний рушій застосував дефолт до вже наявних рядків.
    op.execute(sa.text("UPDATE clients SET status = 'active' WHERE status IS NULL"))


def downgrade() -> None:
    with op.batch_alter_table("clients") as batch:
        batch.drop_column("status_note")
        batch.drop_column("status_changed_at")
        batch.drop_column("status")
