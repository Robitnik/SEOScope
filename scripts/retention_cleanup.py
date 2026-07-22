#!/usr/bin/env python
"""Прибирання старих `job_events` за політикою retention (PLAN §18, Етап 8).

`job_events` росте на кожну стадію кожної клітинки кожного запуску — це
найшвидше зростаюча таблиця в системі, і єдина, дані якої мають строк
придатності: після завершення job подія потрібна лише для аудиту.
Snapshot-и й метрики **ніколи** не чистяться — це історія, на якій
будуються порівняння періодів.

    docker compose exec api poetry run python scripts/retention_cleanup.py --dry-run
    docker compose exec api poetry run python scripts/retention_cleanup.py
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

MIN_RETENTION_DAYS = 7


def main(argv: list[str] | None = None) -> int:
    from sqlalchemy import func, select

    from app.core.config import get_settings
    from app.db.models import JobEvent
    from app.db.repositories.events import EventRepository
    from app.db.session import worker_session_scope

    settings = get_settings()
    parser = argparse.ArgumentParser(prog="retention_cleanup")
    parser.add_argument(
        "--days",
        type=int,
        default=settings.event_retention_days,
        help=(
            "скільки днів подій лишати (за замовчуванням "
            f"EVENT_RETENTION_DAYS={settings.event_retention_days})"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="лише порахувати, нічого не видаляти"
    )
    args = parser.parse_args(argv)

    if args.days < MIN_RETENTION_DAYS:
        print(
            f"відмова: --days={args.days} менше за безпечний мінімум {MIN_RETENTION_DAYS}; "
            "свіжі події ще потрібні для SSE-replay і розбору інцидентів",
            file=sys.stderr,
        )
        return 2

    cutoff = datetime.now(UTC) - timedelta(days=args.days)

    with worker_session_scope() as session:
        total = session.scalar(select(func.count()).select_from(JobEvent)) or 0
        stale = (
            session.scalar(
                select(func.count()).select_from(JobEvent).where(JobEvent.timestamp < cutoff)
            )
            or 0
        )
        if args.dry_run:
            print(f"dry-run: подій {total}, старших за {cutoff.isoformat()} — {stale}")
            return 0
        deleted = EventRepository(session).purge_older_than(args.days)

    print(f"видалено {deleted} подій, старших за {cutoff.isoformat()} (лишилось {total - deleted})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
