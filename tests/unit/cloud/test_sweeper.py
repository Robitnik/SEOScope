"""Pull-sweeper (task-52 §3.4): idempotency, honest `local_file_missing`
failures, retry-next-tick for a `failed` row, the "only new" watermark, and
the report-file name-collision guard — all against `FakeCloudStore`, never
real B2 (see `tests/unit/cloud/test_client.py`'s docstring for why).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.cloud.keys import monitor_blob_key, report_object_key
from app.cloud.sweeper import ARCHIVE_WATERMARK, sweep_monitor_blobs, sweep_report_files
from app.core.config import Settings
from app.db.models import MonitorBlob, ReportFile
from app.db.repositories.clients import ClientRepository
from app.db.repositories.cloud import CloudLedgerRepository

from .fakes import FakeCloudStore

T0 = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        artifacts_dir=tmp_path,
        cloud_enabled=True,
        b2_vault_bucket_name="vault",
        b2_vault_key_id="key-id",
        b2_vault_application_key="secret",
        **overrides,
    )


def _write_blob(artifacts_dir: Path, sha256: str, data: bytes = b"gzip-bytes") -> MonitorBlob:
    path = artifacts_dir / "monitor" / sha256[:2] / f"{sha256}.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return MonitorBlob(
        sha256=sha256,
        size_bytes=len(data) * 2,
        gzip_size_bytes=len(data),
        mime="text/html",
        path=f"monitor/{sha256[:2]}/{sha256}.gz",
        first_seen_at=T0,
        last_seen_at=T0,
    )


def _fix_watermark_before(session: Session, when: datetime) -> None:
    """Pin the "only new" watermark (task-52 §3.4) well before `when` so a
    test's fixed `T0` timestamp can never lose a race against the sweeper's
    own default of "now" (real wall-clock time) on first activation."""
    CloudLedgerRepository(session).set_watermark(ARCHIVE_WATERMARK, when - timedelta(days=1))
    session.commit()


def _client(session: Session) -> str:
    client = ClientRepository(session).create(
        name="Acme", slug="acme", domain="acme.com", timezone="America/New_York"
    )
    session.commit()
    return client.id


def _write_report_file(
    artifacts_dir: Path, client_id: str, *, rel_path: str, data: bytes = b"pdf-bytes"
) -> ReportFile:
    path = artifacts_dir / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return ReportFile(
        client_id=client_id,
        kind="pdf",
        path=rel_path,
        sha256="deadbeef" * 8,
        size_bytes=len(data),
        mime="application/pdf",
        created_at=T0,
    )


# --- monitor blobs -----------------------------------------------------------


def test_sweep_monitor_blobs_uploads_and_records_ledger(session: Session, tmp_path: Path):
    settings = _settings(tmp_path)
    blob = _write_blob(settings.artifacts_dir, "a" * 64)
    session.add(blob)
    session.commit()
    _fix_watermark_before(session, T0)

    store = FakeCloudStore()
    result = sweep_monitor_blobs(session, store, settings)

    assert result.uploaded == 1
    assert result.failed == 0
    key = monitor_blob_key("a" * 64)
    assert store.objects[key] == b"gzip-bytes"

    ledger = CloudLedgerRepository(session)
    obj = ledger.get("monitor_blob", "a" * 64)
    assert obj is not None
    assert obj.status == "uploaded"
    assert obj.key == key


def test_sweep_monitor_blobs_is_idempotent(session: Session, tmp_path: Path):
    settings = _settings(tmp_path)
    blob = _write_blob(settings.artifacts_dir, "a" * 64)
    session.add(blob)
    session.commit()
    _fix_watermark_before(session, T0)
    store = FakeCloudStore()

    sweep_monitor_blobs(session, store, settings)
    second = sweep_monitor_blobs(session, store, settings)

    assert second.attempted == 0
    assert len(store.uploads) == 1


def test_sweep_monitor_blobs_missing_local_file_is_an_honest_failure(
    session: Session, tmp_path: Path
):
    settings = _settings(tmp_path)
    blob = MonitorBlob(
        sha256="b" * 64, size_bytes=10, gzip_size_bytes=5, mime=None,
        path="monitor/bb/" + "b" * 64 + ".gz", first_seen_at=T0, last_seen_at=T0,
    )
    session.add(blob)
    session.commit()
    _fix_watermark_before(session, T0)
    store = FakeCloudStore()

    result = sweep_monitor_blobs(session, store, settings)

    assert result.failed == 1
    assert result.reasons == {"local_file_missing": 1}
    ledger = CloudLedgerRepository(session)
    obj = ledger.get("monitor_blob", "b" * 64)
    assert obj is not None
    assert obj.status == "failed"
    assert obj.error_message == "local_file_missing"


def test_sweep_monitor_blobs_retries_a_failed_row_next_tick(session: Session, tmp_path: Path):
    settings = _settings(tmp_path)
    blob = _write_blob(settings.artifacts_dir, "a" * 64)
    session.add(blob)
    session.commit()
    _fix_watermark_before(session, T0)
    key = monitor_blob_key("a" * 64)
    store = FakeCloudStore()
    store.fail_keys.add(key)

    first = sweep_monitor_blobs(session, store, settings)
    assert first.failed == 1
    assert CloudLedgerRepository(session).get("monitor_blob", "a" * 64).status == "failed"

    store.fail_keys.discard(key)
    second = sweep_monitor_blobs(session, store, settings)

    assert second.uploaded == 1
    assert CloudLedgerRepository(session).get("monitor_blob", "a" * 64).status == "uploaded"


def test_sweep_monitor_blobs_only_syncs_new_since_the_watermark(session: Session, tmp_path: Path):
    settings = _settings(tmp_path)
    old_blob = _write_blob(settings.artifacts_dir, "a" * 64)
    old_blob.first_seen_at = T0 - timedelta(days=5)
    session.add(old_blob)
    session.commit()
    store = FakeCloudStore()

    # First tick fixes the watermark at "now" (>= T0) — the pre-existing
    # blob predates activation and must never be archived (task-52 §8: the
    # historical backlog is a deliberate, separate backfill).
    result = sweep_monitor_blobs(session, store, settings)

    assert result.attempted == 0
    assert store.uploads == []


def test_sweep_monitor_blobs_disabled_store_is_a_noop(session: Session, tmp_path: Path):
    settings = _settings(tmp_path)
    blob = _write_blob(settings.artifacts_dir, "a" * 64)
    session.add(blob)
    session.commit()
    store = FakeCloudStore(enabled=False)

    result = sweep_monitor_blobs(session, store, settings)

    assert result.attempted == 0
    assert store.uploads == []
    assert CloudLedgerRepository(session).get("monitor_blob", "a" * 64) is None


def test_sweep_monitor_blobs_respects_limit(session: Session, tmp_path: Path):
    settings = _settings(tmp_path)
    for i in range(3):
        sha = f"{i}" * 64
        blob = _write_blob(settings.artifacts_dir, sha)
        blob.first_seen_at = T0 + timedelta(minutes=i)
        session.add(blob)
    session.commit()
    _fix_watermark_before(session, T0)
    store = FakeCloudStore()

    result = sweep_monitor_blobs(session, store, settings, limit=2)

    assert result.attempted == 2


# --- report files -------------------------------------------------------------


def test_sweep_report_files_uploads_and_records(session: Session, tmp_path: Path):
    settings = _settings(tmp_path)
    client_id = _client(session)
    rf = _write_report_file(settings.artifacts_dir, client_id, rel_path="reports/run-1/a.pdf")
    session.add(rf)
    session.commit()
    store = FakeCloudStore()

    result = sweep_report_files(session, store, settings)

    assert result.uploaded == 1
    expected_key = report_object_key("acme", 2026, "a.pdf")
    assert store.objects[expected_key] == b"pdf-bytes"


def test_sweep_report_files_disambiguates_a_name_collision(session: Session, tmp_path: Path):
    settings = _settings(tmp_path)
    client_id = _client(session)
    rf1 = _write_report_file(settings.artifacts_dir, client_id, rel_path="reports/run-1/a.pdf")
    rf1.sha256 = "1" * 64
    session.add(rf1)
    session.commit()
    store = FakeCloudStore()
    sweep_report_files(session, store, settings)

    rf2 = _write_report_file(
        settings.artifacts_dir, client_id, rel_path="reports/run-2/a.pdf", data=b"different-bytes"
    )
    rf2.sha256 = "2" * 64
    session.add(rf2)
    session.commit()
    result = sweep_report_files(session, store, settings)

    assert result.uploaded == 1
    base_key = report_object_key("acme", 2026, "a.pdf")
    disambiguated_key = report_object_key("acme", 2026, "a.pdf", disambiguator="2" * 8)
    assert store.objects[base_key] == b"pdf-bytes"
    assert store.objects[disambiguated_key] == b"different-bytes"
