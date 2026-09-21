"""indexing first_not_indexed_at task 100

Revision ID: c8a2d5f10b73
Revises: a7f31c92e4d8
Create Date: 2026-09-21

Одна колонка на `indexing_urls`: коли ми ВПЕРШЕ побачили сторінку не в
індексі. Без неї контрольна група аналітики (§2.6) рахує лаг від моменту,
коли ми URL узагалі вперше побачили, і сторінка, яка вже була в індексі,
дає 0 днів. Живо це намалювало медіану 0.0d на 27 сторінках — тобто
«сторінки заходять миттєво самі», що зробило б висновок «ручна робота
марна» на підставі артефакту вимірювання.

Прямий `op.add_column`, без `batch_alter_table` (CLAUDE.md): колонка
nullable і без константного дефолту, SQLite такого recreate не вимагає.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

import app.db.base

revision: str = "c8a2d5f10b73"
down_revision: str | Sequence[str] | None = "a7f31c92e4d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    import sqlalchemy as sa

    op.add_column(
        "indexing_urls",
        sa.Column("first_not_indexed_at", app.db.base.UTCDateTime(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("indexing_urls", "first_not_indexed_at")
