from __future__ import annotations

import pytest
from harness import build_test_engine
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def engine():
    # task-75 §9: shared, fast-reset Postgres engine (schema created once
    # per pytest process, `TRUNCATE`d between tests) — see
    # `tests/harness/db.py:build_test_engine` for why this replaced the
    # old per-test SQLite file.
    eng = build_test_engine()
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine) -> Session:
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as s:
        yield s


@pytest.fixture
def client_row(session: Session):
    """Мінімальний активний клієнт — усе в цьому пакеті прив'язане до нього."""
    from app.db.models import Client
    from app.schemas.enums import ClientStatus

    row = Client(name="TESTCO", slug="testco", domain="x.test", status=ClientStatus.active)
    session.add(row)
    session.commit()
    return row
