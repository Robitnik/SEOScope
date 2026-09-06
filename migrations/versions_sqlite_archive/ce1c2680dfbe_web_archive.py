"""web archive (task-59)

Ten new, additive tables for the self-hosted Wayback-style Web Archive:
`archive_settings`, `archive_captures`, `archive_urls`, `archive_blobs`,
`archive_snapshots`, `archive_assets`, `archive_asset_versions`,
`archive_snapshot_assets`, `archive_changes`, `archive_replay_misses`.
Straight `op.create_table`, no `batch_alter_table` (CLAUDE.md: the incident
of 2026-09-01 — `batch_alter_table` recreates the table on SQLite, which is
unrelated risk for brand-new tables, but the project convention is to reach
for it only when SQLite actually forces a recreate).

Revision ID: ce1c2680dfbe
Revises: a064ff1f082f
Create Date: 2026-09-04 00:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Custom column types (GUID/UTCDateTime) render with a fully qualified name
# below; alembic's autogenerate import-detection doesn't always pick up this
# module, so it's imported unconditionally (same convention as other
# migrations in this tree, e.g. a064ff1f082f).
import app.db.base

# revision identifiers, used by Alembic.
revision: str = "ce1c2680dfbe"
down_revision: str | Sequence[str] | None = "a064ff1f082f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "archive_settings",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("capture_assets", sa.Boolean(), nullable=False),
        sa.Column("max_urls", sa.Integer(), nullable=True),
        sa.Column("ignore_patterns_json", sa.JSON(), nullable=False),
        sa.Column("include_query_strings", sa.Boolean(), nullable=False),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"],
            name=op.f("fk_archive_settings_client_id_clients"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_archive_settings")),
        sa.UniqueConstraint("client_id", name=op.f("uq_archive_settings_client_id")),
    )

    op.create_table(
        "archive_captures",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("slot_key", sa.String(length=64), nullable=False),
        sa.Column("scheduled_for", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("finished_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("urls_total", sa.Integer(), nullable=True),
        sa.Column("urls_captured", sa.Integer(), nullable=False),
        sa.Column("urls_unchanged", sa.Integer(), nullable=False),
        sa.Column("urls_new", sa.Integer(), nullable=False),
        sa.Column("urls_failed", sa.Integer(), nullable=False),
        sa.Column("urls_blocked", sa.Integer(), nullable=False),
        sa.Column("assets_fetched", sa.Integer(), nullable=False),
        sa.Column("assets_reused", sa.Integer(), nullable=False),
        sa.Column("assets_failed", sa.Integer(), nullable=False),
        sa.Column("bytes_stored", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"],
            name=op.f("fk_archive_captures_client_id_clients"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_archive_captures")),
        sa.UniqueConstraint(
            "client_id", "slot_key", name=op.f("uq_archive_captures_client_id_slot_key")
        ),
    )
    op.create_index(
        "ix_archive_captures_client_started", "archive_captures",
        ["client_id", "started_at"], unique=False,
    )
    op.create_index(
        op.f("ix_archive_captures_client_id"), "archive_captures", ["client_id"], unique=False
    )
    op.create_index(
        op.f("ix_archive_captures_status"), "archive_captures", ["status"], unique=False
    )

    op.create_table(
        "archive_urls",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("url_key", sa.String(length=2048), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("first_captured_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("last_captured_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("snapshot_count", sa.Integer(), nullable=False),
        sa.Column("gone_since", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"],
            name=op.f("fk_archive_urls_client_id_clients"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_archive_urls")),
        sa.UniqueConstraint(
            "client_id", "url_key", name=op.f("uq_archive_urls_client_id_url_key")
        ),
    )
    op.create_index(
        op.f("ix_archive_urls_client_id"), "archive_urls", ["client_id"], unique=False
    )

    op.create_table(
        "archive_blobs",
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("gzip_size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=200), nullable=True),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("first_seen_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("uploaded_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("local_deleted_at", app.db.base.UTCDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("sha256", name=op.f("pk_archive_blobs")),
    )

    op.create_table(
        "archive_snapshots",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("archive_url_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("capture_id", app.db.base.GUID(length=36), nullable=True),
        sa.Column("observed_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("final_url", sa.String(length=2048), nullable=True),
        sa.Column("headers_json", sa.JSON(), nullable=False),
        sa.Column("content_type", sa.String(length=200), nullable=True),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("raw_sha256", sa.String(length=64), nullable=True),
        sa.Column("body_blob_sha256", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("canonical", sa.String(length=2048), nullable=True),
        sa.Column("meta_robots", sa.String(length=200), nullable=True),
        sa.Column("word_count", sa.Integer(), nullable=True),
        sa.Column("asset_count", sa.Integer(), nullable=False),
        sa.Column("assets_truncated", sa.Boolean(), nullable=False),
        sa.Column("store_reason", sa.String(length=16), nullable=False),
        sa.Column("prev_snapshot_id", app.db.base.GUID(length=36), nullable=True),
        sa.Column("last_confirmed_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("confirmed_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["archive_url_id"], ["archive_urls.id"],
            name=op.f("fk_archive_snapshots_archive_url_id_archive_urls"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"],
            name=op.f("fk_archive_snapshots_client_id_clients"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["capture_id"], ["archive_captures.id"],
            name=op.f("fk_archive_snapshots_capture_id_archive_captures"), ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["body_blob_sha256"], ["archive_blobs.sha256"],
            name=op.f("fk_archive_snapshots_body_blob_sha256_archive_blobs"), ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["prev_snapshot_id"], ["archive_snapshots.id"],
            name=op.f("fk_archive_snapshots_prev_snapshot_id_archive_snapshots"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_archive_snapshots")),
    )
    op.create_index(
        "ix_archive_snapshots_url_observed", "archive_snapshots",
        ["archive_url_id", "observed_at"], unique=False,
    )
    op.create_index(
        "ix_archive_snapshots_client_observed", "archive_snapshots",
        ["client_id", "observed_at"], unique=False,
    )
    op.create_index(
        op.f("ix_archive_snapshots_archive_url_id"), "archive_snapshots",
        ["archive_url_id"], unique=False,
    )

    op.create_table(
        "archive_assets",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("url_key", sa.String(length=2048), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("first_seen_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("last_checked_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("etag", sa.String(length=200), nullable=True),
        sa.Column("last_modified", sa.String(length=100), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_archive_assets")),
        sa.UniqueConstraint("url_key", name=op.f("uq_archive_assets_url_key")),
    )

    op.create_table(
        "archive_asset_versions",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("asset_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("blob_sha256", sa.String(length=64), nullable=True),
        sa.Column("content_type", sa.String(length=200), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("first_seen_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("last_confirmed_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["archive_assets.id"],
            name=op.f("fk_archive_asset_versions_asset_id_archive_assets"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["blob_sha256"], ["archive_blobs.sha256"],
            name=op.f("fk_archive_asset_versions_blob_sha256_archive_blobs"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_archive_asset_versions")),
    )
    op.create_index(
        "ix_archive_asset_versions_asset_confirmed", "archive_asset_versions",
        ["asset_id", "last_confirmed_at"], unique=False,
    )
    op.create_index(
        op.f("ix_archive_asset_versions_asset_id"), "archive_asset_versions",
        ["asset_id"], unique=False,
    )

    op.create_table(
        "archive_snapshot_assets",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("snapshot_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("asset_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("asset_version_id", app.db.base.GUID(length=36), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["archive_snapshots.id"],
            name=op.f("fk_archive_snapshot_assets_snapshot_id_archive_snapshots"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["archive_assets.id"],
            name=op.f("fk_archive_snapshot_assets_asset_id_archive_assets"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["asset_version_id"], ["archive_asset_versions.id"],
            name=op.f(
                "fk_archive_snapshot_assets_asset_version_id_archive_asset_versions"
            ),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_archive_snapshot_assets")),
    )
    op.create_index(
        "ix_archive_snapshot_assets_snapshot", "archive_snapshot_assets",
        ["snapshot_id"], unique=False,
    )

    op.create_table(
        "archive_changes",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("archive_url_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("snapshot_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("prev_snapshot_id", app.db.base.GUID(length=36), nullable=True),
        sa.Column("detected_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("change_kinds_json", sa.JSON(), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column("text_delta_words", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"],
            name=op.f("fk_archive_changes_client_id_clients"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["archive_url_id"], ["archive_urls.id"],
            name=op.f("fk_archive_changes_archive_url_id_archive_urls"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["archive_snapshots.id"],
            name=op.f("fk_archive_changes_snapshot_id_archive_snapshots"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["prev_snapshot_id"], ["archive_snapshots.id"],
            name=op.f("fk_archive_changes_prev_snapshot_id_archive_snapshots"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_archive_changes")),
    )
    op.create_index(
        "ix_archive_changes_client_detected", "archive_changes",
        ["client_id", "detected_at"], unique=False,
    )
    op.create_index(
        op.f("ix_archive_changes_client_id"), "archive_changes", ["client_id"], unique=False
    )
    op.create_index(
        op.f("ix_archive_changes_archive_url_id"), "archive_changes",
        ["archive_url_id"], unique=False,
    )

    op.create_table(
        "archive_replay_misses",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("url_key", sa.String(length=2048), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("last_seen_at", app.db.base.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_archive_replay_misses")),
        sa.UniqueConstraint("url_key", name=op.f("uq_archive_replay_misses_url_key")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("archive_replay_misses")
    op.drop_index(op.f("ix_archive_changes_archive_url_id"), table_name="archive_changes")
    op.drop_index(op.f("ix_archive_changes_client_id"), table_name="archive_changes")
    op.drop_index("ix_archive_changes_client_detected", table_name="archive_changes")
    op.drop_table("archive_changes")
    op.drop_index(
        "ix_archive_snapshot_assets_snapshot", table_name="archive_snapshot_assets"
    )
    op.drop_table("archive_snapshot_assets")
    op.drop_index(
        op.f("ix_archive_asset_versions_asset_id"), table_name="archive_asset_versions"
    )
    op.drop_index(
        "ix_archive_asset_versions_asset_confirmed", table_name="archive_asset_versions"
    )
    op.drop_table("archive_asset_versions")
    op.drop_table("archive_assets")
    op.drop_index(
        op.f("ix_archive_snapshots_archive_url_id"), table_name="archive_snapshots"
    )
    op.drop_index(
        "ix_archive_snapshots_client_observed", table_name="archive_snapshots"
    )
    op.drop_index("ix_archive_snapshots_url_observed", table_name="archive_snapshots")
    op.drop_table("archive_snapshots")
    op.drop_table("archive_blobs")
    op.drop_index(op.f("ix_archive_urls_client_id"), table_name="archive_urls")
    op.drop_table("archive_urls")
    op.drop_index(op.f("ix_archive_captures_status"), table_name="archive_captures")
    op.drop_index(op.f("ix_archive_captures_client_id"), table_name="archive_captures")
    op.drop_index(
        "ix_archive_captures_client_started", table_name="archive_captures"
    )
    op.drop_table("archive_captures")
    op.drop_table("archive_settings")
