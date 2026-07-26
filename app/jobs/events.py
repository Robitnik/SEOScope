"""Persisted progress events + heartbeat (PLAN §9.2/§9.3, §13).

Every `ProgressEvent` an adapter emits during `collect()` is written here as
a `job_events` row (monotonic `seq`, doubling as the SSE id) plus a cell
progress/heartbeat update — this is the "continuous progress persistence"
invariant: a crash loses at most the one cell whose persist step never ran.

Redaction is defense-in-depth. `ProviderAdapter.collect()` is contractually
required to never put secrets in a `ProgressEvent` (`app/integrations/contract.py`),
but this is the last seam before something becomes a permanent DB row, so it
masks known-sensitive keys/patterns again rather than trusting the adapter.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy.orm import Session

from app.db.repositories.cells import CellRepository
from app.db.repositories.events import EventRepository
from app.schemas.adapter import ProgressEvent
from app.schemas.enums import CellStatus, Provider

_REDACT_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "password",
    "api_key",
    "apikey",
    "api-key",
    "token",
    "secret",
    "session",
    "session_path",
    "session_paths",
}

_REDACT_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*[^\s,;]+"),
)


def _redact_text(value: str) -> str:
    for pattern in _REDACT_PATTERNS:
        value = pattern.sub("***REDACTED***", value)
    return value


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {
            k: ("***REDACTED***" if k.lower() in _REDACT_KEYS else _redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(_redact_value(v) for v in value)
    return value


SessionScope = Callable[[], "Generator[Session, None, None] | Any"]


def make_emitter(
    session_scope: Any,
    *,
    job_id: str,
    cell_id: str,
    lease_owner: str,
    lease_ttl_seconds: int = 300,
) -> Callable[[ProgressEvent], None]:
    """Build the `emit` callable a `ProviderAdapter.collect()` call receives.

    `session_scope` is a zero-arg context manager factory yielding a short
    sync `Session` (`app.db.session.worker_session_scope` in production) —
    each event is its own short atomic-persist transaction, never sharing one
    with the network/browser work happening around it (PLAN §5.1/§9.1).
    Exceptions are swallowed: `emit` is best-effort per the adapter contract,
    and a broken event pipe must never break collection itself.
    """

    def _emit(event: ProgressEvent) -> None:
        try:
            message = _redact_text(event.message)
            details = _redact_value(event.details)
            with session_scope() as session:
                EventRepository(session).append(
                    job_id,
                    event.level,
                    provider=event.provider,
                    stage=event.stage,
                    percent=event.percent,
                    message=message,
                    details=details,
                )
                cells = CellRepository(session)
                cells.set_progress(
                    cell_id, event.percent, current_stage=event.stage,
                    progress_message=message or None,
                )
                cells.heartbeat(
                    cell_id, lease_owner=lease_owner, lease_ttl_seconds=lease_ttl_seconds
                )
        except Exception:  # noqa: BLE001 - emit must never break collection
            pass

    return _emit


# Stage/level a cell-status transition is reported under. `stage` is
# free-form by contract, but these names stay inside `STAGE_ORDER` so the UI
# can keep ordering them alongside adapter-emitted stages.
_CELL_STATUS_STAGE = {
    CellStatus.queued: "queued",
    CellStatus.not_configured: "preflight",
    CellStatus.skipped: "preflight",
    CellStatus.awaiting_auth: "authenticate",
}

_CELL_STATUS_LEVEL = {
    CellStatus.failed: "error",
    CellStatus.failed_interrupted: "error",
    CellStatus.awaiting_auth: "warning",
    CellStatus.not_configured: "warning",
}


def append_cell_status_event(
    session: Session,
    *,
    job_id: str,
    provider: Provider,
    status: CellStatus,
    error_code: str | None = None,
    message: str | None = None,
) -> None:
    """Record a `job_events` row for a cell **status** transition.

    Why this exists (the "session check hangs" defect): the SSE stream
    `GET /sync-jobs/{id}/events` ships nothing but `job_events` rows, and the
    frontend re-reads the job snapshot — the only place cell *statuses* live —
    exclusively when such a row arrives (`frontend/js/sse.js`). Until now the
    only writer was `make_emitter`, which runs *inside* `adapter.collect()`.
    So a cell resolved during preflight (`not_configured`, `awaiting_auth`,
    freshness `skipped`) produced zero events, the stream stayed silent, and
    the sync modal was frozen on its initial all-`queued` snapshot forever —
    every provider stuck on «Checking…» even though the worker had already
    parked the lane awaiting login.

    The payload is deliberately thin: the frontend re-fetches the full job DTO
    on *any* event, so this only has to be a truthful wake-up, not a second
    source of truth for status. Runs inside the caller's session/transaction so
    the row and the status change commit together.
    """
    EventRepository(session).append(
        job_id,
        _CELL_STATUS_LEVEL.get(status, "info"),
        provider=provider,
        stage=_CELL_STATUS_STAGE.get(status, "complete"),
        message=_redact_text(message or f"cell {status.value}"),
        details=_redact_value({"cell_status": status.value, "error_code": error_code}),
    )


@contextmanager
def event_scope(session_scope: Any, job_id: str) -> Generator[EventRepository, None, None]:
    """Convenience wrapper for one-off, non-progress-event writes (e.g. the
    runner logging a stage transition) that still need redaction + `seq`.
    """
    with session_scope() as session:
        yield EventRepository(session)


def _format_sse(event: Any) -> bytes:
    provider = getattr(event, "provider", None)
    payload = {
        "level": event.level,
        "provider": provider.value if provider is not None else None,
        "stage": event.stage,
        "percent": event.percent,
        "message": event.message,
        "details": event.details_json,
        "timestamp": event.timestamp.isoformat(),
    }
    body = json.dumps(payload, ensure_ascii=False)
    return f"id: {event.seq}\ndata: {body}\n\n".encode()


async def event_stream(
    fetch_since: Callable[[int], Awaitable[list[Any]]],
    *,
    last_event_id: int = 0,
    poll_interval_seconds: float = 1.0,
    keepalive_seconds: float = 15.0,
    is_done: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[bytes]:
    """SSE body generator for `GET /sync-jobs/{id}/events` (task-06 mounts it).

    Deliberately decoupled from any concrete session/engine: the caller
    supplies `fetch_since(last_seq)` — typically `EventRepository.since`
    bridged through `AsyncSession.run_sync` — so this stays a pure
    polling/formatting/keepalive loop. Replay after reconnect is just calling
    it again with `last_event_id=Last-Event-ID` (PLAN §9.3).
    """
    last_seq = last_event_id
    idle_seconds = 0.0
    while True:
        events = await fetch_since(last_seq)
        if events:
            idle_seconds = 0.0
            for event in events:
                last_seq = event.seq
                yield _format_sse(event)
            continue
        if is_done is not None and await is_done():
            return
        idle_seconds += poll_interval_seconds
        if idle_seconds >= keepalive_seconds:
            yield b": keepalive\n\n"
            idle_seconds = 0.0
        await asyncio.sleep(poll_interval_seconds)
