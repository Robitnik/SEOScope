"""Тести persistence-шару інструмента Image Alt Text (task-27)."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import AltTextItem, AltTextReport, Client
from app.db.repositories.alt_text import AltTextItemInput, AltTextRepository, ItemFilters

UPLOADS = "https://acme.test/wp-content/uploads/2024/03"


def _item(index: int, **overrides: object) -> AltTextItemInput:
    payload: dict[str, object] = {
        "row_index": index,
        "image_url": f"{UPLOADS}/photo-{index}-300x200.jpg",
        "search_key": f"photo-{index}",
        "search_key_fallback": None,
        "url_kind": "uploads",
        "derivation": {"stripped": ["-300x200"], "warnings": []},
        "new_alt": f"Alt number {index}",
        "old_alt": None,
        "page_urls": [f"https://acme.test/page-{index}"],
        "extra": {"Notes": "from audit"},
    }
    payload.update(overrides)
    return AltTextItemInput(**payload)  # type: ignore[arg-type]


def _create(session: Session, *, client_id: str | None = None, count: int = 3) -> AltTextReport:
    return AltTextRepository(session).create_report(
        client_id=client_id,
        title="March image audit",
        source_filename="audit.xlsx",
        source_kind="xlsx",
        source_sheet="Images",
        source_sha256="a" * 64,
        column_map={"image_url": 0, "new_alt": 1, "page_url": 2},
        skipped_count=2,
        warnings=["rows_without_new_alt"],
        items=[_item(index) for index in range(1, count + 1)],
    )


def test_create_report_persists_rows_and_counts(session: Session) -> None:
    report = _create(session)
    session.commit()

    assert report.row_count == 3
    assert report.skipped_count == 2
    assert report.warnings_json == ["rows_without_new_alt"]
    assert report.column_map_json == {"image_url": 0, "new_alt": 1, "page_url": 2}
    items = AltTextRepository(session).all_items(report.id)
    assert [item.row_index for item in items] == [1, 2, 3]
    assert items[0].status == "pending"
    assert items[0].status_changed_at is None
    assert items[0].derivation_json["stripped"] == ["-300x200"]
    assert items[0].extra_json == {"Notes": "from audit"}


def test_report_can_be_bound_to_a_client(session: Session) -> None:
    client = Client(name="Acme", slug="acme", domain="acme.test", timezone="UTC")
    session.add(client)
    session.flush()
    report = _create(session, client_id=client.id)
    session.commit()
    assert report.client_id == client.id


def test_report_without_a_client_is_valid(session: Session) -> None:
    # Аудит проспекта: клієнта в системі ще нема, звіт усе одно має жити.
    report = _create(session)
    session.commit()
    assert report.client_id is None


def test_row_index_is_unique_within_a_report(session: Session) -> None:
    report = _create(session)
    session.commit()
    session.add(AltTextItem(report_id=report.id, row_index=1, image_url="x", search_key="x"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_same_row_index_in_another_report_is_fine(session: Session) -> None:
    first = _create(session)
    second = AltTextRepository(session).create_report(
        client_id=None,
        title="Another",
        source_filename="other.csv",
        source_kind="csv",
        source_sheet=None,
        source_sha256="b" * 64,
        column_map={"image_url": 0, "new_alt": 1},
        skipped_count=0,
        warnings=[],
        items=[_item(1)],
    )
    session.commit()
    assert first.id != second.id
    assert AltTextRepository(session).all_items(second.id)[0].row_index == 1


# --- ідемпотентність джерела --------------------------------------------------


def test_find_by_source_matches_file_and_sheet(session: Session) -> None:
    report = _create(session)
    session.commit()
    repository = AltTextRepository(session)
    assert repository.find_by_source("a" * 64, "Images") is not None
    assert repository.find_by_source("a" * 64, "Other sheet") is None
    assert repository.find_by_source("c" * 64, "Images") is None
    assert repository.find_by_source("a" * 64, None) is None
    assert report.source_sheet == "Images"


def test_find_by_source_handles_a_sheetless_csv(session: Session) -> None:
    AltTextRepository(session).create_report(
        client_id=None,
        title="CSV",
        source_filename="audit.csv",
        source_kind="csv",
        source_sheet=None,
        source_sha256="d" * 64,
        column_map={"image_url": 0, "new_alt": 1},
        skipped_count=0,
        warnings=[],
        items=[_item(1)],
    )
    session.commit()
    assert AltTextRepository(session).find_by_source("d" * 64, None) is not None


# --- статуси прогресу ---------------------------------------------------------


def test_set_status_stamps_the_change_time(session: Session) -> None:
    report = _create(session)
    session.commit()
    repository = AltTextRepository(session)
    item = repository.all_items(report.id)[0]

    repository.set_status(item, "copied")
    session.commit()
    assert item.status == "copied"
    assert item.status_changed_at is not None


def test_reset_to_pending_clears_the_change_time(session: Session) -> None:
    report = _create(session)
    session.commit()
    repository = AltTextRepository(session)
    item = repository.all_items(report.id)[0]
    repository.set_status(item, "copied")
    repository.set_status(item, "pending")
    session.commit()
    assert item.status == "pending"
    assert item.status_changed_at is None


def test_set_status_rejects_an_unknown_value(session: Session) -> None:
    report = _create(session)
    session.commit()
    item = AltTextRepository(session).all_items(report.id)[0]
    with pytest.raises(ValueError):
        AltTextRepository(session).set_status(item, "done")


def test_status_counts_include_every_status(session: Session) -> None:
    report = _create(session)
    session.commit()
    repository = AltTextRepository(session)
    items = repository.all_items(report.id)
    repository.set_status(items[0], "copied")
    repository.set_status(items[1], "skipped")
    session.commit()

    assert repository.status_counts(report.id) == {
        "pending": 1,
        "copied": 1,
        "applied": 0,
        "skipped": 1,
    }


# --- результат перевірки ------------------------------------------------------


def test_verify_result_is_stored(session: Session) -> None:
    report = _create(session)
    session.commit()
    repository = AltTextRepository(session)
    item = repository.all_items(report.id)[0]

    repository.set_verify_result(
        item, status="mismatch", found_alt="IMG_0042", http_status=200, detail=None
    )
    session.commit()
    assert item.verify_status == "mismatch"
    assert item.verify_alt == "IMG_0042"
    assert item.verify_http_status == 200
    assert item.verify_checked_at is not None
    # Невідповідність не рухає прогрес оператора.
    assert item.status == "pending"


def test_a_confirmed_match_promotes_the_row_to_applied(session: Session) -> None:
    report = _create(session)
    session.commit()
    repository = AltTextRepository(session)
    item = repository.all_items(report.id)[0]

    repository.set_verify_result(
        item, status="match", found_alt="Alt number 1", http_status=200, detail=None
    )
    session.commit()
    assert item.status == "applied"
    assert item.status_changed_at == item.verify_checked_at


def test_failed_fetch_does_not_touch_the_status(session: Session) -> None:
    report = _create(session)
    session.commit()
    repository = AltTextRepository(session)
    item = repository.all_items(report.id)[0]
    repository.set_status(item, "copied")
    repository.set_verify_result(
        item, status="fetch_failed", found_alt=None, http_status=503, detail="HTTP 503"
    )
    session.commit()
    assert item.status == "copied"


def test_verify_counts_ignore_unchecked_rows(session: Session) -> None:
    report = _create(session)
    session.commit()
    repository = AltTextRepository(session)
    items = repository.all_items(report.id)
    repository.set_verify_result(
        items[0], status="match", found_alt="Alt number 1", http_status=200, detail=None
    )
    repository.set_verify_result(
        items[1], status="empty_alt", found_alt="", http_status=200, detail=None
    )
    session.commit()

    assert repository.verify_counts(report.id) == {"match": 1, "empty_alt": 1}
    assert repository.verifiable_count(report.id) == 3


def test_verifiable_count_excludes_rows_without_a_page(session: Session) -> None:
    report = AltTextRepository(session).create_report(
        client_id=None,
        title="No pages",
        source_filename="a.csv",
        source_kind="csv",
        source_sheet=None,
        source_sha256="e" * 64,
        column_map={"image_url": 0, "new_alt": 1},
        skipped_count=0,
        warnings=[],
        items=[_item(1, page_urls=[]), _item(2)],
    )
    session.commit()
    assert AltTextRepository(session).verifiable_count(report.id) == 1


# --- читання й фільтри --------------------------------------------------------


def test_list_items_paginates_in_source_order(session: Session) -> None:
    report = _create(session, count=5)
    session.commit()
    rows, total = AltTextRepository(session).list_items(
        report.id, filters=ItemFilters(), limit=2, offset=2
    )
    assert total == 5
    assert [row.row_index for row in rows] == [3, 4]


def test_list_items_filters_by_status_and_verdict(session: Session) -> None:
    report = _create(session, count=4)
    session.commit()
    repository = AltTextRepository(session)
    items = repository.all_items(report.id)
    repository.set_status(items[0], "copied")
    repository.set_verify_result(
        items[1], status="mismatch", found_alt="x", http_status=200, detail=None
    )
    session.commit()

    copied, total = repository.list_items(
        report.id, filters=ItemFilters(status="copied"), limit=50, offset=0
    )
    assert total == 1 and copied[0].row_index == 1
    mismatched, _ = repository.list_items(
        report.id, filters=ItemFilters(verify_status="mismatch"), limit=50, offset=0
    )
    assert [row.row_index for row in mismatched] == [2]


def test_list_items_search_covers_url_alt_and_search_key(session: Session) -> None:
    report = _create(session, count=3)
    session.commit()
    repository = AltTextRepository(session)

    by_alt, _ = repository.list_items(
        report.id, filters=ItemFilters(query="alt number 2"), limit=50, offset=0
    )
    assert [row.row_index for row in by_alt] == [2]
    by_key, _ = repository.list_items(
        report.id, filters=ItemFilters(query="PHOTO-3"), limit=50, offset=0
    )
    assert [row.row_index for row in by_key] == [3]
    by_url, _ = repository.list_items(
        report.id, filters=ItemFilters(query="uploads/2024"), limit=50, offset=0
    )
    assert len(by_url) == 3


def test_list_items_filters_by_url_kind_and_page_presence(session: Session) -> None:
    report = AltTextRepository(session).create_report(
        client_id=None,
        title="Mixed",
        source_filename="a.csv",
        source_kind="csv",
        source_sheet=None,
        source_sha256="f" * 64,
        column_map={"image_url": 0, "new_alt": 1},
        skipped_count=0,
        warnings=[],
        items=[_item(1), _item(2, url_kind="theme_asset", page_urls=[])],
    )
    session.commit()
    repository = AltTextRepository(session)

    theme, _ = repository.list_items(
        report.id, filters=ItemFilters(url_kind="theme_asset"), limit=50, offset=0
    )
    assert [row.row_index for row in theme] == [2]
    with_page, _ = repository.list_items(
        report.id, filters=ItemFilters(has_page=True), limit=50, offset=0
    )
    assert [row.row_index for row in with_page] == [1]


def test_get_item_refuses_a_row_from_another_report(session: Session) -> None:
    first = _create(session)
    second = AltTextRepository(session).create_report(
        client_id=None,
        title="Other",
        source_filename="b.csv",
        source_kind="csv",
        source_sheet=None,
        source_sha256="0" * 64,
        column_map={"image_url": 0, "new_alt": 1},
        skipped_count=0,
        warnings=[],
        items=[_item(1)],
    )
    session.commit()
    repository = AltTextRepository(session)
    foreign = repository.all_items(second.id)[0]
    assert repository.get_item(first.id, foreign.id) is None
    assert repository.get_item(second.id, foreign.id) is not None


def test_list_reports_is_newest_first(session: Session) -> None:
    _create(session)
    AltTextRepository(session).create_report(
        client_id=None,
        title="Newer",
        source_filename="b.csv",
        source_kind="csv",
        source_sheet=None,
        source_sha256="1" * 64,
        column_map={"image_url": 0, "new_alt": 1},
        skipped_count=0,
        warnings=[],
        items=[_item(1)],
    )
    session.commit()
    reports, total = AltTextRepository(session).list_reports(limit=10, offset=0)
    assert total == 2
    assert reports[0].created_at >= reports[1].created_at


def test_delete_report_removes_its_rows(session: Session) -> None:
    report = _create(session)
    session.commit()
    repository = AltTextRepository(session)

    assert repository.delete_report(report.id) is True
    session.commit()
    assert repository.get_report(report.id) is None
    assert repository.all_items(report.id) == []


def test_delete_missing_report_is_false_not_an_error(session: Session) -> None:
    assert AltTextRepository(session).delete_report("00000000-0000-0000-0000-000000000000") is False


# --- WordPress media ID (task-39) -----------------------------------------------


def test_media_result_is_stored(session: Session) -> None:
    report = _create(session)
    session.commit()
    repository = AltTextRepository(session)
    item = repository.all_items(report.id)[0]

    repository.set_media_result(
        item,
        media_id=42,
        status="resolved",
        confidence="exact",
        alt_text="Team at the office",
        admin_url="https://acme.test/wp-admin/upload.php?item=42",
        detail=None,
        candidates=[],
    )
    session.commit()

    assert item.wp_media_id == 42
    assert item.wp_media_status == "resolved"
    assert item.wp_media_confidence == "exact"
    assert item.wp_media_alt == "Team at the office"
    assert item.wp_media_admin_url == "https://acme.test/wp-admin/upload.php?item=42"
    assert item.wp_media_json == {"candidates": []}
    assert item.wp_media_checked_at is not None


def test_media_status_counts_ignore_unresolved_rows(session: Session) -> None:
    report = _create(session)
    session.commit()
    repository = AltTextRepository(session)
    items = repository.all_items(report.id)
    repository.set_media_result(
        items[0],
        media_id=1,
        status="resolved",
        confidence="exact",
        alt_text=None,
        admin_url=None,
        detail=None,
        candidates=[],
    )
    repository.set_media_result(
        items[1],
        media_id=None,
        status="ambiguous",
        confidence=None,
        alt_text=None,
        admin_url=None,
        detail="2 attachment(s) matched",
        candidates=[{"id": 1, "file": None, "source_url": None, "alt_text": None, "title": None}],
    )
    session.commit()

    assert repository.media_status_counts(report.id) == {"resolved": 1, "ambiguous": 1}


def test_list_unresolved_items_only_returns_uploads_rows_without_a_status(
    session: Session,
) -> None:
    report = AltTextRepository(session).create_report(
        client_id=None,
        title="Mixed sources",
        source_filename="a.csv",
        source_kind="csv",
        source_sheet=None,
        source_sha256="c" * 64,
        column_map={"image_url": 0, "new_alt": 1},
        skipped_count=0,
        warnings=[],
        items=[
            _item(1, url_kind="uploads"),
            _item(2, url_kind="theme_asset"),
            _item(3, url_kind="uploads"),
        ],
    )
    session.commit()
    repository = AltTextRepository(session)
    items = repository.all_items(report.id)
    repository.set_media_result(
        items[0],
        media_id=7,
        status="resolved",
        confidence="exact",
        alt_text=None,
        admin_url=None,
        detail=None,
        candidates=[],
    )
    session.commit()

    unresolved = repository.list_unresolved_items(report.id, limit=10)
    assert [item.row_index for item in unresolved] == [3]
    assert repository.unresolved_count(report.id) == 1


def test_list_unresolved_items_respects_the_limit(session: Session) -> None:
    report = _create(session, count=5)
    session.commit()
    repository = AltTextRepository(session)

    unresolved = repository.list_unresolved_items(report.id, limit=2)
    assert len(unresolved) == 2
