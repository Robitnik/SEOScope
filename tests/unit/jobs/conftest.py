from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db import models  # noqa: F401 - registers tables on Base.metadata
from app.db.base import Base
from app.db.repositories.cells import CellRepository
from app.db.repositories.jobs import JobRepository
from app.db.session import build_sync_engine
from app.integrations import registry
from app.jobs.fake_adapter import FakeAdapter
from app.schemas.enums import ComparisonMode, PeriodKind, Provider


@pytest.fixture
def engine(tmp_path):
    db_path = tmp_path / "test.db"
    settings = Settings(_env_file=None, database_url=f"sqlite:///{db_path}")
    eng = build_sync_engine(settings)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_scope(engine):
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def _scope() -> Generator[Session, None, None]:
        session = maker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return _scope


@pytest.fixture(autouse=True)
def _clean_adapter_registry(monkeypatch):
    """`registry` is a module-level singleton (by design: one worker process,
    PLAN §5.1) — tests reset it directly so registrations don't leak/collide
    across tests, without needing a production reset API.
    """
    monkeypatch.setattr(registry, "_registry", {})
    yield


@pytest.fixture
def register_fake(_clean_adapter_registry):
    def _register(provider: Provider, adapter: FakeAdapter | None = None) -> FakeAdapter:
        fake = adapter or FakeAdapter(provider)
        registry.register_adapter(fake)
        return fake

    return _register


@pytest.fixture
def make_cell(session_scope):
    """Create one client + integration + job + `(client, provider)` cell,
    returning `(job_id, cell_id, client_id)`. `integration_config` drives the
    `FakeAdapter` scenario for that cell.
    """

    def _make(
        provider: Provider = Provider.ahrefs,
        *,
        scenario: str = "success",
        stage_seconds: float = 0.0,
        no_integration: bool = False,
        integration_enabled: bool = True,
        mode: str = "collect_missing",
        slug: str = "acme",
        period_kind: PeriodKind = PeriodKind.week,
        custom_start: date | None = None,
        custom_end: date | None = None,
    ) -> tuple[str, str, str]:
        from app.db.models import Client, ClientIntegration

        with session_scope() as session:
            client = Client(
                name="Acme", slug=slug, domain=f"{slug}.com", timezone="America/New_York"
            )
            session.add(client)
            session.flush()

            if not no_integration:
                session.add(
                    ClientIntegration(
                        client_id=client.id,
                        provider=provider,
                        enabled=integration_enabled,
                        config_json={"scenario": scenario, "stage_seconds": stage_seconds},
                    )
                )
                session.flush()

            jobs = JobRepository(session).create_run(
                client_ids=[client.id],
                providers=[provider],
                period_kind=period_kind,
                comparison_mode=ComparisonMode.closed,
                anchor_date=date(2026, 1, 1),
                custom_start=custom_start,
                custom_end=custom_end,
                mode=mode,
            )
            job = jobs[0]
            cell = CellRepository(session).list_for_job(job.id)[0]
            return job.id, cell.id, client.id

    return _make
