"""task-75 §4 — `scripts/migrate_sqlite_to_pg.py`'s copy/reconciliation logic.

Exercised against two SQLite databases (a seeded source, an empty
target), not a live Postgres: every Postgres-only step
(`session_replication_role`, `pg_get_serial_sequence`/`setval`) is
dialect-guarded to a no-op against a non-Postgres target, so this runs
the exact same code path the real migration used live against Postgres
(task-75 §4, live-verified: 50 tables, 1 234 522 rows copied, sequence
`job_events.seq` aligned, zero row-count or checksum mismatches — see
that task's plan for the full transcript).

Scripts subdirectory is not a package (see `test_ops_scripts.py`, the
existing precedent for this loading pattern) — loaded by path so the
test runs the exact file an operator would invoke.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from harness import use_temp_sqlite_database
from sqlalchemy import create_engine, delete, update

from app.db.models import Client, ClientIntegration
from app.schemas.enums import Provider

pytestmark = pytest.mark.integration

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(script_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(script_name, SCRIPTS / f"{script_name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Unlike `test_ops_scripts.py`'s otherwise-identical loader: this
    # script's `@dataclass MigrationReport` needs its module registered
    # in `sys.modules` *before* `exec_module` — `dataclasses.py` resolves
    # string annotations via `sys.modules.get(cls.__module__)`, which is
    # `None` (crashing on `.__dict__`) for a module that was built but
    # never registered. `backup_sqlite`/`restore_sqlite` have no
    # dataclass, so the existing loader never hit this.
    sys.modules[script_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migrate_module() -> ModuleType:
    return _load("migrate_sqlite_to_pg")


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Локально перевизначає кореневий (тепер Postgres-бекований, task-75
    §9) `temp_db` — цей файл перевіряє саме sqlite-джерело міграційного
    скрипта, тож і фікстур має лишатись sqlite незалежно від бекенду
    решти застосунку (той самий підхід, що `test_ops_scripts.py`).
    """
    with use_temp_sqlite_database(tmp_path, monkeypatch) as database:
        yield database


@pytest.fixture
def seed_client(temp_db):
    """Мінімальний локальний еквівалент кореневого `seed_client` — цим
    тестам потрібен лише клієнт (+опційна інтеграція), не повний
    FakeAdapter/worker запуск.
    """

    def _seed(*, name: str = "Acme", slug: str = "acme", with_integration: bool = True) -> str:
        with temp_db.session_scope() as session:
            client = Client(name=name, slug=slug, domain=f"{slug}.example.com")
            session.add(client)
            session.flush()
            if with_integration:
                session.add(ClientIntegration(client_id=client.id, provider=Provider.ahrefs))
            return client.id

    return _seed


def test_integer_primary_key_columns_finds_exactly_job_events_seq(migrate_module):
    """The regression this guards against: the first version of
    `align_sequences` called `col.type.python_type`, which raises
    `NotImplementedError` for every GUID-keyed table (`GUID`/
    `DecimalString`/`UTCDateTime` in `app/db/base.py` are
    `TypeDecorator`s that never override it) — i.e. every table except
    this one. Caught live on the very first real run against Postgres,
    not by any test that existed before this one.
    """
    pairs = migrate_module.integer_primary_key_columns()
    names = {f"{table.name}.{col.name}" for table, col in pairs}
    assert names == {"job_events.seq"}


def test_copy_and_verify_round_trip(migrate_module, temp_db, seed_client, tmp_path):
    """The full pipeline (minus the schema step, done explicitly here to
    mirror `migrate()`'s own order) against a source seeded with a real
    client + integration: copy lands the exact row counts, sequence
    alignment is a safe no-op on a non-Postgres target, and both
    verification passes come back clean.
    """
    seed_client()

    source = create_engine(f"sqlite:///{temp_db.path}")
    target = create_engine(f"sqlite:///{tmp_path / 'target.db'}")

    migrate_module.create_schema(target)
    copied = migrate_module.copy_all_tables(source, target)
    assert copied["clients"] == 1
    assert copied["client_integrations"] == 1

    aligned = migrate_module.align_sequences(target)
    assert aligned == {}  # SQLite target: dialect-guarded no-op, not "nothing to align"

    assert migrate_module.verify_row_counts(source, target) == []
    assert migrate_module.verify_checksums(source, target) == []


def test_verify_row_counts_catches_a_missing_row(migrate_module, temp_db, seed_client, tmp_path):
    seed_client()
    source = create_engine(f"sqlite:///{temp_db.path}")
    target = create_engine(f"sqlite:///{tmp_path / 'target.db'}")
    migrate_module.create_schema(target)
    migrate_module.copy_all_tables(source, target)

    with target.begin() as conn:
        conn.execute(delete(Client))

    mismatches = migrate_module.verify_row_counts(source, target)
    assert ("clients", 1, 0) in mismatches


def test_verify_checksums_catches_a_changed_value(migrate_module, temp_db, seed_client, tmp_path):
    client_id = seed_client(name="Acme")
    source = create_engine(f"sqlite:///{temp_db.path}")
    target = create_engine(f"sqlite:///{tmp_path / 'target.db'}")
    migrate_module.create_schema(target)
    migrate_module.copy_all_tables(source, target)

    # Row counts alone would still match here — this is what count(*)
    # cannot catch (task-75 §4 step 7's whole reason for existing).
    with target.begin() as conn:
        conn.execute(update(Client).where(Client.id == client_id).values(name="Corrupted"))

    assert migrate_module.verify_row_counts(source, target) == []
    bad = migrate_module.verify_checksums(source, target, table_names=("clients",))
    assert len(bad) == 1
    assert "clients" in bad[0]
