"""Точка запису журналу для доменного шару (task-72).

HTTP-дії знімає `AuditMiddleware` сам. Ця функція — для всього, що НЕ є
запитом: тіки воркера, планувальники, CLI, нічний backup. Без неї журнал
відповідав би лише «що робили люди», і будь-яка нічна дія системи виглядала
б як така, що не сталася.
"""

from __future__ import annotations

from typing import Any

from app.audit.sink import AuditRecord, get_audit_sink
from app.core.config import get_settings


def record_audit(
    action: str,
    *,
    actor: Any | None = None,
    actor_kind: str = "system",
    outcome: str = "success",
    resource_type: str | None = None,
    resource_id: str | None = None,
    resource_label: str | None = None,
    details: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> None:
    """Кладе подію в чергу. Ніколи не блокує й ніколи не кидає винятку —
    журнал не має права зламати дію, яку журналює.

    Перевіряє `settings.audit_enabled` тут, а не лише в `AuditMiddleware`:
    інакше фонові виклики (`app/cloud/scheduler.py` тощо) стартували б
    глобальний `AuditSink` навіть коли журнал вимкнено — саме так тестовий
    прогін `tests/unit/cloud/test_scheduler.py`, що вимикає лише HTTP-гейт,
    міг би все одно розбудити фоновий потік і дописати в БОЙОВУ БД цього
    контейнера вже після відкату тестового `DATABASE_URL` (LIVE 2026-09-06).
    """
    if not get_settings().audit_enabled:
        return
    record = AuditRecord(
        action=action,
        actor_kind=getattr(actor, "kind", actor_kind) if actor is not None else actor_kind,
        actor_id=getattr(actor, "id", None) if actor is not None else None,
        actor_email=getattr(actor, "email", None) if actor is not None else None,
        actor_name=getattr(actor, "name", None) if actor is not None else None,
        actor_role=(
            getattr(getattr(actor, "role", None), "value", None) if actor is not None else None
        ),
        session_id=getattr(actor, "session_id", None) if actor is not None else None,
        ip=getattr(actor, "ip", None) if actor is not None else None,
        outcome=outcome,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_label=resource_label,
        details=details or {},
        duration_ms=duration_ms,
    )
    get_audit_sink().submit(record)
