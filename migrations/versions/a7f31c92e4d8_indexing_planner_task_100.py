"""indexing planner task 100

Revision ID: a7f31c92e4d8
Revises: eb9b03cf56b4
Create Date: 2026-09-21

Чотири таблиці планера ручного індексування (план task-100 §2.2). Жодної
нової колонки на наявній таблиці немає, тож `batch_alter_table` не потрібен
узагалі (CLAUDE.md: на SQLite він пересоздає таблицю й під
`PRAGMA foreign_keys=ON` каскадно чистить залежні).

Бекап перед застосуванням зроблено:
`docker compose exec -T db pg_dump -U yovth -Fc yovth >
app_data/backups/pre_task100_indexing_2026-09-21.dump` (267 MB).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.base

# revision identifiers, used by Alembic.
revision: str = "a7f31c92e4d8"
down_revision: str | Sequence[str] | None = "eb9b03cf56b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "indexing_settings",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("daily_quota", sa.Integer(), nullable=False),
        sa.Column("resubmit_after_days", sa.Integer(), nullable=False),
        sa.Column("scan_mode", sa.String(length=16), nullable=False),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_indexing_settings_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_indexing_settings")),
    )
    op.create_index(
        op.f("ix_indexing_settings_client_id"), "indexing_settings", ["client_id"], unique=True
    )

    op.create_table(
        "indexing_urls",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("url_key", sa.String(length=2048), nullable=False),
        sa.Column("first_seen_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("last_seen_in_sitemap_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("gone_from_sitemap_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("sitemap_lastmod", sa.Date(), nullable=True),
        sa.Column("coverage_state", sa.String(length=100), nullable=True),
        sa.Column("verdict", sa.String(length=32), nullable=True),
        sa.Column("robots_txt_state", sa.String(length=32), nullable=True),
        sa.Column("indexing_state", sa.String(length=48), nullable=True),
        sa.Column("page_fetch_state", sa.String(length=48), nullable=True),
        sa.Column("google_canonical", sa.String(length=2048), nullable=True),
        sa.Column("user_canonical", sa.String(length=2048), nullable=True),
        sa.Column("last_crawl_time", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("inspection_result_link", sa.String(length=1000), nullable=True),
        sa.Column("inspected_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("is_indexed", sa.Boolean(), nullable=True),
        sa.Column("first_indexed_seen_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("left_index_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("excluded", sa.Boolean(), nullable=False),
        sa.Column("excluded_reason", sa.String(length=32), nullable=True),
        sa.Column("excluded_by_user_id", app.db.base.GUID(length=36), nullable=True),
        sa.Column("priority_score", app.db.base.DecimalString(), nullable=True),
        sa.Column("reason_code", sa.String(length=32), nullable=True),
        sa.Column("reason_json", sa.JSON(), nullable=False),
        sa.Column("last_submitted_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("submissions_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_indexing_urls_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["excluded_by_user_id"],
            ["users.id"],
            name=op.f("fk_indexing_urls_excluded_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_indexing_urls")),
        sa.UniqueConstraint("client_id", "url_key", name="uq_indexing_urls_client_id_url_key"),
    )
    op.create_index(op.f("ix_indexing_urls_client_id"), "indexing_urls", ["client_id"])
    op.create_index(
        "ix_indexing_urls_client_indexed", "indexing_urls", ["client_id", "is_indexed"]
    )
    op.create_index(
        "ix_indexing_urls_client_score", "indexing_urls", ["client_id", "priority_score"]
    )
    op.create_index("ix_indexing_urls_inspected_at", "indexing_urls", ["inspected_at"])

    op.create_table(
        "indexing_submissions",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("indexing_url_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("planned_for", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=32), nullable=False),
        sa.Column("reason_json", sa.JSON(), nullable=False),
        sa.Column("state_at_plan", sa.String(length=100), nullable=True),
        sa.Column("submitted_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("submitted_by_user_id", app.db.base.GUID(length=36), nullable=True),
        sa.Column("state_at_submission", sa.String(length=100), nullable=True),
        sa.Column("indexed_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("days_to_index", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_indexing_submissions_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["indexing_url_id"],
            ["indexing_urls.id"],
            name=op.f("fk_indexing_submissions_indexing_url_id_indexing_urls"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by_user_id"],
            ["users.id"],
            name=op.f("fk_indexing_submissions_submitted_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_indexing_submissions")),
        sa.UniqueConstraint(
            "indexing_url_id", "planned_for", name="uq_indexing_submissions_url_planned_for"
        ),
    )
    op.create_index(
        op.f("ix_indexing_submissions_client_id"), "indexing_submissions", ["client_id"]
    )
    op.create_index(
        op.f("ix_indexing_submissions_indexing_url_id"),
        "indexing_submissions",
        ["indexing_url_id"],
    )
    op.create_index(
        "ix_indexing_submissions_client_planned",
        "indexing_submissions",
        ["client_id", "planned_for", "status"],
    )
    op.create_index(
        "ix_indexing_submissions_submitted_at", "indexing_submissions", ["submitted_at"]
    )

    op.create_table(
        "indexing_scans",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=True),
        sa.Column("started_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("finished_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("urls_total", sa.Integer(), nullable=True),
        sa.Column("urls_inspected", sa.Integer(), nullable=False),
        sa.Column("urls_failed", sa.Integer(), nullable=False),
        sa.Column("sitemap_added", sa.Integer(), nullable=False),
        sa.Column("sitemap_gone", sa.Integer(), nullable=False),
        sa.Column("sitemap_errors_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("owner", sa.String(length=64), nullable=True),
        sa.Column("heartbeat_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("created_by_user_id", app.db.base.GUID(length=36), nullable=True),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_indexing_scans_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_indexing_scans_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_indexing_scans")),
    )
    op.create_index(op.f("ix_indexing_scans_client_id"), "indexing_scans", ["client_id"])
    op.create_index("ix_indexing_scans_client_started", "indexing_scans", ["client_id", "started_at"])
    op.create_index("ix_indexing_scans_status", "indexing_scans", ["status"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("indexing_scans")
    op.drop_table("indexing_submissions")
    op.drop_table("indexing_urls")
    op.drop_table("indexing_settings")
