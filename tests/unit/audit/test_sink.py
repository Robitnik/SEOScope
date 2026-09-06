"""Черга/батч журналу (task-72) — головний ризик цієї таски.

Найважливіше тут одне: `submit()` НІКОЛИ не блокує й не кидає виняток,
і запис у БД відбувається БАТЧАМИ через `DbWriteGate`, а не по одному рядку
на дію. Порушення цього — прямий шлях до лок-шторму task-66/67.
"""

from __future__ import annotations

import json
import queue
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.audit.sink import AuditRecord, AuditSink
from app.db.repositories.audit import AuditRepository


def _record(action: str = "client.create", **kwargs: object) -> AuditRecord:
    return AuditRecord(action=action, **kwargs)  # type: ignore[arg-type]


def test_submit_never_raises_and_writes_eventually(db: Session) -> None:
    sink = AuditSink(batch_size=10, flush_interval_s=0.05)
    try:
        assert sink.submit(_record()) is True
        sink.flush()
        assert AuditRepository(db).count() == 1
    finally:
        sink.stop()


def test_batches_are_written_as_one_transaction(db: Session, monkeypatch) -> None:
    sink = AuditSink(batch_size=5, flush_interval_s=5.0)
    calls: list[int] = []
    original_bulk_append = AuditRepository.bulk_append

    def counting_bulk_append(self: AuditRepository, records: list) -> int:  # type: ignore[type-arg]
        calls.append(len(records))
        return original_bulk_append(self, records)

    monkeypatch.setattr(AuditRepository, "bulk_append", counting_bulk_append)
    try:
        for i in range(5):
            sink.submit(_record(action=f"a.{i}"))
        sink.flush()
        assert calls == [5], "п'ять подій мають лягти ОДНИМ викликом bulk_append, не п'ятьма"
        assert AuditRepository(db).count() == 5
    finally:
        sink.stop()


def test_full_queue_drops_instead_of_blocking(db: Session) -> None:
    sink = AuditSink(max_queue=2, batch_size=1000, flush_interval_s=5.0)
    try:
        # Черга заповнюється навмисно ДО того, як флашер її розбере: спираємось
        # на великий batch_size/інтервал, щоб перегін не встиг спорожнити чергу.
        with patch.object(sink._queue, "put_nowait", side_effect=queue.Full):
            accepted = sink.submit(_record())
        assert accepted is False
        assert sink.dropped == 1
    finally:
        sink.stop()


def test_flush_is_synchronous_and_deterministic(db: Session) -> None:
    """Тести не мають спати в очікуванні потоку — `flush()` гарантує, що
    все прийняте до виклику вже лежить у БД."""
    sink = AuditSink(batch_size=1000, flush_interval_s=5.0)
    try:
        for i in range(3):
            sink.submit(_record(action=f"a.{i}"))
        sink.flush()
        assert AuditRepository(db).count() == 3
    finally:
        sink.stop()


def test_db_failure_falls_back_to_a_local_file(db: Session, tmp_path: Path) -> None:
    fallback = tmp_path / "fallback.jsonl"
    sink = AuditSink(batch_size=10, flush_interval_s=0.05, fallback_path=fallback)
    try:
        with patch(
            "app.db.repositories.audit.AuditRepository.bulk_append",
            side_effect=RuntimeError("db is down"),
        ):
            sink.submit(_record(action="client.create"))
            sink.flush()
        assert fallback.exists()
        lines = fallback.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["action"] == "client.create"
        assert sink.failed == 1
    finally:
        sink.stop()


def test_health_reports_counters(db: Session) -> None:
    sink = AuditSink(batch_size=10, flush_interval_s=0.05)
    try:
        sink.submit(_record())
        sink.flush()
        health = sink.health()
        assert health["written"] == 1
        assert health["dropped"] == 0
    finally:
        sink.stop()
