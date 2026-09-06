"""Ретенція журналу + протухлих сесій (task-72 §6)."""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy.orm import Session

from app.audit.retention import archive_path, run_retention
from app.audit.sink import AuditRecord
from app.core.config import get_settings
from app.db.base import utcnow
from app.db.repositories.audit import AuditRepository
from app.db.repositories.users import UserRepository


def test_old_rows_are_exported_before_being_purged(db: Session, tmp_path) -> None:
    settings = get_settings()
    audit_repo = AuditRepository(db)
    old = AuditRecord(action="old.one", ts=utcnow() - timedelta(days=400))
    fresh = AuditRecord(action="fresh.one", ts=utcnow())
    audit_repo.bulk_append([old, fresh])
    db.commit()

    now = utcnow()
    result = run_retention(db, settings=settings, now=now)
    assert result["purged"] == 1
    assert result["exported"] == 1
    assert audit_repo.count() == 1

    archive = archive_path(settings, now)
    assert archive.exists()
    lines = archive.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["action"] == "old.one"


def test_expired_sessions_are_purged_alongside_audit(db: Session) -> None:
    user_repo = UserRepository(db)
    user = user_repo.create(email="ivan@yovth.com", name="Ivan", role=__import__(
        "app.schemas.enums", fromlist=["UserRole"]
    ).UserRole.member, password="pw")
    row, _ = user_repo.create_session(user, absolute_ttl_days=14)
    db.commit()

    future = row.expires_at + timedelta(days=1)
    result = run_retention(db, now=future)
    assert result["expired_sessions"] == 1


def test_retention_with_nothing_to_do_is_a_no_op(db: Session) -> None:
    result = run_retention(db)
    assert result == {"exported": 0, "purged": 0, "expired_sessions": 0}


def test_sink_runs_retention_at_most_once_per_day(db: Session, monkeypatch) -> None:
    """Ретенція живе в потоці sink'а — цей тест лише підтверджує, що вона
    не намагається бігти на кожен idle-тик."""
    from app.audit.sink import AuditSink

    calls: list[int] = []
    monkeypatch.setattr(
        "app.audit.retention.run_retention",
        lambda *a, **k: (calls.append(1), {"exported": 0, "purged": 0, "expired_sessions": 0})[1],
    )
    sink = AuditSink(batch_size=1000, flush_interval_s=0.02)
    try:
        sink.submit(AuditRecord(action="client.create"))
        sink.flush()
        sink.flush()
        sink.flush()
    finally:
        sink.stop()
    assert len(calls) <= 1
