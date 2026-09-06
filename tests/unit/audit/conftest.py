from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.audit.sink import reset_audit_sink_for_tests
from app.core.config import get_settings
from app.db import models  # noqa: F401 - реєструє таблиці на Base.metadata
from app.db import session as db_session
from app.db.base import Base


@pytest.fixture
def db(tmp_path, monkeypatch) -> Iterator[Session]:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}")
    monkeypatch.setenv("AUDIT_FALLBACK_PATH", str(tmp_path / "audit_fallback.jsonl"))
    db_session._async_engine = None
    db_session._async_sessionmaker = None
    db_session._sync_engine = None
    db_session._sync_sessionmaker = None
    get_settings.cache_clear()
    reset_audit_sink_for_tests()

    Base.metadata.create_all(db_session.get_sync_engine())
    maker = sessionmaker(bind=db_session.get_sync_engine(), expire_on_commit=False)
    with maker() as session:
        yield session

    reset_audit_sink_for_tests()
    db_session._sync_engine = None
    db_session._sync_sessionmaker = None
    get_settings.cache_clear()
