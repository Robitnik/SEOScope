"""Daily composite-health history (`client_health_daily`, PLAN §21.2).

The composite score is derived on read by `app.metrics.health.compute_health`
and was never stored, so a card could show today's number but no trend. This
repository is the store behind the card's week-over-week arrow and its daily
sparkline.

Upsert-per-day rather than append-only: a day's score summarises that day's
best-known data, so a second collection the same day supersedes the first.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import ClientHealthDaily


class HealthHistoryRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        client_id: str,
        day: date,
        value: Decimal,
        *,
        coverage_contributing: int = 0,
        coverage_configured: int = 0,
    ) -> ClientHealthDaily:
        """Insert or update this client's score for `day`.

        Callers must not pass a `None` score: a day with no contributing
        service has no health, and storing it as `0` would invent a value the
        UI would then chart as a crash (PLAN §12, None != 0).
        """
        row = self._session.scalars(
            select(ClientHealthDaily).where(
                ClientHealthDaily.client_id == client_id, ClientHealthDaily.day == day
            )
        ).one_or_none()

        if row is None:
            row = ClientHealthDaily(client_id=client_id, day=day)
            self._session.add(row)

        row.value = value
        row.coverage_contributing = coverage_contributing
        row.coverage_configured = coverage_configured
        row.computed_at = utcnow()
        self._session.flush()
        return row

    def history(self, client_id: str, *, since: date, until: date) -> list[ClientHealthDaily]:
        """Points within `[since, until]`, oldest first. Days without a real
        score are simply absent — the caller must not read a gap as a zero.
        """
        stmt = (
            select(ClientHealthDaily)
            .where(
                ClientHealthDaily.client_id == client_id,
                ClientHealthDaily.day >= since,
                ClientHealthDaily.day <= until,
            )
            .order_by(ClientHealthDaily.day)
        )
        return list(self._session.scalars(stmt))

    def value_on_or_before(self, client_id: str, day: date) -> ClientHealthDaily | None:
        """Most recent point at or before `day`.

        "Or before" matters: collection is not guaranteed daily, so asking for
        exactly D-7 would usually find nothing and silently suppress the trend.
        The nearest earlier point is the honest comparison basis.
        """
        stmt = (
            select(ClientHealthDaily)
            .where(ClientHealthDaily.client_id == client_id, ClientHealthDaily.day <= day)
            .order_by(ClientHealthDaily.day.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def purge_older_than(self, days: int, *, now: date | None = None) -> int:
        """Retention hook, mirroring `EventRepository.purge_older_than`."""
        cutoff = (now or utcnow().date()) - timedelta(days=days)
        rows = list(
            self._session.scalars(select(ClientHealthDaily).where(ClientHealthDaily.day < cutoff))
        )
        for row in rows:
            self._session.delete(row)
        self._session.flush()
        return len(rows)
