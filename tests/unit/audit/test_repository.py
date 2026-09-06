"""Репозиторій журналу (task-72): курсорна пагінація, фільтри, append-only."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.audit.sink import AuditRecord
from app.db.base import utcnow
from app.db.models import AuditEvent
from app.db.repositories.audit import AuditRepository


def _seed(db: Session, count: int, *, action: str = "client.create") -> None:
    repo = AuditRepository(db)
    base = utcnow()
    records = [
        AuditRecord(action=action, ts=base + timedelta(seconds=i), actor_email=f"u{i}@yovth.com")
        for i in range(count)
    ]
    repo.bulk_append(records)
    db.commit()


def test_pagination_covers_every_row_without_duplicates(db: Session) -> None:
    _seed(db, 25)
    repo = AuditRepository(db)
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        page = repo.query(limit=7, cursor=cursor)
        if not page:
            break
        seen.extend(event.id for event in page)
        last = page[-1]
        cursor = (last.ts, last.id)
    assert len(seen) == 25
    assert len(set(seen)) == 25, "курсор не має ні пропускати, ні дублювати рядки"


def test_pagination_handles_identical_timestamps(db: Session) -> None:
    """Рядки одного батчу мають однаковий `ts` з точністю Python — курсор
    мусить розрізняти їх за `id`, інакше межа сторінки губить або дублює рядки."""
    repo = AuditRepository(db)
    moment = utcnow()
    records = [AuditRecord(action="client.create", ts=moment) for _ in range(6)]
    repo.bulk_append(records)
    db.commit()

    first_page = repo.query(limit=3)
    assert len(first_page) == 3
    last = first_page[-1]
    second_page = repo.query(limit=3, cursor=(last.ts, last.id))
    assert len(second_page) == 3
    assert {e.id for e in first_page}.isdisjoint({e.id for e in second_page})


def test_filters_narrow_the_result(db: Session) -> None:
    _seed(db, 3, action="client.create")
    _seed(db, 2, action="job.start")
    repo = AuditRepository(db)
    assert len(repo.query(action="job.start", limit=10)) == 2
    assert len(repo.query(action="client.create", limit=10)) == 3


def test_search_matches_actor_email(db: Session) -> None:
    _seed(db, 5)
    repo = AuditRepository(db)
    results = repo.query(search="u2@yovth.com", limit=10)
    assert len(results) == 1
    assert results[0].actor_email == "u2@yovth.com"


def test_purge_only_removes_rows_older_than_the_cutoff(db: Session) -> None:
    repo = AuditRepository(db)
    old = AuditRecord(action="old.one", ts=utcnow() - timedelta(days=400))
    fresh = AuditRecord(action="fresh.one", ts=utcnow())
    repo.bulk_append([old, fresh])
    db.commit()

    purged = repo.purge_older_than(365)
    db.commit()
    assert purged == 1
    remaining = repo.query(limit=10)
    assert len(remaining) == 1
    assert remaining[0].action == "fresh.one"


def test_the_model_forbids_updating_a_row() -> None:
    """Append-only на рівні ORM: `before_update` кидає виняток на КОЖНЕ поле,
    не лише на «важливі» — журнал не має напівредагованих рядків."""
    from app.db import session as db_session

    maker_cls = db_session.get_sync_sessionmaker()
    with maker_cls() as session:
        repo = AuditRepository(session)
        repo.bulk_append([AuditRecord(action="client.create")])
        session.commit()
        event = session.query(AuditEvent).first()
        assert event is not None
        event.action = "tampered"
        try:
            session.commit()
            raised = False
        except ValueError:
            raised = True
            session.rollback()
        assert raised, "AuditEvent має бути незмінним після коміту"
