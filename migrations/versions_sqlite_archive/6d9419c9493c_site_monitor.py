"""site monitor: robots/sitemap/pages/DNS with full change history (task-35)

Ten additive tables implementing "current state + immutable version chain"
(task-35 §3.2): a mutable row per object (`monitor_pages`/`monitor_resources`/
`monitor_dns_records`) carries `last_checked_at`; a `*_versions` row is
appended only when the fingerprint actually changed, so storage grows with
real change, not with run count. `monitor_blobs` is a content-addressed store
shared by every client and run — identical bytes are kept exactly once.
`monitor_changes` is the change/alert feed; it deliberately avoids the word
"alerts" (already `/clients/{id}/alerts`, GSC traffic-drop alerts, task-17).

Nothing existing is touched — no `clients` table columns, no `enabled`
semantics. `kind`/`status`/`severity`/`scope`/`change_type` are plain
`String`, not `sa.Enum` — the same choice already made for
`redirect_checks`/`alt_text_items`: adding a new value must not require DDL.

Revision ID: 6d9419c9493c
Revises: c9f45a1b3e72
Create Date: 2026-08-11 12:21:45.433584
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Custom column types (GUID/UTCDateTime/DecimalString) render with a fully
# qualified name below; alembic's autogenerate import-detection doesn't
# always pick up this module, so it's imported unconditionally.
import app.db.base

# revision identifiers, used by Alembic.
revision: str = "6d9419c9493c"
down_revision: str | Sequence[str] | None = "c9f45a1b3e72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "monitor_blobs",
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("gzip_size_bytes", sa.Integer(), nullable=False),
        sa.Column("mime", sa.String(length=100), nullable=True),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("first_seen_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("last_seen_at", app.db.base.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("sha256", name=op.f("pk_monitor_blobs")),
    )
    op.create_table(
        "monitor_dns_records",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("record_type", sa.String(length=16), nullable=False),
        sa.Column("values_json", sa.JSON(), nullable=False),
        sa.Column("ttl", sa.Integer(), nullable=True),
        sa.Column("resolver", sa.String(length=100), nullable=True),
        sa.Column("version_seq", sa.Integer(), nullable=False),
        sa.Column("last_checked_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_monitor_dns_records_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitor_dns_records")),
        sa.UniqueConstraint(
            "client_id", "name", "record_type", name="uq_monitor_dns_records_client_name_type"
        ),
    )
    op.create_index(
        op.f("ix_monitor_dns_records_client_id"), "monitor_dns_records", ["client_id"], unique=False
    )
    op.create_table(
        "monitor_pages",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("is_key_page", sa.Boolean(), nullable=False),
        sa.Column("in_sitemap", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Integer(), nullable=True),
        sa.Column("final_url", sa.String(length=2048), nullable=True),
        sa.Column("hop_count", sa.Integer(), nullable=True),
        sa.Column("indexable", sa.Boolean(), nullable=True),
        sa.Column("meta_robots", sa.String(length=200), nullable=True),
        sa.Column("x_robots_tag", sa.String(length=200), nullable=True),
        sa.Column("canonical", sa.String(length=2048), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("h1", sa.String(length=500), nullable=True),
        sa.Column("word_count", sa.Integer(), nullable=True),
        sa.Column("analytics_ids_json", sa.JSON(), nullable=False),
        sa.Column("signals_sha256", sa.String(length=64), nullable=True),
        sa.Column("text_sha256", sa.String(length=64), nullable=True),
        sa.Column("raw_sha256", sa.String(length=64), nullable=True),
        sa.Column("version_seq", sa.Integer(), nullable=False),
        sa.Column("gone_since", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_checked_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_monitor_pages_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitor_pages")),
        sa.UniqueConstraint("client_id", "url", name="uq_monitor_pages_client_id_url"),
    )
    op.create_index(
        op.f("ix_monitor_pages_client_id"), "monitor_pages", ["client_id"], unique=False
    )
    op.create_index(
        "ix_monitor_pages_client_last_checked",
        "monitor_pages",
        ["client_id", "last_checked_at"],
        unique=False,
    )
    op.create_table(
        "monitor_resources",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("status", sa.Integer(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("structure_json", sa.JSON(), nullable=False),
        sa.Column("version_seq", sa.Integer(), nullable=False),
        sa.Column("missing_since", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("last_checked_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_monitor_resources_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitor_resources")),
        sa.UniqueConstraint("client_id", "url", name="uq_monitor_resources_client_id_url"),
    )
    op.create_index(
        op.f("ix_monitor_resources_client_id"), "monitor_resources", ["client_id"], unique=False
    )
    op.create_table(
        "monitor_runs",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("started_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("finished_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("urls_checked", sa.Integer(), nullable=False),
        sa.Column("urls_changed", sa.Integer(), nullable=False),
        sa.Column("urls_new", sa.Integer(), nullable=False),
        sa.Column("urls_gone", sa.Integer(), nullable=False),
        sa.Column("requests_made", sa.Integer(), nullable=False),
        sa.Column("bytes_downloaded", sa.Integer(), nullable=False),
        sa.Column("changes_critical", sa.Integer(), nullable=False),
        sa.Column("changes_warning", sa.Integer(), nullable=False),
        sa.Column("changes_info", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("remaining_count", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("notes_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_monitor_runs_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitor_runs")),
    )
    op.create_index(op.f("ix_monitor_runs_client_id"), "monitor_runs", ["client_id"], unique=False)
    op.create_index(
        "ix_monitor_runs_client_started", "monitor_runs", ["client_id", "started_at"], unique=False
    )
    op.create_index(op.f("ix_monitor_runs_status"), "monitor_runs", ["status"], unique=False)
    op.create_table(
        "monitor_settings",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("base_url_override", sa.String(length=500), nullable=True),
        sa.Column("light_interval_hours", sa.Float(), nullable=True),
        sa.Column("full_interval_hours", sa.Float(), nullable=True),
        sa.Column("max_urls", sa.Integer(), nullable=True),
        sa.Column("monitor_when_paused", sa.Boolean(), nullable=False),
        sa.Column("ignore_patterns_json", sa.JSON(), nullable=False),
        sa.Column("muted_change_types_json", sa.JSON(), nullable=False),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_monitor_settings_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitor_settings")),
        sa.UniqueConstraint("client_id", name="uq_monitor_settings_client_id"),
    )
    op.create_table(
        "monitor_changes",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("run_id", app.db.base.GUID(length=36), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("change_type", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("subject", sa.String(length=2048), nullable=False),
        sa.Column("related_id", sa.String(length=64), nullable=True),
        sa.Column("old_value", sa.String(length=2000), nullable=True),
        sa.Column("new_value", sa.String(length=2000), nullable=True),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("detected_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("acknowledged_at", app.db.base.UTCDateTime(), nullable=True),
        sa.Column("acknowledged_note", sa.String(length=500), nullable=True),
        sa.Column("resolved_at", app.db.base.UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_monitor_changes_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["monitor_runs.id"],
            name=op.f("fk_monitor_changes_run_id_monitor_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitor_changes")),
    )
    op.create_index(
        "ix_monitor_changes_client_detected",
        "monitor_changes",
        ["client_id", "detected_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_monitor_changes_client_id"), "monitor_changes", ["client_id"], unique=False
    )
    op.create_index(
        "ix_monitor_changes_client_severity_state",
        "monitor_changes",
        ["client_id", "severity", "resolved_at"],
        unique=False,
    )
    op.create_table(
        "monitor_dns_versions",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("record_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("observed_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("values_json", sa.JSON(), nullable=False),
        sa.Column("ttl", sa.Integer(), nullable=True),
        sa.Column("resolver", sa.String(length=100), nullable=True),
        sa.Column("prev_version_id", app.db.base.GUID(length=36), nullable=True),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_monitor_dns_versions_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["prev_version_id"],
            ["monitor_dns_versions.id"],
            name=op.f("fk_monitor_dns_versions_prev_version_id_monitor_dns_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["record_id"],
            ["monitor_dns_records.id"],
            name=op.f("fk_monitor_dns_versions_record_id_monitor_dns_records"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitor_dns_versions")),
    )
    op.create_index(
        op.f("ix_monitor_dns_versions_record_id"),
        "monitor_dns_versions",
        ["record_id"],
        unique=False,
    )
    op.create_index(
        "ix_monitor_dns_versions_record_observed",
        "monitor_dns_versions",
        ["record_id", "observed_at"],
        unique=False,
    )
    op.create_table(
        "monitor_page_versions",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("page_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("observed_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("signals_json", sa.JSON(), nullable=False),
        sa.Column("signals_sha256", sa.String(length=64), nullable=True),
        sa.Column("text_sha256", sa.String(length=64), nullable=True),
        sa.Column("raw_sha256", sa.String(length=64), nullable=True),
        sa.Column("body_blob_sha256", sa.String(length=64), nullable=True),
        sa.Column("changed_fields_json", sa.JSON(), nullable=False),
        sa.Column("prev_version_id", app.db.base.GUID(length=36), nullable=True),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["body_blob_sha256"],
            ["monitor_blobs.sha256"],
            name=op.f("fk_monitor_page_versions_body_blob_sha256_monitor_blobs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_monitor_page_versions_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["page_id"],
            ["monitor_pages.id"],
            name=op.f("fk_monitor_page_versions_page_id_monitor_pages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["prev_version_id"],
            ["monitor_page_versions.id"],
            name=op.f("fk_monitor_page_versions_prev_version_id_monitor_page_versions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitor_page_versions")),
    )
    op.create_index(
        op.f("ix_monitor_page_versions_page_id"), "monitor_page_versions", ["page_id"], unique=False
    )
    op.create_index(
        "ix_monitor_page_versions_page_observed",
        "monitor_page_versions",
        ["page_id", "observed_at"],
        unique=False,
    )
    op.create_table(
        "monitor_resource_versions",
        sa.Column("id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("resource_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("client_id", app.db.base.GUID(length=36), nullable=False),
        sa.Column("observed_at", app.db.base.UTCDateTime(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("structure_json", sa.JSON(), nullable=False),
        sa.Column("changed_fields_json", sa.JSON(), nullable=False),
        sa.Column("prev_version_id", app.db.base.GUID(length=36), nullable=True),
        sa.Column("created_at", app.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_monitor_resource_versions_client_id_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["prev_version_id"],
            ["monitor_resource_versions.id"],
            name=op.f("fk_monitor_resource_versions_prev_version_id_monitor_resource_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["resource_id"],
            ["monitor_resources.id"],
            name=op.f("fk_monitor_resource_versions_resource_id_monitor_resources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitor_resource_versions")),
    )
    op.create_index(
        op.f("ix_monitor_resource_versions_resource_id"),
        "monitor_resource_versions",
        ["resource_id"],
        unique=False,
    )
    op.create_index(
        "ix_monitor_resource_versions_resource_observed",
        "monitor_resource_versions",
        ["resource_id", "observed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_monitor_resource_versions_resource_observed", table_name="monitor_resource_versions"
    )
    op.drop_index(
        op.f("ix_monitor_resource_versions_resource_id"), table_name="monitor_resource_versions"
    )
    op.drop_table("monitor_resource_versions")
    op.drop_index("ix_monitor_page_versions_page_observed", table_name="monitor_page_versions")
    op.drop_index(op.f("ix_monitor_page_versions_page_id"), table_name="monitor_page_versions")
    op.drop_table("monitor_page_versions")
    op.drop_index("ix_monitor_dns_versions_record_observed", table_name="monitor_dns_versions")
    op.drop_index(op.f("ix_monitor_dns_versions_record_id"), table_name="monitor_dns_versions")
    op.drop_table("monitor_dns_versions")
    op.drop_index("ix_monitor_changes_client_severity_state", table_name="monitor_changes")
    op.drop_index(op.f("ix_monitor_changes_client_id"), table_name="monitor_changes")
    op.drop_index("ix_monitor_changes_client_detected", table_name="monitor_changes")
    op.drop_table("monitor_changes")
    op.drop_table("monitor_settings")
    op.drop_index(op.f("ix_monitor_runs_status"), table_name="monitor_runs")
    op.drop_index("ix_monitor_runs_client_started", table_name="monitor_runs")
    op.drop_index(op.f("ix_monitor_runs_client_id"), table_name="monitor_runs")
    op.drop_table("monitor_runs")
    op.drop_index(op.f("ix_monitor_resources_client_id"), table_name="monitor_resources")
    op.drop_table("monitor_resources")
    op.drop_index("ix_monitor_pages_client_last_checked", table_name="monitor_pages")
    op.drop_index(op.f("ix_monitor_pages_client_id"), table_name="monitor_pages")
    op.drop_table("monitor_pages")
    op.drop_index(op.f("ix_monitor_dns_records_client_id"), table_name="monitor_dns_records")
    op.drop_table("monitor_dns_records")
    op.drop_table("monitor_blobs")
