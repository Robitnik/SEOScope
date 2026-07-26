"""alt text tool: reports and items (task-27)

Дві **мутабельні** таблиці — свідомо не в immutable `analytics_dataset_rows`
(див. докстрінги моделей): статус прогресу й результат перевірки за задумом
змінюються багато разів на життя рядка.

Статуси/вердикти зберігаються як `String`, а не `sa.Enum`: це UI-стани
інструмента, а не спільний контракт провайдерів — валідація живе у
Pydantic-DTO, і додати новий вердикт не має вимагати DDL.

Revision ID: f69c3d5a8b17
Revises: e58b2f4d6a91
Create Date: 2026-07-26 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base

revision: str = "f69c3d5a8b17"
down_revision: str | Sequence[str] | None = "e58b2f4d6a91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alt_text_reports",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        # Nullable: аудит роблять і для проспекта, якого ще нема в клієнтах.
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("source_sheet", sa.String(length=255), nullable=True),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("column_map_json", sa.JSON(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("warnings_json", sa.JSON(), nullable=False),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_alt_text_reports_client_id_clients"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alt_text_reports")),
    )
    op.create_index(
        "ix_alt_text_reports_source_sha256", "alt_text_reports", ["source_sha256"], unique=False
    )
    op.create_index(
        "ix_alt_text_reports_created_at", "alt_text_reports", ["created_at"], unique=False
    )

    op.create_table(
        "alt_text_items",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("report_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("image_url", sa.String(length=2048), nullable=False),
        sa.Column("search_key", sa.String(length=512), nullable=False),
        sa.Column("search_key_fallback", sa.String(length=512), nullable=True),
        sa.Column("url_kind", sa.String(length=24), nullable=False),
        sa.Column("derivation_json", sa.JSON(), nullable=False),
        sa.Column("new_alt", sa.String(length=8192), nullable=False),
        sa.Column("old_alt", sa.String(length=8192), nullable=True),
        sa.Column("page_url", sa.String(length=2048), nullable=True),
        # Повний список сторінок використання: аудити пакують їх в одну
        # клітинку, а перевірка alt має право обійти кілька сторінок.
        sa.Column("page_urls_json", sa.JSON(), nullable=False),
        sa.Column("extra_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("status_changed_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("verify_status", sa.String(length=24), nullable=True),
        sa.Column("verify_alt", sa.String(length=8192), nullable=True),
        sa.Column("verify_http_status", sa.Integer(), nullable=True),
        sa.Column("verify_detail", sa.String(length=500), nullable=True),
        sa.Column("verify_checked_at", app.db.base.UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["alt_text_reports.id"],
            name=op.f("fk_alt_text_items_report_id_alt_text_reports"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alt_text_items")),
        sa.UniqueConstraint("report_id", "row_index", name="uq_alt_text_items_report_id_row_index"),
    )
    op.create_index(
        op.f("ix_alt_text_items_report_id"), "alt_text_items", ["report_id"], unique=False
    )
    op.create_index(
        "ix_alt_text_items_report_status", "alt_text_items", ["report_id", "status"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_alt_text_items_report_status", table_name="alt_text_items")
    op.drop_index(op.f("ix_alt_text_items_report_id"), table_name="alt_text_items")
    op.drop_table("alt_text_items")
    op.drop_index("ix_alt_text_reports_created_at", table_name="alt_text_reports")
    op.drop_index("ix_alt_text_reports_source_sha256", table_name="alt_text_reports")
    op.drop_table("alt_text_reports")
