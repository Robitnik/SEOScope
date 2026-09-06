"""EventRepository: monotonic `seq`, `since` replay, retention purge."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.db.models import Client, JobEvent, SyncJob
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


def _make_event(session, job_id: str, *, timestamp: datetime) -> JobEvent:
    """Bypass `EventRepository.append()` (always stamps `utcnow()`) to
    construct a row with an explicit `timestamp` — task-75 §3.3's tests
    need to control "how old is this row", which `append()` deliberately
    does not expose (nothing else needs to backdate an event).
    """
    event = JobEvent(job_id=job_id, level="info", message="x", timestamp=timestamp)
    session.add(event)
    session.flush()
    assert event.seq is not None
    return event


def test_since_with_safe_before_excludes_events_newer_than_the_window(session):
    """task-75 §3.3: this is the mechanism that closes the multi-writer
    SSE cursor race — `safe_before` must actually filter by `timestamp`,
    independent of `seq` order, or it does nothing.
    """
    job = _make_job(session)
    repo = EventRepository(session)
    now = datetime.now(UTC)
    old_event = _make_event(session, job.id, timestamp=now - timedelta(seconds=1))
    new_event = _make_event(session, job.id, timestamp=now)

    safe_before = now - timedelta(milliseconds=200)
    replayed = repo.since(job.id, 0, safe_before=safe_before)

    assert [e.seq for e in replayed] == [old_event.seq]
    assert new_event.seq not in [e.seq for e in replayed]


def test_since_without_safe_before_is_unchanged(session):
    """The old, unfiltered behaviour is still there for a caller that
    does not pass `safe_before` — correct on its own under exactly one
    writer, and what `has_pending` itself relies on.
    """
    job = _make_job(session)
    repo = EventRepository(session)
    seqs = [repo.append(job.id, "info", message=f"step {i}") for i in range(3)]

    assert [e.seq for e in repo.since(job.id, 0)] == seqs


def test_has_pending_ignores_the_safety_window(session):
    """task-75 §3.3: a row that exists but has not aged past the safety
    window must still count as "pending" — this is what stops
    `stream_sync_job_events` from declaring the stream finished before
    its own last event(s) ever got a chance to be delivered.
    """
    job = _make_job(session)
    repo = EventRepository(session)
    event = _make_event(session, job.id, timestamp=datetime.now(UTC))

    safe_before = datetime.now(UTC) - timedelta(milliseconds=200)
    assert repo.since(job.id, 0, safe_before=safe_before) == []  # not yet safe to serve
    assert repo.has_pending(job.id, 0) is True  # but it does exist
    assert repo.has_pending(job.id, event.seq) is False  # nothing PAST it, though
