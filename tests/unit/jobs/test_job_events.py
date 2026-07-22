"""`app.jobs.events`: redacted persisted progress + the SSE polling loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from app.db.repositories.cells import CellRepository
from app.db.repositories.events import EventRepository
from app.jobs.events import event_stream, make_emitter
from app.schemas.adapter import ProgressEvent
from app.schemas.enums import Provider


def test_emit_persists_event_and_updates_cell_progress(session_scope, make_cell):
    job_id, cell_id, _client_id = make_cell(Provider.ahrefs)
    with session_scope() as session:
        CellRepository(session).claim_next_cell(Provider.ahrefs, lease_owner="w1")
    emit = make_emitter(session_scope, job_id=job_id, cell_id=cell_id, lease_owner="w1")

    emit(ProgressEvent(provider=Provider.ahrefs, stage="fetch_current", percent=42.0, message="ok"))

    with session_scope() as session:
        events = EventRepository(session).since(job_id, 0)
        assert len(events) == 1
        assert events[0].message == "ok"
        cell = CellRepository(session).get(cell_id)
        assert cell.progress_percent == 42.0
        assert cell.current_stage == "fetch_current"
        assert cell.heartbeat_at is not None


def test_emit_redacts_secrets_in_message_and_details(session_scope, make_cell):
    job_id, cell_id, _client_id = make_cell(Provider.ahrefs)
    emit = make_emitter(session_scope, job_id=job_id, cell_id=cell_id, lease_owner="w1")

    emit(
        ProgressEvent(
            provider=Provider.ahrefs,
            stage="authenticate",
            message="Authorization: Bearer sk-abcdef1234567890",
            details={"api_key": "super-secret", "nested": {"cookie": "session=xyz"}},
        )
    )

    with session_scope() as session:
        event = EventRepository(session).since(job_id, 0)[0]
        assert "sk-abcdef1234567890" not in event.message
        assert event.details_json["api_key"] == "***REDACTED***"
        assert event.details_json["nested"]["cookie"] == "***REDACTED***"


def test_emit_never_raises_when_session_scope_is_broken(make_cell):
    job_id, cell_id, _client_id = "missing-job", "missing-cell", "missing-client"

    def _broken_scope():
        raise RuntimeError("db is down")

    emit = make_emitter(_broken_scope, job_id=job_id, cell_id=cell_id, lease_owner="w1")
    # must not raise
    emit(ProgressEvent(provider=Provider.ahrefs, stage="fetch_current", message="ok"))


@dataclass
class _FakeEvent:
    seq: int
    message: str
    provider: Provider | None = None
    stage: str | None = None
    percent: float | None = None
    level: str = "info"
    details_json: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@pytest.mark.asyncio
async def test_event_stream_replays_since_last_event_id_and_stops_when_done():
    all_events = [_FakeEvent(seq=i, message=f"m{i}") for i in range(1, 4)]

    async def fetch_since(last_seq: int) -> list[_FakeEvent]:
        return [e for e in all_events if e.seq > last_seq]

    calls = {"n": 0}

    async def is_done() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # done right after the first empty poll

    chunks = []
    async for chunk in event_stream(
        fetch_since,
        last_event_id=1,
        poll_interval_seconds=0.01,
        keepalive_seconds=100,
        is_done=is_done,
    ):
        chunks.append(chunk)

    assert len(chunks) == 2  # events with seq 2 and 3 only (replay from last_event_id=1)
    assert b"id: 2" in chunks[0]
    assert b"id: 3" in chunks[1]


@pytest.mark.asyncio
async def test_event_stream_sends_keepalive_when_idle():
    async def fetch_since(_last_seq: int) -> list[_FakeEvent]:
        return []

    calls = {"n": 0}

    async def is_done() -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    chunks = []
    async for chunk in event_stream(
        fetch_since, poll_interval_seconds=0.01, keepalive_seconds=0.02, is_done=is_done
    ):
        chunks.append(chunk)

    assert b": keepalive\n\n" in chunks
