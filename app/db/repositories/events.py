"""Event repository (`job_events`, PLAN §6): monotonic `seq` doubles as the SSE event id."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, exists, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import JobEvent
from app.schemas.enums import Provider


class EventRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append(
        self,
        job_id: str,
        level: str,
        *,
        provider: Provider | None = None,
        stage: str | None = None,
        percent: float | None = None,
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> int:
        event = JobEvent(
            job_id=job_id,
            level=level,
            provider=provider,
            stage=stage,
            percent=percent,
            message=message,
            details_json=details or {},
        )
        self._session.add(event)
        self._session.flush()
        assert event.seq is not None
        return event.seq

    def since(
        self, job_id: str, last_seq: int, *, safe_before: datetime | None = None
    ) -> list[JobEvent]:
        """Events after `last_seq` (task-75 §3.3).

        `safe_before` is the SSE cursor's safety window (task-75 §3.3 /
        `Settings.job_event_cursor_safety_window_ms`): when given, only
        rows older than it are returned, so a caller advancing its cursor
        to the highest returned `seq` can never skip past a row from a
        concurrent, still-uncommitted transaction that took a lower
        `seq` earlier. `None` (the default) is the old, unfiltered
        behaviour — still correct under exactly one writer, and used by
        `has_pending` below, which needs "does anything exist at all
        past `last_seq`", not "what is safe to show right now".
        """
        conditions = [JobEvent.job_id == job_id, JobEvent.seq > last_seq]
        if safe_before is not None:
            conditions.append(JobEvent.timestamp <= safe_before)
        stmt = select(JobEvent).where(*conditions).order_by(JobEvent.seq)
        return list(self._session.scalars(stmt))

    def has_pending(self, job_id: str, last_seq: int) -> bool:
        """Does ANY row exist past `last_seq`, ignoring the safety window?

        Companion to `since(..., safe_before=...)` (task-75 §3.3): a
        caller must not treat the stream as finished just because
        `since()` returned nothing this poll — that can simply mean the
        newest events have not aged past the safety window yet. This
        answers "is there something still coming", independent of
        whether it is safe to serve this instant.
        """
        stmt = select(
            exists().where(JobEvent.job_id == job_id, JobEvent.seq > last_seq)
        )
        return bool(self._session.scalar(stmt))

    def purge_older_than(self, days: int) -> int:
        cutoff = utcnow() - timedelta(days=days)
        result = cast(
            "CursorResult[Any]",
            self._session.execute(delete(JobEvent).where(JobEvent.timestamp < cutoff)),
        )
        self._session.flush()
        return result.rowcount or 0
