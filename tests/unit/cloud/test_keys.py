"""B2 object key layout (task-52 §3.1) — pure functions."""

from __future__ import annotations

from app.cloud.keys import (
    db_backup_key,
    db_backup_manifest_key,
    monitor_blob_key,
    report_object_key,
)


def test_monitor_blob_key_is_content_addressed():
    sha = "abcd1234" + "0" * 56
    key = monitor_blob_key(sha)
    assert key == f"archive/ab/{sha}.gz"


def test_monitor_blob_key_identical_bytes_share_a_key():
    sha = "f" * 64
    assert monitor_blob_key(sha) == monitor_blob_key(sha)


def test_db_backup_key_layout_per_tier():
    ts = "20260819T033000Z"
    assert db_backup_key("daily", ts) == "db/daily/app-20260819T033000Z.db.gz"
    assert db_backup_key("weekly", ts) == "db/weekly/app-20260819T033000Z.db.gz"
    assert db_backup_manifest_key("monthly", ts) == "db/monthly/app-20260819T033000Z.manifest.json"


def test_report_object_key_no_disambiguator():
    key = report_object_key("acme", 2026, "acme__monthly__2026-07-01_2026-07-31.pdf")
    assert key == "reports/acme/2026/acme__monthly__2026-07-01_2026-07-31.pdf"


def test_report_object_key_disambiguator_inserted_before_extension():
    key = report_object_key(
        "acme", 2026, "acme__monthly__2026-07-01_2026-07-31.pdf", disambiguator="a1b2c3d4"
    )
    assert key == "reports/acme/2026/acme__monthly__2026-07-01_2026-07-31-a1b2c3d4.pdf"


def test_report_object_key_disambiguator_without_extension():
    key = report_object_key("acme", 2026, "no_extension_name", disambiguator="deadbeef")
    assert key == "reports/acme/2026/no_extension_name-deadbeef"
