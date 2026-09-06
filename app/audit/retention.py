"""Ретенція журналу і протухлих сесій (task-72 §6, task-71 follow-up).

Журнал у БД тримаємо `audit_retention_days` (за замовчуванням рік), але
історію не втрачаємо: перед видаленням рядки вивантажуються у JSONL поруч із
базою (і, якщо ввімкнено cloud, звідти їх забирає звичайний B2-синк task-52).
Тобто база лишається швидкою, а «що робили два роки тому» відповідь має.

Сам факт очищення теж потрапляє в журнал — інакше в ньому була б дірка,
пояснити яку нічим.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.repositories.audit import AuditRepository
from app.db.repositories.users import UserRepository

logger = get_logger(__name__)


def archive_path(settings: Settings, now: datetime) -> Path:
    base = Path(settings.audit_fallback_path).parent / "audit_archive"
    return base / f"audit-{now:%Y%m}.jsonl"


def run_retention(
    session: Session, *, settings: Settings | None = None, now: datetime | None = None
) -> dict[str, int]:
    """Вивантажити → видалити → прибрати мертві сесії. Повертає лічильники."""
    settings = settings or get_settings()
    moment = now or datetime.now(UTC)
    audit_repo = AuditRepository(session)
    days = settings.audit_retention_days

    doomed = audit_repo.iter_older_than(days, now=moment)
    exported = 0
    if doomed:
        target = archive_path(settings, moment)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            for event in doomed:
                handle.write(
                    json.dumps(
                        {
                            "id": event.id,
                            "ts": event.ts.isoformat(),
                            "actor_kind": event.actor_kind,
                            "actor_email": event.actor_email,
                            "actor_role": event.actor_role,
                            "action": event.action,
                            "outcome": event.outcome,
                            "resource_type": event.resource_type,
                            "resource_id": event.resource_id,
                            "method": event.method,
                            "path": event.path,
                            "status_code": event.status_code,
                            "ip": event.ip,
                            "details": event.details_json,
                        },
                        default=str,
                    )
                    + "\n"
                )
                exported += 1

    purged = audit_repo.purge_older_than(days, now=moment)
    # Протухлі сесії живуть у тій самій «щоденній прибиральні»: обидві таблиці
    # ростуть від самого користування системою й нікому не потрібні старими.
    sessions = UserRepository(session).purge_expired_sessions(now=moment)
    session.commit()

    logger.info(
        "audit_retention exported=%d purged=%d expired_sessions=%d retention_days=%d",
        exported,
        purged,
        sessions,
        days,
    )
    return {"exported": exported, "purged": purged, "expired_sessions": sessions}
