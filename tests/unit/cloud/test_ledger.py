"""`CloudLedgerRepository` (task-52 §3.3): idempotent upserts, candidate
selection (never re-picks an `uploaded` row, always re-picks a `failed` one),
the watermark, and GFS tier bookkeeping.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.db.models import MonitorBlob, ReportFile
from app.db.repositories.clients import ClientRepository
from app.db.repositories.cloud import CloudLedgerRepository

T0 = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def _client(session: Session, *, slug: str = "acme") -> str:
    client = ClientRepository(session).create(
        name="Acme", slug=slug, domain=f"{slug}.com", timezone="America/New_York"
    )
    session.commit()
    return client.id


def _blob(session: Session, sha256: str, *, first_seen_at: datetime = T0) -> MonitorBlob:
    blob = MonitorBlob(
        sha256=sha256,
        size_bytes=100,
        gzip_size_bytes=40,
        mime="text/html",
        path=f"monitor/{sha256[:2]}/{sha256}.gz",
        first_seen_at=first_seen_at,
        last_seen_at=first_seen_at,
    )
    session.add(blob)
    session.commit()
    return blob


def _report_file(session: Session, client_id: str, *, created_at: datetime = T0) -> ReportFile:
    rf = ReportFile(
        client_id=client_id,
        kind="pdf",
        path="reports/run-1/acme.pdf",
        sha256="a" * 64,
        size_bytes=1234,
        mime="application/pdf",
        created_at=created_at,
    )
    session.add(rf)
    session.commit()
    return rf


# --- record_uploaded / record_failed idempotency --------------------------


def test_record_uploaded_then_record_uploaded_again_updates_the_same_row(session: Session):
    ledger = CloudLedgerRepository(session)
    ledger.record_uploaded(
        kind="monitor_blob", source_ref="sha-1", key="archive/sh/sha-1.gz",
        sha256="hash-a", size_bytes=10,
    )
    session.commit()
    ledger.record_uploaded(
        kind="monitor_blob", source_ref="sha-1", key="archive/sh/sha-1.gz",
        sha256="hash-b", size_bytes=20,
    )
    session.commit()

    obj = ledger.get("monitor_blob", "sha-1")
    assert obj is not None
    assert obj.sha256 == "hash-b"
    assert obj.attempts == 2


def test_record_failed_then_record_uploaded_flips_status(session: Session):
    ledger = CloudLedgerRepository(session)
    ledger.record_failed(
        kind="monitor_blob", source_ref="sha-1", key="archive/sh/sha-1.gz",
        error_message="boom",
    )
    session.commit()
    assert ledger.get("monitor_blob", "sha-1").status == "failed"

    ledger.record_uploaded(
        kind="monitor_blob", source_ref="sha-1", key="archive/sh/sha-1.gz",
        sha256="hash-a", size_bytes=10,
    )
    session.commit()
    obj = ledger.get("monitor_blob", "sha-1")
    assert obj.status == "uploaded"
    assert obj.error_message is None


# --- monitor_blob candidates ------------------------------------------------


def test_monitor_blob_candidates_excludes_uploaded_includes_failed_and_never_tried(
    session: Session,
):
    _blob(session, "a" * 64)
    _blob(session, "b" * 64)
    _blob(session, "c" * 64)
    ledger = CloudLedgerRepository(session)
    ledger.record_uploaded(
        kind="monitor_blob", source_ref="a" * 64, key="k1", sha256="x", size_bytes=1
    )
    ledger.record_failed(kind="monitor_blob", source_ref="b" * 64, key="k2", error_message="boom")
    session.commit()

    candidates = {b.sha256 for b in ledger.monitor_blob_candidates(since=T0, limit=100)}
    assert candidates == {"b" * 64, "c" * 64}


def test_monitor_blob_candidates_respects_since_watermark(session: Session):
    _blob(session, "a" * 64, first_seen_at=T0 - timedelta(days=2))
    _blob(session, "b" * 64, first_seen_at=T0 + timedelta(hours=1))
    ledger = CloudLedgerRepository(session)

    candidates = {b.sha256 for b in ledger.monitor_blob_candidates(since=T0, limit=100)}
    assert candidates == {"b" * 64}


def test_monitor_blob_candidates_respects_limit(session: Session):
    for i in range(5):
        _blob(session, f"{i}" * 64, first_seen_at=T0 + timedelta(minutes=i))
    ledger = CloudLedgerRepository(session)

    candidates = ledger.monitor_blob_candidates(since=T0, limit=2)
    assert len(candidates) == 2


# --- report_file candidates -------------------------------------------------


def test_report_file_candidates_excludes_uploaded(session: Session):
    client_id = _client(session)
    rf1 = _report_file(session, client_id)
    rf2 = ReportFile(
        client_id=client_id, kind="xlsx", path="reports/run-1/acme.xlsx",
        sha256="b" * 64, size_bytes=99, mime=None, created_at=T0,
    )
    session.add(rf2)
    session.commit()

    ledger = CloudLedgerRepository(session)
    ledger.record_uploaded(
        kind="report_file", source_ref=rf1.id, key="reports/acme/2026/acme.pdf",
        sha256=rf1.sha256, size_bytes=rf1.size_bytes,
    )
    session.commit()

    candidates = {rf.id for rf in ledger.report_file_candidates(limit=100)}
    assert candidates == {rf2.id}


def test_report_file_key_taken_by_other(session: Session):
    client_id = _client(session)
    rf1 = _report_file(session, client_id)
    ledger = CloudLedgerRepository(session)
    ledger.record_uploaded(
        kind="report_file", source_ref=rf1.id, key="reports/acme/2026/acme.pdf",
        sha256=rf1.sha256, size_bytes=rf1.size_bytes,
    )
    session.commit()

    assert ledger.report_file_key_taken_by_other(
        "reports/acme/2026/acme.pdf", exclude_source_ref="some-other-id"
    )
    assert not ledger.report_file_key_taken_by_other(
        "reports/acme/2026/acme.pdf", exclude_source_ref=rf1.id
    )
    assert not ledger.report_file_key_taken_by_other(
        "reports/acme/2026/different.pdf", exclude_source_ref="some-other-id"
    )


# --- watermark ---------------------------------------------------------------


def test_get_or_init_watermark_fixes_the_value_permanently(session: Session):
    ledger = CloudLedgerRepository(session)
    first = ledger.get_or_init_watermark("archive_since", default=T0)
    session.commit()
    assert first == T0

    second = ledger.get_or_init_watermark("archive_since", default=T0 + timedelta(days=10))
    assert second == T0  # unchanged — the first activation already fixed it


def test_set_watermark_is_read_back_exactly(session: Session):
    ledger = CloudLedgerRepository(session)
    ledger.set_watermark("archive_since", T0)
    session.commit()
    assert ledger.get_watermark("archive_since") == T0


def test_get_watermark_returns_none_before_first_use(session: Session):
    ledger = CloudLedgerRepository(session)
    assert ledger.get_watermark("archive_since") is None


# --- GFS backup tiers --------------------------------------------------------


def test_backups_by_tier_newest_first_and_scoped_to_tier(session: Session):
    ledger = CloudLedgerRepository(session)
    for date_key in ("2026-08-17", "2026-08-18", "2026-08-19"):
        ledger.record_backup(
            tier="daily", date_key=date_key, key=f"db/daily/app-{date_key}.db.gz",
            sha256="x", size_bytes=1,
        )
    ledger.record_backup(
        tier="weekly", date_key="2026-08-17", key="db/weekly/app-2026-08-17.db.gz",
        sha256="x", size_bytes=1,
    )
    session.commit()

    daily = ledger.backups_by_tier("daily")
    assert [obj.source_ref for obj in daily] == [
        "daily/2026-08-19", "daily/2026-08-18", "daily/2026-08-17",
    ]
    weekly = ledger.backups_by_tier("weekly")
    assert len(weekly) == 1


def test_has_backup_and_latest_backup(session: Session):
    ledger = CloudLedgerRepository(session)
    assert ledger.latest_backup() is None
    assert not ledger.has_backup(tier="daily", date_key="2026-08-19")

    ledger.record_backup(
        tier="daily", date_key="2026-08-19", key="db/daily/app-1.db.gz", sha256="x", size_bytes=1
    )
    session.commit()

    assert ledger.has_backup(tier="daily", date_key="2026-08-19")
    latest = ledger.latest_backup()
    assert latest is not None
    assert latest.key == "db/daily/app-1.db.gz"


# --- status counts -----------------------------------------------------------


def test_kind_counts_and_totals(session: Session):
    _blob(session, "a" * 64)
    _blob(session, "b" * 64)
    ledger = CloudLedgerRepository(session)
    ledger.record_uploaded(
        kind="monitor_blob", source_ref="a" * 64, key="k1", sha256="x", size_bytes=1
    )
    ledger.record_failed(kind="monitor_blob", source_ref="b" * 64, key="k2", error_message="e")
    session.commit()

    counts = ledger.kind_counts("monitor_blob")
    assert counts == {"uploaded": 1, "failed": 1}
    assert ledger.monitor_blob_total(since=T0) == 2
