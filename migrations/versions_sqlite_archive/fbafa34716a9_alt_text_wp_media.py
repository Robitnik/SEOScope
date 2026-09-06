"""alt text tool: WordPress media ID resolution (task-39)

Адитивні nullable-колонки на `alt_text_items`. `NULL` у `wp_media_status`
означає «ще не шукали» — окремий стан від `not_found` («шукали, нема»),
той самий інваріант PLAN §12, що й у `verify_status` (task-27).

`wp_media_json` — єдина NOT NULL колонка (кандидати при `ambiguous`, `{}` за
замовчуванням, як `derivation_json`/`extra_json` у task-27); backfill явний,
а не покладений на `server_default` — той самий підхід, що в task-34.

Revision ID: fbafa34716a9
Revises: 9d4e2f7a6c81
Create Date: 2026-08-14 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Кастомні типи колонок рендеряться повним іменем (як у решті ревізій).
import app.db.base

revision: str = "fbafa34716a9"
down_revision: str | Sequence[str] | None = "9d4e2f7a6c81"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite не вміє ALTER COLUMN — усі зміни схеми йдуть через batch-режим.
    with op.batch_alter_table("alt_text_items") as batch:
        batch.add_column(sa.Column("wp_media_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("wp_media_status", sa.String(length=24), nullable=True))
        batch.add_column(sa.Column("wp_media_confidence", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("wp_media_alt", sa.String(length=8192), nullable=True))
        batch.add_column(sa.Column("wp_media_admin_url", sa.String(length=2048), nullable=True))
        batch.add_column(sa.Column("wp_media_detail", sa.String(length=500), nullable=True))
        batch.add_column(
            sa.Column("wp_media_json", sa.JSON(), nullable=False, server_default="{}")
        )
        batch.add_column(
            sa.Column("wp_media_checked_at", app.db.base.UTCDateTime(), nullable=True)
        )

    # Явний backfill замість опори на `server_default` (той самий підхід, що
    # в c9f45a1b3e72): міграція має лишати таблицю коректною сама по собі.
    op.execute(
        sa.text("UPDATE alt_text_items SET wp_media_json = '{}' WHERE wp_media_json IS NULL")
    )


def downgrade() -> None:
    with op.batch_alter_table("alt_text_items") as batch:
        batch.drop_column("wp_media_checked_at")
        batch.drop_column("wp_media_json")
        batch.drop_column("wp_media_detail")
        batch.drop_column("wp_media_admin_url")
        batch.drop_column("wp_media_alt")
        batch.drop_column("wp_media_confidence")
        batch.drop_column("wp_media_status")
        batch.drop_column("wp_media_id")
