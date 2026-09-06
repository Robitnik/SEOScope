"""Репозиторій журналу (task-72): запис батчами, читання курсором.

Свідомо БЕЗ методів `update`/`delete` по одному рядку — журнал append-only,
і відсутність такого методу є частиною гарантії, а не забудькуватістю.
Єдине видалення — `purge_older_than` (ретенція), і воно саме журналюється.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, and_, delete, func, or_, select
from sqlalchemy.orm import Session

from app.audit.redaction import prepare
from app.db.base import utcnow
from app.db.models import AuditEvent

if TYPE_CHECKING:
    from app.audit.sink import AuditRecord


class AuditRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def bulk_append(self, records: list[AuditRecord]) -> int:
        for record in records:
            self._session.add(
                AuditEvent(
                    ts=record.ts,
                    actor_kind=record.actor_kind,
                    actor_id=record.actor_id,
                    actor_email=record.actor_email,
                    actor_name=record.actor_name,
                    actor_role=record.actor_role,
                    session_id=record.session_id,
                    ip=record.ip,
                    user_agent=(record.user_agent or "")[:256] or None,
                    action=record.action,
                    resource_type=record.resource_type,
                    resource_id=record.resource_id,
                    resource_label=record.resource_label,
                    method=record.method,
                    path=(record.path or "")[:500] or None,
                    status_code=record.status_code,
                    outcome=record.outcome,
                    duration_ms=record.duration_ms,
                    details_json=prepare(record.details),
                )
            )
        self._session.flush()
        return len(records)

    def query(
        self,
        *,
        actor_id: str | None = None,
        actor_kind: str | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        outcome: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        search: str | None = None,
        limit: int = 100,
        cursor: tuple[datetime, str] | None = None,
    ) -> list[AuditEvent]:
        """Сторінка журналу, найновіше першим.

        Курсор `(ts, id)`, а не `OFFSET`: ця таблиця росте найшвидше в
        системі, і `OFFSET` на сотнях тисяч рядків деградує лінійно —
        сторінка 50 коштувала б скану всього, що перед нею. `id` у ключі —
        щоб події з однаковою міток часу не губились і не дублювались на межі
        сторінки (у батчі їх буває багато).
        """
        stmt = select(AuditEvent)
        if actor_id:
            stmt = stmt.where(AuditEvent.actor_id == actor_id)
        if actor_kind:
            stmt = stmt.where(AuditEvent.actor_kind == actor_kind)
        if action:
            stmt = stmt.where(AuditEvent.action == action)
        if resource_type:
            stmt = stmt.where(AuditEvent.resource_type == resource_type)
        if resource_id:
            stmt = stmt.where(AuditEvent.resource_id == resource_id)
        if outcome:
            stmt = stmt.where(AuditEvent.outcome == outcome)
        if since:
            stmt = stmt.where(AuditEvent.ts >= since)
        if until:
            stmt = stmt.where(AuditEvent.ts <= until)
        if search:
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(
                or_(
                    AuditEvent.actor_email.like(pattern),
                    AuditEvent.actor_name.like(pattern),
                    AuditEvent.resource_label.like(pattern),
                    AuditEvent.path.like(pattern),
                    AuditEvent.action.like(pattern),
                )
            )
        if cursor is not None:
            cursor_ts, cursor_id = cursor
            stmt = stmt.where(
                or_(
                    AuditEvent.ts < cursor_ts,
                    and_(AuditEvent.ts == cursor_ts, AuditEvent.id < cursor_id),
                )
            )
        stmt = stmt.order_by(AuditEvent.ts.desc(), AuditEvent.id.desc()).limit(limit)
        return list(self._session.scalars(stmt))

    def get(self, event_id: str) -> AuditEvent | None:
        return self._session.get(AuditEvent, event_id)

    def facets(self, *, days: int = 90) -> dict[str, list[Any]]:
        """Значення для селектів фільтра — тільки ті, що реально трапляються.

        Вікно в днях, бо список дій має відповідати тому, що система робить
        зараз, а не тому, що вона робила в перший тиждень після запуску.
        """
        since = utcnow() - timedelta(days=days)
        actions = self._session.execute(
            select(AuditEvent.action, func.count())
            .where(AuditEvent.ts >= since)
            .group_by(AuditEvent.action)
            .order_by(func.count().desc())
        ).all()
        actors = self._session.execute(
            select(AuditEvent.actor_id, AuditEvent.actor_email, func.count())
            .where(AuditEvent.ts >= since, AuditEvent.actor_id.is_not(None))
            .group_by(AuditEvent.actor_id, AuditEvent.actor_email)
            .order_by(func.count().desc())
        ).all()
        resource_types = self._session.scalars(
            select(AuditEvent.resource_type)
            .where(AuditEvent.ts >= since, AuditEvent.resource_type.is_not(None))
            .group_by(AuditEvent.resource_type)
        ).all()
        return {
            "actions": [{"action": row[0], "count": int(row[1])} for row in actions],
            "actors": [
                {"actor_id": row[0], "email": row[1], "count": int(row[2])} for row in actors
            ],
            "resource_types": sorted(resource_types),
        }

    def count(self) -> int:
        return int(self._session.scalar(select(func.count()).select_from(AuditEvent)) or 0)

    def purge_older_than(self, days: int, *, now: datetime | None = None) -> int:
        cutoff = (now or utcnow()) - timedelta(days=days)
        result = cast(
            "CursorResult[Any]",
            self._session.execute(delete(AuditEvent).where(AuditEvent.ts < cutoff)),
        )
        self._session.flush()
        return int(result.rowcount or 0)

    def iter_older_than(self, days: int, *, now: datetime | None = None) -> list[AuditEvent]:
        """Те, що зараз буде видалено — для вивантаження в архів перед purge."""
        cutoff = (now or utcnow()) - timedelta(days=days)
        return list(
            self._session.scalars(
                select(AuditEvent).where(AuditEvent.ts < cutoff).order_by(AuditEvent.ts)
            )
        )
