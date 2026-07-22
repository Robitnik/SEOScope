"""Тимчасова SQLite для наскрізних тестів (task-13).

Ключова відмінність від локальних юніт-фікстур (`tests/unit/*/conftest.py`,
які роблять власний `build_sync_engine`): тут перемикаються **процесні
глобали** `app.db.session` і кеш `get_settings()`. Це обов'язково, бо API-
роутери (`app/api/jobs.py`) і job-engine ходять у БД саме через
`worker_session_scope()`/`get_db()`, а не через переданий ззовні engine —
інакше тест писав би в одну базу, а застосунок читав з іншої.
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


@dataclass(frozen=True)
class TempDatabase:
    """Одна ізольована БД + усе, чим до неї можна дістатись у тесті."""

    path: Path
    url: str
    engine: Engine
    session_factory: sessionmaker[Session]
    session_scope: Any  # zero-arg context-manager factory (як worker_session_scope)
    artifacts_dir: Path


def _reset_process_globals() -> None:
    """Скинути закешовані engine/sessionmaker/Settings.

    `app.db.session` тримає їх у модульних глобалах (один процес = один
    writer, PLAN §5.1), тому між тестами їх треба явно занулити — публічного
    API для reset немає й у проді він не потрібен.
    """
    from app.core.config import get_settings
    from app.db import session as db_session

    db_session._async_engine = None
    db_session._async_sessionmaker = None
    db_session._sync_engine = None
    db_session._sync_sessionmaker = None
    get_settings.cache_clear()


@contextmanager
def use_temp_database(
    tmp_path: Path, monkeypatch: Any, *, use_migrations: bool = False
) -> Iterator[TempDatabase]:
    """Підняти ізольовану БД і націлити на неї весь процес.

    `use_migrations=True` проганяє `alembic upgrade head` замість
    `create_all` — повільніше, зате перевіряє саме ту схему, яку отримає
    прод (PLAN §14 «readiness не green, доки schema не актуальна»).
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
    if use_migrations:
        _alembic_upgrade_head()
    else:
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
        yield TempDatabase(
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


def _alembic_upgrade_head() -> None:
    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(str(repo_root / "alembic.ini")), "head")
