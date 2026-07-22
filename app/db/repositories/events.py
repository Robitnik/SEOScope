"""Event repository (`job_events`, PLAN §6): monotonic `seq` doubles as the SSE event id."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
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

    def since(self, job_id: str, last_seq: int) -> list[JobEvent]:
        stmt = (
            select(JobEvent)
            .where(JobEvent.job_id == job_id, JobEvent.seq > last_seq)
            .order_by(JobEvent.seq)
        )
        return list(self._session.scalars(stmt))

    def purge_older_than(self, days: int) -> int:
        cutoff = utcnow() - timedelta(days=days)
        result = cast(
            "CursorResult[Any]",
            self._session.execute(delete(JobEvent).where(JobEvent.timestamp < cutoff)),
        )
        self._session.flush()
        return result.rowcount or 0
