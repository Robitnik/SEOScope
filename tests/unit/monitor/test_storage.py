"""Content-addressed blob store (task-35 §5)."""

from __future__ import annotations

import pytest

from app.monitor.storage import (
    delete_blob_file,
    read_blob,
    resolve_blob_path,
    sha256_hex,
    store_blob,
)


def test_identical_bytes_are_stored_once(tmp_path):
    data = b"hello world"
    first = store_blob(tmp_path, data)
    second = store_blob(tmp_path, data)
    assert first.sha256 == second.sha256 == sha256_hex(data)
    # Only one file on disk for this content, regardless of how many times
    # `store_blob` was called with the same bytes.
    matches = list(tmp_path.rglob(f"{first.sha256}.gz"))
    assert len(matches) == 1


def test_different_bytes_get_different_addresses(tmp_path):
    a = store_blob(tmp_path, b"AAAA")
    b = store_blob(tmp_path, b"BBBB")
    assert a.sha256 != b.sha256


def test_stored_blob_is_gzip_compressed_and_round_trips(tmp_path):
    data = b"x" * 10_000
    stored = store_blob(tmp_path, data)
    raw_path = tmp_path / stored.path
    with open(raw_path, "rb") as handle:
        assert handle.read(2) == b"\x1f\x8b"  # gzip magic
    assert read_blob(tmp_path, stored.sha256) == data
    assert stored.gzip_size_bytes < stored.size_bytes


def test_resolve_blob_path_refuses_to_escape_the_artifacts_root(tmp_path):
    with pytest.raises(PermissionError):
        resolve_blob_path(tmp_path, "../../../etc/passwd")


def test_delete_blob_file_removes_it_and_reports_whether_it_existed(tmp_path):
    stored = store_blob(tmp_path, b"data")
    assert delete_blob_file(tmp_path, stored.sha256) is True
    assert delete_blob_file(tmp_path, stored.sha256) is False


def test_sha256_hex_matches_hashlib():
    import hashlib

    assert sha256_hex(b"abc") == hashlib.sha256(b"abc").hexdigest()
