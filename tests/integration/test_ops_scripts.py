"""Ops-скрипти як гейт, а не як «є файл» (PLAN §18: «є backup/restore інструкція»).

Інструкція, яку ніхто не проганяв, не рахується. Тут backup і restore
проходять повний цикл на справжній БД із даними, а retention-чистка
перевіряється на межі: старе прибирається, свіже — ні, snapshot-и й метрики
не чіпаються ніколи.

Скрипти підвантажуються по шляху (`scripts/` — не пакет), щоб тест ганяв
рівно той файл, який запускає оператор.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from harness import (
    FakeScenario,
    register_fakes,
    reset_registry,
    running_worker,
    use_temp_sqlite_database,
    wait_for,
)
from sqlalchemy import func, select

from app.db.models import Client, ClientIntegration, JobEvent, MetricValue, ProviderSnapshot
from app.db.repositories.jobs import JobRepository
from app.schemas.enums import ComparisonMode, PeriodKind, Provider

pytestmark = pytest.mark.integration

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(script_name: str):
    spec = importlib.util.spec_from_file_location(script_name, SCRIPTS / f"{script_name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Локально перевизначає кореневий (тепер Postgres-бекований, task-75
    §9) `temp_db` — рівно тим самим ім'ям, для тестів у цьому файлі. Ops-
    скрипти (`scripts/backup_sqlite.py`/`restore_sqlite.py`) працюють
    напряму з sqlite3-файлом і лишаються sqlite-специфічними інструментами
    (task-75 §8 — `pg_dump`-бекап ще не зроблено), тож саме sqlite тут і
    треба перевіряти, незалежно від бекенду решти застосунку.
    """
    with use_temp_sqlite_database(tmp_path, monkeypatch) as database:
        yield database


@pytest.fixture
def fake_adapters_local(monkeypatch):
    reset_registry(monkeypatch)
    return register_fakes()


@pytest.fixture
def populated_db(temp_db, fake_adapters_local):
    """БД із реальним завершеним запуском: клієнт, події, snapshot, метрики.

    Власний, локальний `seed_client`-еквівалент (не кореневий фікстур,
    який тепер сидить на Postgres-бекованому `temp_db`) — цей файл сам
    перевизначив `temp_db` вище на sqlite, тож і клієнта заводить сам,
    проти того самого sqlite `temp_db`.
    """
    with temp_db.session_scope() as session:
        client = Client(name="Acme", slug="acme", domain="acme.com")
        session.add(client)
        session.flush()
        session.add(
            ClientIntegration(
                client_id=client.id,
                provider=Provider.ahrefs,
                enabled=True,
                config_json=FakeScenario().as_config(),
            )
        )
        client_id = client.id
    with temp_db.session_scope() as session:
        job = JobRepository(session).create_run(
            client_ids=[client_id],
            providers=[Provider.ahrefs],
            period_kind=PeriodKind.week,
            comparison_mode=ComparisonMode.closed,
            anchor_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        )[0]
        job_id = job.id

    with running_worker(temp_db.session_scope, providers=(Provider.ahrefs,)):
        wait_for(
            lambda: _count(temp_db, ProviderSnapshot) == 1, timeout=30, what="snapshot записано"
        )
    return temp_db, client_id, job_id


def _count(temp_db, model) -> int:
    with temp_db.session_scope() as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def test_backup_then_restore_round_trip_preserves_data(populated_db, tmp_path):
    temp_db, client_id, _ = populated_db
    backup = _load("backup_sqlite")
    restore = _load("restore_sqlite")

    out_dir = tmp_path / "backups"
    exit_code = backup.main(
        [
            "--database-url",
            f"sqlite:///{temp_db.path}",
            "--artifacts-dir",
            str(temp_db.artifacts_dir),
            "--out-dir",
            str(out_dir),
        ]
    )
    assert exit_code == 0
    snapshots = list(out_dir.glob("app-*.db"))
    assert len(snapshots) == 1
    assert snapshots[0].with_suffix(".db.sha256").is_file()

    # Катастрофа: робоча база зникла разом із WAL.
    for suffix in ("", "-wal", "-shm"):
        Path(str(temp_db.path) + suffix).unlink(missing_ok=True)

    assert (
        restore.main(["--from", str(snapshots[0]), "--database-url", f"sqlite:///{temp_db.path}"])
        == 1
    ), "без --force відновлення не має нічого чіпати"
    assert not temp_db.path.exists()

    assert (
        restore.main(
            [
                "--from",
                str(snapshots[0]),
                "--database-url",
                f"sqlite:///{temp_db.path}",
                "--force",
            ]
        )
        == 0
    )

    connection = sqlite3.connect(f"file:{temp_db.path}?mode=ro", uri=True)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        restored_clients = connection.execute("SELECT id FROM clients").fetchall()
        metrics = connection.execute("SELECT COUNT(*) FROM metric_values").fetchone()[0]
    finally:
        connection.close()

    assert [row[0] for row in restored_clients] == [client_id]
    assert metrics > 0, "метрики мають пережити відновлення"


def test_backup_refuses_a_corrupted_source(temp_db, tmp_path):
    restore = _load("restore_sqlite")
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"definitely not a sqlite file")

    with pytest.raises(SystemExit):
        restore.verify_backup(broken)


def test_backup_rotation_keeps_only_the_newest(populated_db, tmp_path):
    temp_db, _, _ = populated_db
    backup = _load("backup_sqlite")
    out_dir = tmp_path / "backups"
    out_dir.mkdir()

    for stamp in ("20260701T000000Z", "20260702T000000Z", "20260703T000000Z"):
        (out_dir / f"app-{stamp}.db").write_bytes(b"")
        (out_dir / f"app-{stamp}.db.sha256").write_text("x\n")
        (out_dir / f"artifacts-{stamp}.tar.gz").write_bytes(b"")

    backup.rotate(out_dir, keep=2)

    assert sorted(p.name for p in out_dir.glob("app-*.db")) == [
        "app-20260702T000000Z.db",
        "app-20260703T000000Z.db",
    ]
    assert not (out_dir / "artifacts-20260701T000000Z.tar.gz").exists(), (
        "ротація має прибирати архів artifacts разом із його базою"
    )
    assert not (out_dir / "app-20260701T000000Z.db.sha256").exists()


def test_retention_cleanup_removes_only_old_events(populated_db, capsys):
    temp_db, _, job_id = populated_db
    retention = _load("retention_cleanup")

    fresh_events = _count(temp_db, JobEvent)
    assert fresh_events > 0

    with temp_db.session_scope() as session:
        session.add(
            JobEvent(
                job_id=job_id,
                timestamp=datetime.now(UTC) - timedelta(days=120),
                level="info",
                message="дуже стара подія",
            )
        )

    assert retention.main(["--days", "30", "--dry-run"]) == 0
    assert "dry-run" in capsys.readouterr().out
    assert _count(temp_db, JobEvent) == fresh_events + 1, "dry-run нічого не видаляє"

    assert retention.main(["--days", "30"]) == 0
    assert _count(temp_db, JobEvent) == fresh_events
    assert _count(temp_db, ProviderSnapshot) == 1, "snapshot-и не є предметом retention"
    assert _count(temp_db, MetricValue) > 0, "метрики не є предметом retention"
    assert _count(temp_db, Client) == 1


def test_retention_cleanup_refuses_a_dangerously_small_window(temp_db):
    retention = _load("retention_cleanup")
    assert retention.main(["--days", "1"]) == 2
