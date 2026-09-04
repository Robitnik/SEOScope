from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db import models  # noqa: F401 - registers tables on Base.metadata
from app.db.base import Base
from app.db.session import build_sync_engine


@pytest.fixture
def engine(tmp_path):
    db_path = tmp_path / "test.db"
    settings = Settings(_env_file=None, database_url=f"sqlite:///{db_path}")
    eng = build_sync_engine(settings)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine) -> Session:
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as s:
        yield s


@pytest.fixture
def artifacts_dir(tmp_path):
    path = tmp_path / "artifacts"
    path.mkdir()
    return path
