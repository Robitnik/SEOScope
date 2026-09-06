"""Спільна тестова БД для наскрізних тестів (task-13, переведено на Postgres task-75 §9).

Ключова відмінність від локальних юніт-фікстур (`tests/unit/*/conftest.py`,
які роблять власний `build_sync_engine`): тут перемикаються **процесні
глобали** `app.db.session` і кеш `get_settings()`. Це обов'язково, бо API-
роутери (`app/api/jobs.py`) і job-engine ходять у БД саме через
`worker_session_scope()`/`get_db()`, а не через переданий ззовні engine —
інакше тест писав би в одну базу, а застосунок читав з іншої.

task-75 §9: після cutover продакшн — Postgres, тож і тести тепер ганяються
проти нього (`yovth_test`, окрема база на тому самому ізольованому
`db`-сервісі — ніколи не `yovth`, бойова). Схема створюється **один раз на
процес** (`Base.metadata.create_all`, 54 таблиці — надто дорого повторювати
на кожен виклик, як робив старий per-test SQLite-файл), а ізоляція між
тестами — `TRUNCATE ... RESTART IDENTITY CASCADE`: та сама гарантія
«нічого не існує до тесту», у рази дешевша за повторний `create_all`.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - типи лише для mypy
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from app.core.config import Settings


@dataclass(frozen=True)
class TempDatabase:
    """Одна ізольована БД + усе, чим до неї можна дістатись у тесті."""

    url: str
    engine: Engine
    session_factory: sessionmaker[Session]
    session_scope: Any  # zero-arg context-manager factory (як worker_session_scope)
    artifacts_dir: Path


_schema_ready = False


def test_database_url() -> str:
    """Похідна від бойового `DATABASE_URL` (`.env`) — той самий сервер/
    користувач/пароль/порт, назва бази замінена на `yovth_test`
    (task-75 §9). Тести й прод завжди на різних базах ОДНОГО ізольованого
    `db`-сервісу, навіть попри спільні креденшели — жодних окремих
    env-змінних заводити не треба.
    """
    from app.core.config import get_settings

    prod_url = get_settings().database_url
    if not prod_url.startswith("postgresql"):
        raise RuntimeError(
            f"test_database_url() очікує postgresql:// DATABASE_URL у .env, отримано: {prod_url!r}"
        )
    base, _, _dbname = prod_url.rpartition("/")
    return f"{base}/yovth_test"


def build_test_engine(settings: Settings | None = None) -> Engine:
    """One shared, fast-reset Postgres engine for a local module's own
    `engine`/`session` fixture (task-75 §9) — the ~10 `tests/unit/*/conftest.py`
    files that used to each build their own `sqlite:///{tmp_path}/test.db`
    engine directly now call this instead, with the same one-line-swap
    shape (still takes an optional `Settings` override for a module that
    needs one, e.g. a non-default `artifacts_dir`).

    Schema is created once per pytest process (`Base.metadata.create_all`,
    54 tables — too slow to repeat per test function, unlike the old
    per-test SQLite file this replaces). `TRUNCATE` runs on **every**
    call, including the first: `yovth_test` is a durable database that
    outlives any one `pytest` process, so "first call in this process"
    is not "nothing exists yet" — a previous, separate test run can
    (and, live-caught task-75 §9, did) leave rows behind for this one to
    collide with.
    """
    global _schema_ready
    from app.db import models  # noqa: F401 - реєструє таблиці на Base.metadata
    from app.db.base import Base
    from app.db.session import build_sync_engine

    if settings is None:
        from app.core.config import Settings

        settings = Settings(_env_file=None, database_url=test_database_url())
    engine = build_sync_engine(settings)
    if not _schema_ready:
        Base.metadata.create_all(engine)
        _schema_ready = True
    _truncate_all_tables(engine)
    return engine


def _truncate_all_tables(engine: Engine) -> None:
    from app.db.base import Base

    names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE")


def _reset_process_globals() -> None:
    """Скинути закешовані engine/sessionmaker/Settings.

    `app.db.session` тримає їх у модульних глобалах (один процес = один
    writer, PLAN §5.1), тому між тестами їх треба явно занулити — публічного
    API для reset немає й у проді він не потрібен.
    """
    from app.core.config import get_settings
    from app.db import session as db_session
    from app.db import writegate as db_writegate

    db_session._async_engine = None
    db_session._async_sessionmaker = None
    db_session._sync_engine = None
    db_session._sync_sessionmaker = None
    # task-66: `DbWriteGate` is ALSO a process-global singleton (its lock
    # path and slow-job settings are resolved once, at construction, from
    # whatever `Settings` was cached at the time) — without this reset it
    # would keep pointing at a PREVIOUS test's tmp database's lock file
    # instead of this one's, exactly the class of bug this function exists
    # to prevent for the engine/sessionmaker globals above.
    db_writegate.reset_write_gate_for_tests()
    get_settings.cache_clear()


@contextmanager
def use_temp_database(
    tmp_path: Path, monkeypatch: Any, *, use_migrations: bool = False
) -> Iterator[TempDatabase]:
    """Підняти ізольовану БД (Postgres, `yovth_test`) і націлити на неї весь процес.

    `use_migrations=True` проганяє `alembic upgrade head` замість
    `create_all` — повільніше, зате перевіряє саме той шлях, яким піде
    прод (staged PG baseline, task-75 §5). Обидва шляхи виробляють ту
    саму схему — інакше вони розходились би тихо; це саме той факт, що
    `test_pg_baseline_migration.py` перевіряє окремо (порожній
    autogenerate diff).
    """
    global _schema_ready
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    url = test_database_url()
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("ARTIFACTS_DIR", str(artifacts_dir))
    monkeypatch.setenv("BROWSER_PROFILES_DIR", str(tmp_path / "browser_profiles"))
    _reset_process_globals()

    from sqlalchemy.orm import sessionmaker

    from app.db import models  # noqa: F401 - реєструє таблиці на Base.metadata
    from app.db import session as db_session
    from app.db.base import Base

    engine = db_session.get_sync_engine()
    if not _schema_ready:
        if use_migrations:
            _alembic_upgrade_head()
        else:
            Base.metadata.create_all(engine)
        _schema_ready = True
    # Every call, including the first: `yovth_test` outlives any one
    # `pytest` process (see `build_test_engine`'s own docstring for the
    # live-caught reason this isn't just "else").
    _truncate_all_tables(engine)

    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope() -> Generator[Session, None, None]:
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    try:
        yield TempDatabase(
            url=url,
            engine=engine,
            session_factory=factory,
            session_scope=scope,
            artifacts_dir=artifacts_dir,
        )
    finally:
        engine.dispose()
        _reset_process_globals()


def _alembic_upgrade_head() -> None:
    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(str(repo_root / "alembic.ini")), "head")


@dataclass(frozen=True)
class TempSqliteDatabase:
    """Той самий контракт, що й `TempDatabase`, плюс `path` — для тестів,
    яким потрібен саме реальний sqlite-файл на диску (task-75 §9): ops-
    скрипти (`scripts/backup_sqlite.py`/`restore_sqlite.py`) працюють
    напряму з sqlite3-файлом і лишаються sqlite-специфічними навіть після
    переїзду прод-бази на Postgres (task-75 §8 — `pg_dump`-бекап ще не
    зроблено), тож їхні тести мають продовжувати перевіряти саме sqlite,
    незалежно від того, чим тепер бекнуто спільний `temp_db`.
    """

    path: Path
    url: str
    engine: Engine
    session_factory: sessionmaker[Session]
    session_scope: Any
    artifacts_dir: Path


@contextmanager
def use_temp_sqlite_database(
    tmp_path: Path, monkeypatch: Any
) -> Iterator[TempSqliteDatabase]:
    """Ізольований sqlite-файл, що й далі націлює на себе весь процес —
    буквально те, чим `use_temp_database` була до task-75 §9. Не для
    звичайних тестів (ті йдуть через Postgres-бекований `temp_db`) —
    лише для тестів самих sqlite-інструментів.
    """
    db_path = tmp_path / "yovth-test.db"
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("ARTIFACTS_DIR", str(artifacts_dir))
    monkeypatch.setenv("BROWSER_PROFILES_DIR", str(tmp_path / "browser_profiles"))
    _reset_process_globals()

    from sqlalchemy.orm import sessionmaker

    from app.db import models  # noqa: F401 - реєструє таблиці на Base.metadata
    from app.db import session as db_session
    from app.db.base import Base

    engine = db_session.get_sync_engine()
    Base.metadata.create_all(engine)

    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope() -> Generator[Session, None, None]:
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    try:
        yield TempSqliteDatabase(
            path=db_path,
            url=f"sqlite+aiosqlite:///{db_path}",
            engine=engine,
            session_factory=factory,
            session_scope=scope,
            artifacts_dir=artifacts_dir,
        )
    finally:
        engine.dispose()
        _reset_process_globals()
