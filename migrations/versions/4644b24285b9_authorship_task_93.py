"""authorship task 93

Revision ID: 4644b24285b9
Revises: bef57326e291
Create Date: 2026-09-14 09:19:01.327937

Who created a report run/batch or a Strategy Package Check package
(task-93 plan §2, decisions D1/D2). Two nullable columns per table:

- `created_by_user_id` (`GUID`, FK `users.id ON DELETE SET NULL`) — the real
  human, when there is one.
- `created_by_kind` (`String(16)`) — `user | agent | local | cli | system`,
  the same vocabulary as `audit_events.actor_kind` (task-72). `NULL` in both
  means "unknown" (a row written before this migration, or an ambiguous
  backfill candidate) — it is never guessed into `"system"`.

Direct `op.add_column`/`op.create_foreign_key`, no `batch_alter_table` (prod
is on Postgres since task-75 — adding nullable columns with no default and a
`SET NULL` FK is a catalog-only change there, CLAUDE.md). Backup taken before
running this: `docker compose exec -T db pg_dump -U yovth -Fc yovth >
app_data/backups/pre_task93_authorship_2026-09-14.dump`.
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
revision: str = "4644b24285b9"
down_revision: str | Sequence[str] | None = "bef57326e291"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("report_batches", "report_runs", "content_qa_packages")


def upgrade() -> None:
    """Upgrade schema."""
    for table in _TABLES:
        op.add_column(
            table, sa.Column("created_by_user_id", app.db.base.GUID(length=36), nullable=True)
        )
        op.add_column(
            table, sa.Column("created_by_kind", sa.String(length=16), nullable=True)
        )
        op.create_foreign_key(
            op.f(f"fk_{table}_created_by_user_id_users"),
            table,
            "users",
            ["created_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # `report_batches` stays at two rows for the foreseeable future (one
    # per portfolio-wide run) — a filter index would be pure overhead there.
    # `report_runs`/`content_qa_packages` back the author filter in
    # `GET /reports/runs` and `GET /tools/strategy-package/reports`
    # (task-93 plan §2 D4).
    op.create_index(
        "ix_report_runs_created_by_user_id_created_at",
        "report_runs",
        ["created_by_user_id", "created_at"],
    )
    op.create_index(
        "ix_content_qa_packages_created_by_user_id_created_at",
        "content_qa_packages",
        ["created_by_user_id", "created_at"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_content_qa_packages_created_by_user_id_created_at",
        table_name="content_qa_packages",
    )
    op.drop_index("ix_report_runs_created_by_user_id_created_at", table_name="report_runs")

    for table in _TABLES:
        op.drop_constraint(
            op.f(f"fk_{table}_created_by_user_id_users"), table, type_="foreignkey"
        )
        op.drop_column(table, "created_by_kind")
        op.drop_column(table, "created_by_user_id")
