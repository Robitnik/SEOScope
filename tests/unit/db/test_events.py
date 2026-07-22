"""EventRepository: monotonic `seq`, `since` replay, retention purge."""

from __future__ import annotations

from datetime import date

from app.db.models import Client, SyncJob
from app.db.repositories.events import EventRepository
from app.schemas.enums import ComparisonMode, PeriodKind


def _make_job(session) -> SyncJob:
    client = Client(name="Acme", slug="acme", domain="acme.com")
    session.add(client)
    session.commit()
    job = SyncJob(
        client_id=client.id,
        period_kind=PeriodKind.week,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 1, 1),
    )
    session.add(job)
    session.commit()
    return job


def test_append_returns_monotonic_seq(session):
    job = _make_job(session)
    repo = EventRepository(session)

    seqs = [repo.append(job.id, "info", message=f"step {i}") for i in range(5)]

    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 5


def test_since_filters_by_last_seq(session):
    job = _make_job(session)
    repo = EventRepository(session)
    seqs = [repo.append(job.id, "info", message=f"step {i}") for i in range(5)]

    replayed = repo.since(job.id, seqs[1])

    assert [e.seq for e in replayed] == seqs[2:]


def test_purge_older_than_removes_old_events(session, monkeypatch):
    job = _make_job(session)
    repo = EventRepository(session)
    repo.append(job.id, "info", message="old enough to purge")

    deleted = repo.purge_older_than(days=-1)  # cutoff in the future: everything qualifies

    assert deleted == 1
    assert repo.since(job.id, 0) == []
