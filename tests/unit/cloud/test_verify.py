"""`cloud-verify` (task-52 §3.9): a re-downloaded object's sha256 must match
what the ledger recorded at upload time — the check that a backup nobody
has read is not actually a verified backup.
"""

from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from app.cloud.verify import verify_sample
from app.db.repositories.cloud import CloudLedgerRepository

from .fakes import FakeCloudStore


def _seed(
    session: Session, store: FakeCloudStore, *, kind: str, source_ref: str, key: str, data: bytes
) -> None:
    store.objects[key] = data
    CloudLedgerRepository(session).record_uploaded(
        kind=kind,
        source_ref=source_ref,
        key=key,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )
    session.commit()


def test_verify_sample_matching_content_is_ok(session: Session):
    store = FakeCloudStore()
    _seed(session, store, kind="monitor_blob", source_ref="a", key="archive/a/a.gz", data=b"hello")

    outcomes = verify_sample(session, store, kind=None, sample=10)

    assert len(outcomes) == 1
    assert outcomes[0].ok
    assert outcomes[0].detail == "ok"


def test_verify_sample_detects_sha256_mismatch(session: Session):
    store = FakeCloudStore()
    _seed(session, store, kind="monitor_blob", source_ref="a", key="archive/a/a.gz", data=b"hello")
    store.objects["archive/a/a.gz"] = b"tampered"  # bytes in the cloud no longer match the ledger

    outcomes = verify_sample(session, store, kind=None, sample=10)

    assert len(outcomes) == 1
    assert not outcomes[0].ok
    assert outcomes[0].detail == "sha256_mismatch"


def test_verify_sample_download_failure_is_reported_not_raised(session: Session):
    store = FakeCloudStore()
    _seed(session, store, kind="monitor_blob", source_ref="a", key="archive/a/a.gz", data=b"hello")
    del store.objects["archive/a/a.gz"]  # simulate the object having vanished from the bucket

    outcomes = verify_sample(session, store, kind=None, sample=10)

    assert len(outcomes) == 1
    assert not outcomes[0].ok
    assert outcomes[0].detail.startswith("download_failed:")


def test_verify_sample_filters_by_kind(session: Session):
    store = FakeCloudStore()
    _seed(session, store, kind="monitor_blob", source_ref="a", key="k1", data=b"x")
    _seed(session, store, kind="report_file", source_ref="b", key="k2", data=b"y")

    outcomes = verify_sample(session, store, kind="report_file", sample=10)

    assert len(outcomes) == 1
    assert outcomes[0].kind == "report_file"


def test_verify_sample_respects_sample_size(session: Session):
    store = FakeCloudStore()
    for i in range(5):
        _seed(
            session,
            store,
            kind="monitor_blob",
            source_ref=str(i),
            key=f"k{i}",
            data=f"{i}".encode(),
        )

    outcomes = verify_sample(session, store, kind=None, sample=2)

    assert len(outcomes) == 2
