"""Repository layer (PLAN §6): the only place that runs queries against the ORM.

All repositories are written against a plain synchronous `sqlalchemy.orm.Session`
— this is the one implementation both runtimes share (CLAUDE.md: "the repository
layer that both the API and the worker operate through"):

- The **worker** is a sync process (PLAN §5.1) and calls repositories directly
  with a `Session` from `app.db.session.worker_session_scope()`.
- The **API** is async and calls the same repository methods through
  `AsyncSession.run_sync(...)`, SQLAlchemy's documented bridge for running sync
  ORM code against an async session's underlying connection in a worker thread:

    async with get_db() as session:
        clients = await session.run_sync(lambda s: ClientRepository(s).list_active())

This keeps query logic in one place instead of a duplicated sync/async pair.
"""

from __future__ import annotations

from app.db.repositories.artifacts import ArtifactRepository
from app.db.repositories.cells import CellRepository
from app.db.repositories.clients import ClientRepository
from app.db.repositories.events import EventRepository
from app.db.repositories.imports import ImportRepository
from app.db.repositories.jobs import JobRepository
from app.db.repositories.metrics import MetricRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.db.repositories.steps import StepRepository

__all__ = [
    "ArtifactRepository",
    "CellRepository",
    "ClientRepository",
    "EventRepository",
    "ImportRepository",
    "JobRepository",
    "MetricRepository",
    "SnapshotRepository",
    "StepRepository",
]
