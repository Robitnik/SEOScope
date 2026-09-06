"""postgres baseline (task-75 §5.1)

Revision ID: 9701790bda11
Revises:
Create Date: 2026-09-06T12:52:19.000000

STAGED, not yet active. Lives in `migrations/pg_baseline/`, a sibling of
the real `migrations/versions/` — deliberately outside
`alembic.ini`'s `script_location`, so it is invisible to `alembic upgrade
head` today and cannot create the "multiple independent heads" problem
that would otherwise break every `docker/entrypoint.sh` run against the
still-live SQLite database (this task has not cut over yet — see
task-75 §10). Activated at cutover (§10 step 7) by `git mv`-ing this
file into `migrations/versions/` at the same time the 31 SQLite-era
migrations move OUT into an archive directory — the two moves happen
together, never one without the other, or `alembic upgrade head` sees
two disconnected roots (this file's `down_revision = None`, and the old
chain's own root, `db1407dec7b0_initial_schema.py`, also
`down_revision = None`) and refuses to run.

`down_revision = None`: a new, independent root, not chained onto the
31 SQLite-era migrations. Replaying those against Postgres was
considered and rejected (task-75 §5.1) — 12 of them reference
`batch_alter_table`, a SQLite-only concern for a schema state that on
Postgres never existed, and each of the 31 steps is a needless chance
at surprise for zero benefit. The 31 files stay in `migrations/versions/`
as a historical record of the SQLite period, but this revision's own
ancestry starts here.

`upgrade()`/`downgrade()` delegate to `Base.metadata.create_all()` /
`drop_all()` rather than enumerating `op.create_table(...)` for 54
tables by hand (contrast `db1407dec7b0_initial_schema.py` above, which
does exactly that for the SQLite-era baseline). This is deliberate: the
schema this produces is the SAME schema
`scripts/migrate_sqlite_to_pg.py`'s `create_schema()` already builds and
was live-verified against (task-75 §4 — 50 tables, 1 234 522 rows, zero
mismatches). Defining this migration in terms of that same call, not a
hand-authored duplicate, is what makes "the baseline matches the
models" true by construction rather than by a separately-maintained DDL
listing that could silently drift from `app/db/models.py` the next time
someone edits a column. Live-verified (task-75 §5, not yet committed to
the versions/ history): applying this migration against the isolated
`db` service, then running `alembic revision --autogenerate` against
the result, produces an EMPTY diff — the DoD this task set for itself.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9701790bda11"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    from app.db import models  # noqa: F401 - registers every table on Base.metadata
    from app.db.base import Base

    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    """Downgrade schema."""
    from app.db import models  # noqa: F401 - registers every table on Base.metadata
    from app.db.base import Base

    Base.metadata.drop_all(op.get_bind())
