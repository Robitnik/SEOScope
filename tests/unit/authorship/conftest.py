from __future__ import annotations

import pytest
from harness import build_test_engine
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def engine():
    # Same shared, fast-reset Postgres engine as `tests/unit/db/conftest.py`
    # (task-75 §9) — `test_backfill.py` needs real `ReportRun`/`ReportBatch`/
    # `ContentQaPackage`/`AuditEvent` rows through the ORM, same as the
    # repository tests in `tests/unit/db/`.
    eng = build_test_engine()
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine) -> Session:
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as s:
        yield s
