"""task-75 §5.1 — the Postgres baseline migration
(`migrations/versions/9701790bda11_postgres_baseline.py`).

Was staged in `migrations/pg_baseline/`, deliberately outside
`migrations/versions/`, until cutover (task-75 §10 step 7) — see its own
module docstring for why (a second `down_revision = None` root next to
the 31 SQLite-era migrations would have given `alembic upgrade head` two
disconnected heads, breaking every `docker/entrypoint.sh` run against the
still-live SQLite database before cutover). Cutover moved it into
`migrations/versions/` and the 31 old ones into
`migrations/versions_sqlite_archive/`, in the same commit — this is now
the sole active revision. Still loaded here by path (`importlib`, same
precedent as `test_ops_scripts.py`/`test_migrate_sqlite_to_pg.py`), not
by import, since `migrations/versions/` is not itself an importable
package.

`upgrade()`/`downgrade()` are exercised via `MigrationContext`/
`Operations.context` — Alembic's own documented pattern for driving a
single migration's functions without a full `Config`/`script_location`
environment — against a throwaway SQLite engine. That proves the
*mechanics* (the calls succeed, the right tables appear and disappear)
without needing a live Postgres in the ordinary test run.

The claim this migration exists to prove — that applying it against
Postgres and then running `alembic revision --autogenerate` yields an
EMPTY diff, i.e. the baseline is byte-for-byte what the current models
would produce — is a Postgres-specific autogenerate-comparator question
that a SQLite engine cannot meaningfully answer (different type
affinity/reflection). That was live-verified instead, against the
`db` compose service (task-75 §5): `upgrade()` -> 0 diff operations
-> `downgrade()` -> 0 tables remaining. See the task plan for the
transcript.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

pytestmark = pytest.mark.integration

MIGRATION_FILE = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "9701790bda11_postgres_baseline.py"
)


@pytest.fixture
def baseline_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pg_baseline_migration", MIGRATION_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["pg_baseline_migration"] = module
    spec.loader.exec_module(module)
    return module


def test_upgrade_creates_every_table_downgrade_removes_them_all(baseline_migration, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'baseline.db'}")

    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            baseline_migration.upgrade()
        conn.commit()

    from app.db.base import Base

    inspector = inspect(engine)
    created = set(inspector.get_table_names())
    expected = set(Base.metadata.tables.keys())
    assert created == expected

    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            baseline_migration.downgrade()
        conn.commit()

    inspector = inspect(engine)
    assert inspector.get_table_names() == []
