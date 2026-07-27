"""redirect checker: checks and targets (task-29)

Дві **мутабельні** таблиці інструмента (той самий вибір, що у task-27): рядок
перезаписується при повторному трейсі, провайдера й періоду немає, тож в
immutable `analytics_dataset_rows` дані не вміщаються.

Хопи ланцюжка — у `chain_json`, а не окремою таблицею: ланцюжок завжди читається
цілком і ніколи не агрегується по хопах.

Вердикти й статуси — `String`, не `sa.Enum`: це UI-стани інструмента, і додати
новий вердикт не має вимагати DDL.

Revision ID: a3f81c62d5b4
Revises: f69c3d5a8b17
Create Date: 2026-07-27 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base

revision: str = "a3f81c62d5b4"
down_revision: str | Sequence[str] | None = "f69c3d5a8b17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "redirect_checks",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        # Nullable: ланцюжки перевіряють і поза контекстом клієнта.
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("input_count", sa.Integer(), nullable=False),
        sa.Column("target_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("options_json", sa.JSON(), nullable=False),
        sa.Column("rejected_json", sa.JSON(), nullable=False),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_redirect_checks_client_id_clients"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_redirect_checks")),
    )
    op.create_index(
        "ix_redirect_checks_created_at", "redirect_checks", ["created_at"], unique=False
    )

    op.create_table(
        "redirect_check_targets",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("check_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("input_url", sa.String(length=2048), nullable=False),
        sa.Column("normalized_url", sa.String(length=2048), nullable=False),
        sa.Column("input_notes_json", sa.JSON(), nullable=False),
        sa.Column("run_status", sa.String(length=12), nullable=False),
        sa.Column("verdict", sa.String(length=24), nullable=True),
        sa.Column("final_url", sa.String(length=2048), nullable=True),
        sa.Column("final_status_code", sa.Integer(), nullable=True),
        sa.Column("hop_count", sa.Integer(), nullable=True),
        # Повний ланцюжок як він був: список хопів зі статусом, `Location`,
        # часом і переліком того, що змінилося на кроці.
        sa.Column("chain_json", sa.JSON(), nullable=False),
        sa.Column("flags_json", sa.JSON(), nullable=False),
        sa.Column("final_page_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=32), nullable=True),
        sa.Column("error_detail", sa.String(length=1000), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        # `NULL` = не перевіряли (це не «редіректів немає»).
        sa.Column("checked_at", app.db.base.UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["check_id"],
            ["redirect_checks.id"],
            name=op.f("fk_redirect_check_targets_check_id_redirect_checks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_redirect_check_targets")),
        sa.UniqueConstraint(
            "check_id", "row_index", name="uq_redirect_check_targets_check_id_row_index"
        ),
    )
    op.create_index(
        op.f("ix_redirect_check_targets_check_id"),
        "redirect_check_targets",
        ["check_id"],
        unique=False,
    )
    op.create_index(
        "ix_redirect_check_targets_check_status",
        "redirect_check_targets",
        ["check_id", "run_status"],
        unique=False,
    )
    op.create_index(
        "ix_redirect_check_targets_check_verdict",
        "redirect_check_targets",
        ["check_id", "verdict"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_redirect_check_targets_check_verdict", table_name="redirect_check_targets"
    )
    op.drop_index("ix_redirect_check_targets_check_status", table_name="redirect_check_targets")
    op.drop_index(
        op.f("ix_redirect_check_targets_check_id"), table_name="redirect_check_targets"
    )
    op.drop_table("redirect_check_targets")
    op.drop_index("ix_redirect_checks_created_at", table_name="redirect_checks")
    op.drop_table("redirect_checks")
