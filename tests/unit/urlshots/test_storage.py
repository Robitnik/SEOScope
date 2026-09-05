"""Content-addressed screenshot store (task-64 §2.3) — no gzip, own root
directory, same root-escape guard as `app.monitor.storage`."""

from __future__ import annotations

import hashlib

import pytest

from app.urlshots.storage import (
    resolve_shot_path,
    sha256_hex,
    shot_exists,
    store_shot,
)


def test_identical_bytes_are_stored_once(tmp_path):
    data = b"fake-jpeg-bytes"
    first = store_shot(tmp_path, data)
    second = store_shot(tmp_path, data)
    assert first.sha256 == second.sha256 == sha256_hex(data)
    matches = list(tmp_path.rglob(f"{first.sha256}.jpg"))
    assert len(matches) == 1


def test_different_bytes_get_different_addresses(tmp_path):
    a = store_shot(tmp_path, b"AAAA")
    b = store_shot(tmp_path, b"BBBB")
    assert a.sha256 != b.sha256


def test_stored_bytes_round_trip_uncompressed(tmp_path):
    data = b"\xff\xd8\xff" + b"x" * 1000  # jpeg-ish magic bytes
    stored = store_shot(tmp_path, data)
    assert (tmp_path / stored.path).read_bytes() == data


def test_lives_under_its_own_urlshots_root(tmp_path):
    stored = store_shot(tmp_path, b"data")
    assert stored.path.startswith("urlshots/")


def test_resolve_shot_path_refuses_to_escape_the_artifacts_root(tmp_path):
    with pytest.raises(PermissionError):
        resolve_shot_path(tmp_path, "../../../etc/passwd")


def test_shot_exists_reflects_disk_state(tmp_path):
    stored = store_shot(tmp_path, b"data")
    assert shot_exists(tmp_path, stored.sha256) is True
    assert shot_exists(tmp_path, "0" * 64) is False


def test_sha256_hex_matches_hashlib():
    assert sha256_hex(b"abc") == hashlib.sha256(b"abc").hexdigest()
