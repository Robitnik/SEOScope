"""Наскрізні API-тести інструмента Image Alt Text і каталогу Tools (task-27)."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from openpyxl import Workbook

from app.tools.alt_text.verify import FetchedPage
from app.tools.alt_text.wp_rest import RestResponse

UPLOADS = "https://acme.example.com/wp-content/uploads/2024/03"

AUDIT_SHEET = [
    ["Image URL", "New alt text", "Page URL", "Current alt", "Notes"],
    [
        f"{UPLOADS}/hero-image-1024x683.jpg",
        "Team at the Kyiv office",
        "https://acme.example.com/about",
        "",
        "priority",
    ],
    [
        f"{UPLOADS}/logo-scaled.png",
        "The Yovth logo",
        "",
        "logo",
        "",
    ],
    [
        "https://acme.example.com/wp-content/themes/acme/img/pattern.svg",
        "Decorative pattern",
        "https://acme.example.com/",
        "",
        "",
    ],
]


def _xlsx(sheets: dict[str, list[list[object]]]) -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        worksheet = workbook.create_sheet(title=name)
        for row in rows:
            worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _upload(client: Any, content: bytes, filename: str = "audit.xlsx") -> Any:
    return client.post(
        "/api/v1/tools/alt-text/uploads",
        content=content,
        headers={"X-Filename": filename, "Content-Type": "application/octet-stream"},
    )


def _create_report(client: Any, _source: bytes | None = None, **overrides: Any) -> dict[str, Any]:
    upload = _upload(client, _source if _source is not None else _xlsx({"Images": AUDIT_SHEET}))
    assert upload.status_code == 200, upload.text
    payload = {
        "upload_id": upload.json()["upload_id"],
        "title": "March image audit",
        "sheet": "Images",
        "column_map": {"image_url": 0, "new_alt": 1, "page_url": 2, "old_alt": 3},
    }
    payload.update(overrides)
    response = client.post("/api/v1/tools/alt-text/reports", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --- каталог ------------------------------------------------------------------


def test_tools_catalog_lists_the_alt_text_tool(api_client: Any) -> None:
    response = api_client.get("/api/v1/tools")
    assert response.status_code == 200
    tools = response.json()["tools"]
    alt_text = next(tool for tool in tools if tool["key"] == "alt-text")
    assert alt_text["route"] == "#/tools/alt-text"
    assert alt_text["status"] == "available"
    # Ще жодного звіту — статистики немає, і це `None`, а не 0 (PLAN §12).
    assert alt_text["stat_value"] is None


def test_tools_catalog_reports_real_usage(api_client: Any) -> None:
    _create_report(api_client)
    tools = api_client.get("/api/v1/tools").json()["tools"]
    alt_text = next(tool for tool in tools if tool["key"] == "alt-text")
    assert alt_text["stat_value"] == "1"
    assert alt_text["stat_label"] == "report"


# --- крок 1: upload -----------------------------------------------------------


def test_upload_returns_sheets_headers_and_a_suggested_mapping(api_client: Any) -> None:
    content = _xlsx({"Summary": [["Report", "March"]], "Images": AUDIT_SHEET})
    response = _upload(api_client, content)
    assert response.status_code == 200
    body = response.json()

    assert body["source_kind"] == "xlsx"
    assert [sheet["name"] for sheet in body["sheets"]] == ["Summary", "Images"]
    assert body["default_sheet"] == "Images"
    images = next(sheet for sheet in body["sheets"] if sheet["name"] == "Images")
    assert images["headers"][:2] == ["Image URL", "New alt text"]
    assert images["suggested_column_map"] == {
        "image_url": 0,
        "new_alt": 1,
        "page_url": 2,
        "old_alt": 3,
    }
    assert len(images["sample_rows"]) == 3


def test_upload_accepts_csv(api_client: Any) -> None:
    raw = b"Image URL,New alt\nhttps://a.test/wp-content/uploads/x.jpg,An alt\n"
    body = _upload(api_client, raw, "audit.csv").json()
    assert body["source_kind"] == "csv"
    assert body["delimiter"] == ","
    assert body["encoding"] == "utf-8-sig"


def test_upload_rejects_legacy_xls_with_a_next_step(api_client: Any) -> None:
    response = _upload(api_client, b"\xd0\xcf\x11\xe0legacy-biff", "audit.xls")
    assert response.status_code == 415
    detail = response.json()["detail"]
    assert detail["error_code"] == "unsupported_source"
    assert ".xlsx" in detail["message"]


def test_upload_rejects_an_empty_body(api_client: Any) -> None:
    response = _upload(api_client, b"", "audit.csv")
    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "empty_upload"


def test_upload_rejects_an_oversized_file(api_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import tools_alt_text

    monkeypatch.setattr(tools_alt_text, "MAX_UPLOAD_BYTES", 32)
    response = _upload(api_client, b"Image URL,New alt\n" + b"x" * 128, "audit.csv")
    assert response.status_code == 413
    assert response.json()["detail"]["error_code"] == "upload_too_large"


def test_upload_rejects_binary_garbage(api_client: Any) -> None:
    response = _upload(api_client, b"\x89PNG\r\n\x1a\n\x00\x00\x00rubbish", "photo.png")
    assert response.status_code == 415


# --- крок 2: створення звіту ---------------------------------------------------


def test_create_report_derives_search_keys_and_persists_rows(api_client: Any) -> None:
    report = _create_report(api_client)
    assert report["row_count"] == 3
    assert report["skipped_count"] == 0
    assert report["status_counts"] == {"pending": 3, "copied": 0, "applied": 0, "skipped": 0}
    assert report["verifiable_count"] == 2  # у другого рядка немає сторінки

    rows = api_client.get(f"/api/v1/tools/alt-text/reports/{report['id']}/items").json()["rows"]
    hero, logo, pattern = rows
    assert hero["search_key"] == "hero-image"
    assert hero["stripped"] == ["-1024x683"]
    assert hero["url_kind"] == "uploads"
    assert hero["searchable"] is True
    assert hero["new_alt"] == "Team at the Kyiv office"
    assert hero["extra"] == {"Notes": "priority"}
    assert hero["status"] == "pending"
    assert hero["verify_status"] is None

    assert logo["search_key"] == "logo"
    assert logo["page_url"] is None
    assert logo["old_alt"] == "logo"

    # Файл теми — не медіатека: чесний стан, а не «схожий» фрагмент.
    assert pattern["url_kind"] == "theme_asset"
    assert pattern["searchable"] is False


def test_create_report_can_bind_a_client_and_flag_foreign_hosts(
    api_client: Any, seed_client: Any
) -> None:
    client_id = seed_client(domain="acme.example.com")
    sheet = [
        AUDIT_SHEET[0],
        ["https://cdn.other.test/wp-content/uploads/2024/03/x-300x200.jpg", "An alt", "", "", ""],
    ]
    upload = _upload(api_client, _xlsx({"Images": sheet}))
    response = api_client.post(
        "/api/v1/tools/alt-text/reports",
        json={
            "upload_id": upload.json()["upload_id"],
            "title": "Client bound",
            "sheet": "Images",
            "client_id": client_id,
            "column_map": {"image_url": 0, "new_alt": 1},
        },
    )
    assert response.status_code == 201
    report = response.json()
    assert report["client_id"] == client_id
    assert report["client_name"] == "Acme"

    row = api_client.get(f"/api/v1/tools/alt-text/reports/{report['id']}/items").json()["rows"][0]
    assert "external_host" in row["derivation_warnings"]


def test_create_report_rejects_an_unknown_client(api_client: Any) -> None:
    upload = _upload(api_client, _xlsx({"Images": AUDIT_SHEET}))
    response = api_client.post(
        "/api/v1/tools/alt-text/reports",
        json={
            "upload_id": upload.json()["upload_id"],
            "title": "Nope",
            "sheet": "Images",
            "client_id": "00000000-0000-0000-0000-000000000000",
            "column_map": {"image_url": 0, "new_alt": 1},
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "client_not_found"


def test_create_report_requires_the_mandatory_columns(api_client: Any) -> None:
    upload = _upload(api_client, _xlsx({"Images": AUDIT_SHEET}))
    response = api_client.post(
        "/api/v1/tools/alt-text/reports",
        json={
            "upload_id": upload.json()["upload_id"],
            "title": "Missing map",
            "sheet": "Images",
            "column_map": {"image_url": 0},
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "invalid_column_map"


def test_create_report_rejects_an_expired_upload(api_client: Any) -> None:
    response = api_client.post(
        "/api/v1/tools/alt-text/reports",
        json={
            "upload_id": "11111111-1111-1111-1111-111111111111",
            "title": "Gone",
            "column_map": {"image_url": 0, "new_alt": 1},
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "upload_not_found"


def test_reimporting_the_same_file_and_sheet_points_at_the_existing_report(
    api_client: Any,
) -> None:
    # Ті самі **байти** для обох завантажень: `openpyxl` пише в архів час
    # створення, тож два виклики `_xlsx()` по різні боки від межі секунди дають
    # різний sha256 — і перевірка ідемпотентності проходила через раз.
    source = _xlsx({"Images": AUDIT_SHEET})
    first = _create_report(api_client, _source=source)
    upload = _upload(api_client, source)
    payload = {
        "upload_id": upload.json()["upload_id"],
        "title": "Duplicate",
        "sheet": "Images",
        "column_map": {"image_url": 0, "new_alt": 1},
    }

    conflict = api_client.post("/api/v1/tools/alt-text/reports", json=payload)
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["error_code"] == "report_exists"
    assert detail["details"]["existing_report_id"] == first["id"]

    # Свідоме повторне створення лишається можливим.
    upload = _upload(api_client, source)
    payload["upload_id"] = upload.json()["upload_id"]
    forced = api_client.post("/api/v1/tools/alt-text/reports?force=true", json=payload)
    assert forced.status_code == 201
    assert forced.json()["id"] != first["id"]


def test_report_counts_rows_the_source_could_not_use(api_client: Any) -> None:
    sheet = [AUDIT_SHEET[0], AUDIT_SHEET[1], ["", "orphan alt", "", "", ""]]
    upload = _upload(api_client, _xlsx({"Images": sheet}))
    report = api_client.post(
        "/api/v1/tools/alt-text/reports",
        json={
            "upload_id": upload.json()["upload_id"],
            "title": "Partial source",
            "sheet": "Images",
            "column_map": {"image_url": 0, "new_alt": 1},
        },
    ).json()
    assert report["row_count"] == 1
    assert report["skipped_count"] == 1


# --- читання й фільтри ---------------------------------------------------------


def test_list_reports(api_client: Any) -> None:
    _create_report(api_client)
    reports = api_client.get("/api/v1/tools/alt-text/reports").json()
    assert len(reports) == 1
    assert reports[0]["title"] == "March image audit"
    assert reports[0]["source_sheet"] == "Images"


def test_get_missing_report_is_404(api_client: Any) -> None:
    response = api_client.get("/api/v1/tools/alt-text/reports/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "report_not_found"


def test_items_are_filterable_and_paginated(api_client: Any) -> None:
    report = _create_report(api_client)
    base = f"/api/v1/tools/alt-text/reports/{report['id']}/items"

    page = api_client.get(f"{base}?limit=2&offset=1").json()
    assert page["total"] == 3 and page["limit"] == 2 and page["offset"] == 1
    assert [row["row_index"] for row in page["rows"]] == [3, 4]

    searched = api_client.get(f"{base}?q=logo").json()
    assert [row["search_key"] for row in searched["rows"]] == ["logo"]

    theme = api_client.get(f"{base}?url_kind=theme_asset").json()
    assert theme["total"] == 1

    with_page = api_client.get(f"{base}?has_page=true").json()
    assert with_page["total"] == 2


def test_items_of_a_missing_report_is_404(api_client: Any) -> None:
    response = api_client.get(
        "/api/v1/tools/alt-text/reports/00000000-0000-0000-0000-000000000000/items"
    )
    assert response.status_code == 404


# --- прогрес -------------------------------------------------------------------


def test_copying_the_alt_marks_the_row_and_survives_a_reload(api_client: Any) -> None:
    report = _create_report(api_client)
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    item = api_client.get(f"{base}/items").json()["rows"][0]

    patched = api_client.patch(f"{base}/items/{item['id']}", json={"status": "copied"})
    assert patched.status_code == 200
    assert patched.json()["status"] == "copied"
    assert patched.json()["status_changed_at"] is not None

    # Читаємо заново — статус у БД, не в памʼяті сторінки.
    reread = api_client.get(f"{base}/items").json()["rows"][0]
    assert reread["status"] == "copied"
    assert api_client.get(base).json()["status_counts"]["copied"] == 1


def test_status_can_be_reset(api_client: Any) -> None:
    report = _create_report(api_client)
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    item = api_client.get(f"{base}/items").json()["rows"][0]
    api_client.patch(f"{base}/items/{item['id']}", json={"status": "copied"})
    reset = api_client.patch(f"{base}/items/{item['id']}", json={"status": "pending"})
    assert reset.json()["status"] == "pending"
    assert reset.json()["status_changed_at"] is None


def test_status_patch_rejects_an_unknown_value(api_client: Any) -> None:
    report = _create_report(api_client)
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    item = api_client.get(f"{base}/items").json()["rows"][0]
    response = api_client.patch(f"{base}/items/{item['id']}", json={"status": "done"})
    assert response.status_code == 422


def test_status_patch_refuses_a_row_from_another_report(api_client: Any) -> None:
    report = _create_report(api_client)
    item = api_client.get(
        f"/api/v1/tools/alt-text/reports/{report['id']}/items"
    ).json()["rows"][0]
    other = "00000000-0000-0000-0000-000000000000"
    response = api_client.patch(
        f"/api/v1/tools/alt-text/reports/{other}/items/{item['id']}", json={"status": "copied"}
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "item_not_found"


# --- перевірка -----------------------------------------------------------------


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, html: str, status: int = 200) -> list[str]:
    """Підмінити мережу; повертає список URL, які реально запросили."""
    from app.tools.alt_text import verify as verify_module

    requested: list[str] = []

    def fake_fetch(url: str) -> FetchedPage:
        requested.append(url)
        return FetchedPage(http_status=status, html=html, final_url=url)

    monkeypatch.setattr(verify_module, "fetch_page", fake_fetch)
    return requested


def test_verify_confirms_a_match_and_promotes_the_row(
    api_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _create_report(api_client)
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    item = api_client.get(f"{base}/items").json()["rows"][0]
    requested = _patch_fetch(
        monkeypatch,
        f'<img src="{UPLOADS}/hero-image-300x200.jpg" alt="Team at the Kyiv office">',
    )

    response = api_client.post(f"{base}/items/{item['id']}/verify")
    assert response.status_code == 200
    body = response.json()
    assert body["verify_status"] == "match"
    assert body["verify_alt"] == "Team at the Kyiv office"
    assert body["verify_http_status"] == 200
    assert body["verify_checked_at"] is not None
    # Підтверджений збіг — єдина підстава підняти прогрес автоматично.
    assert body["status"] == "applied"

    # Запит пішов із cache-buster-ом (шар кешу в клієнтів дуже великий).
    assert len(requested) == 1
    assert "yvcheck=" in requested[0]


def test_verify_reports_a_mismatch_without_touching_progress(
    api_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _create_report(api_client)
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    item = api_client.get(f"{base}/items").json()["rows"][0]
    _patch_fetch(monkeypatch, f'<img src="{UPLOADS}/hero-image.jpg" alt="IMG_0042">')

    body = api_client.post(f"{base}/items/{item['id']}/verify").json()
    assert body["verify_status"] == "mismatch"
    assert body["verify_alt"] == "IMG_0042"
    assert body["status"] == "pending"


def test_verify_distinguishes_a_missing_image_from_a_failed_fetch(
    api_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _create_report(api_client)
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    item = api_client.get(f"{base}/items").json()["rows"][0]

    _patch_fetch(monkeypatch, '<img src="/other.jpg" alt="Other">')
    assert (
        api_client.post(f"{base}/items/{item['id']}/verify").json()["verify_status"]
        == "image_not_found_on_page"
    )

    from app.tools.alt_text import verify as verify_module

    def boom(url: str) -> FetchedPage:
        raise TimeoutError

    monkeypatch.setattr(verify_module, "fetch_page", boom)
    failed = api_client.post(f"{base}/items/{item['id']}/verify").json()
    assert failed["verify_status"] == "fetch_failed"
    # Провал перевірки не означає «alt не оновлений» — попередній вердикт стерто,
    # але статус прогресу лишився недоторканим.
    assert failed["status"] == "pending"


def test_verify_needs_a_page_url(api_client: Any) -> None:
    report = _create_report(api_client)
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    logo = api_client.get(f"{base}/items").json()["rows"][1]
    response = api_client.post(f"{base}/items/{logo['id']}/verify")
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "no_page_url"


def test_verify_needs_an_expected_alt(api_client: Any) -> None:
    sheet = [AUDIT_SHEET[0], [f"{UPLOADS}/x.jpg", "", "https://acme.example.com/p", "", ""]]
    upload = _upload(api_client, _xlsx({"Images": sheet}))
    report = api_client.post(
        "/api/v1/tools/alt-text/reports",
        json={
            "upload_id": upload.json()["upload_id"],
            "title": "No alt",
            "sheet": "Images",
            "column_map": {"image_url": 0, "new_alt": 1, "page_url": 2},
        },
    ).json()
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    item = api_client.get(f"{base}/items").json()["rows"][0]
    response = api_client.post(f"{base}/items/{item['id']}/verify")
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "no_expected_alt"


def test_report_verify_counts_are_reported(
    api_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _create_report(api_client)
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    item = api_client.get(f"{base}/items").json()["rows"][0]
    _patch_fetch(monkeypatch, f'<img src="{UPLOADS}/hero-image.jpg" alt="">')
    api_client.post(f"{base}/items/{item['id']}/verify")
    assert api_client.get(base).json()["verify_counts"] == {"empty_alt": 1}


# --- WordPress media ID (task-39) -----------------------------------------------


def _rest_response(payload: Any, *, status: int = 200) -> RestResponse:
    body = json.dumps(payload).encode()
    return RestResponse(http_status=status, content_type="application/json", body=body)


def _patch_media(monkeypatch: pytest.MonkeyPatch, responses: list[RestResponse]) -> list[str]:
    """Підмінити `wp_rest.fetch_rest`; повертає список реально запитаних URL."""
    from app.tools.alt_text import wp_rest as wp_rest_module

    requested: list[str] = []
    queue = list(responses)

    def fake_fetch(url: str) -> RestResponse:
        requested.append(url)
        return queue.pop(0)

    monkeypatch.setattr(wp_rest_module, "fetch_rest", fake_fetch)
    return requested


def test_resolve_item_media_finds_an_exact_match(
    api_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _create_report(api_client)
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    item = api_client.get(f"{base}/items").json()["rows"][0]  # hero-image, uploads
    requested = _patch_media(
        monkeypatch,
        [
            _rest_response(
                [
                    {
                        "id": 42,
                        "slug": "hero-image",
                        "alt_text": "Team at the office",
                        "source_url": f"{UPLOADS}/hero-image.jpg",
                        "media_details": {"file": "2024/03/hero-image.jpg", "sizes": {}},
                    }
                ]
            )
        ],
    )

    response = api_client.post(f"{base}/items/{item['id']}/resolve-media")
    assert response.status_code == 200
    body = response.json()
    assert body["wp_media_id"] == 42
    assert body["wp_media_status"] == "resolved"
    assert body["wp_media_confidence"] == "exact"
    assert body["wp_media_alt"] == "Team at the office"
    assert body["wp_media_admin_url"] == "https://acme.example.com/wp-admin/upload.php?item=42"
    assert body["wp_media_checked_at"] is not None
    assert len(requested) == 1


def test_resolve_item_media_is_not_searchable_for_a_non_uploads_row(api_client: Any) -> None:
    report = _create_report(api_client)
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    items = api_client.get(f"{base}/items").json()["rows"]
    theme_item = next(row for row in items if row["url_kind"] == "theme_asset")

    response = api_client.post(f"{base}/items/{theme_item['id']}/resolve-media")
    assert response.status_code == 200
    body = response.json()
    assert body["wp_media_status"] == "not_searchable"
    assert body["wp_media_id"] is None


def test_resolve_item_media_is_ambiguous_with_multiple_unconfirmed_candidates(
    api_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _create_report(api_client)
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    item = api_client.get(f"{base}/items").json()["rows"][0]
    candidates = [
        {
            "id": 101,
            "slug": "hero-image",
            "alt_text": None,
            "source_url": f"{UPLOADS}/other/hero-image.jpg",
            "media_details": {"file": "2023/01/hero-image.jpg", "sizes": {}},
        },
        {
            "id": 102,
            "slug": "hero-image-2",
            "alt_text": None,
            "source_url": f"{UPLOADS}/other/hero-image-2.jpg",
            "media_details": {"file": "2022/11/hero-image.jpg", "sizes": {}},
        },
    ]
    _patch_media(monkeypatch, [_rest_response(candidates)])

    response = api_client.post(f"{base}/items/{item['id']}/resolve-media")
    assert response.status_code == 200
    body = response.json()
    assert body["wp_media_status"] == "ambiguous"
    assert body["wp_media_id"] is None
    assert {candidate["id"] for candidate in body["wp_media_candidates"]} == {101, 102}


def test_resolve_item_media_falls_back_to_the_client_domain_when_the_image_host_has_no_rest_api(
    api_client: Any, seed_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (live QA 2026-08-14): a custom CDN subdomain (`cdn.client.com`) that

    isn't one of the recognized proxy patterns (Photon/Cloudflare/Statically) serves
    the image but has no WordPress REST API of its own — the real site lives at the
    client's registered domain. The first attempt must fail honestly (`rest_unavailable`)
    before the fallback, not silently swap hosts.
    """
    client_id = seed_client(domain="realsite.example.com")
    sheet = [
        AUDIT_SHEET[0],
        [
            "https://cdn.realsite.example.com/wp-content/uploads/2024/03/hero-1.jpg",
            "An alt",
            "",
            "",
            "",
        ],
    ]
    upload = _upload(api_client, _xlsx({"Images": sheet}))
    report = api_client.post(
        "/api/v1/tools/alt-text/reports",
        json={
            "upload_id": upload.json()["upload_id"],
            "title": "CDN host",
            "sheet": "Images",
            "client_id": client_id,
            "column_map": {"image_url": 0, "new_alt": 1},
        },
    ).json()
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    item = api_client.get(f"{base}/items").json()["rows"][0]

    from app.tools.alt_text import wp_rest as wp_rest_module

    requested: list[str] = []

    def fake_fetch(url: str) -> RestResponse:
        requested.append(url)
        if "cdn.realsite.example.com" in url:
            return RestResponse(http_status=403, content_type="application/json", body=b"{}")
        return _rest_response(
            [
                {
                    "id": 55,
                    "slug": "hero-1",
                    "alt_text": None,
                    "source_url": "https://realsite.example.com/wp-content/uploads/2024/03/hero-1.jpg",
                    "media_details": {"file": "2024/03/hero-1.jpg", "sizes": {}},
                }
            ]
        )

    monkeypatch.setattr(wp_rest_module, "fetch_rest", fake_fetch)

    response = api_client.post(f"{base}/items/{item['id']}/resolve-media")
    assert response.status_code == 200
    body = response.json()
    assert body["wp_media_status"] == "resolved"
    assert body["wp_media_id"] == 55
    assert body["wp_media_admin_url"] == "https://realsite.example.com/wp-admin/upload.php?item=55"
    # The image host was tried first (and honestly failed) before the fallback.
    assert any("cdn.realsite.example.com" in url for url in requested)
    assert any("realsite.example.com" in url and "cdn." not in url for url in requested)


def test_resolve_item_media_fallback_uses_the_monitor_canonical_www_setting(
    api_client: Any, seed_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (live QA 2026-08-14): the client-domain fallback used

    `client.domain` verbatim, so a client stored as bare `realsite.example.com`
    whose site actually canonicalizes to `www.` got an admin link that bounced
    through a cross-host login redirect. `MonitorSettings.canonical_www`
    (Site Monitor, task-38 follow-up) already records the answer per client —
    the fallback must apply the same www/non-www correction as
    `app.monitor.runner._base_url`, not just echo the stored domain.
    """
    client_id = seed_client(domain="realsite.example.com")
    settings_response = api_client.patch(
        f"/api/v1/clients/{client_id}/monitor/settings", json={"canonical_www": True}
    )
    assert settings_response.status_code == 200

    sheet = [
        AUDIT_SHEET[0],
        [
            "https://cdn.realsite.example.com/wp-content/uploads/2024/03/hero-1.jpg",
            "An alt",
            "",
            "",
            "",
        ],
    ]
    upload = _upload(api_client, _xlsx({"Images": sheet}))
    report = api_client.post(
        "/api/v1/tools/alt-text/reports",
        json={
            "upload_id": upload.json()["upload_id"],
            "title": "CDN host with canonical www",
            "sheet": "Images",
            "client_id": client_id,
            "column_map": {"image_url": 0, "new_alt": 1},
        },
    ).json()
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    item = api_client.get(f"{base}/items").json()["rows"][0]

    from app.tools.alt_text import wp_rest as wp_rest_module

    requested: list[str] = []

    def fake_fetch(url: str) -> RestResponse:
        requested.append(url)
        if "cdn.realsite.example.com" in url:
            return RestResponse(http_status=403, content_type="application/json", body=b"{}")
        return _rest_response(
            [
                {
                    "id": 77,
                    "slug": "hero-1",
                    "alt_text": None,
                    "source_url": "https://www.realsite.example.com/wp-content/uploads/2024/03/hero-1.jpg",
                    "media_details": {"file": "2024/03/hero-1.jpg", "sizes": {}},
                }
            ]
        )

    monkeypatch.setattr(wp_rest_module, "fetch_rest", fake_fetch)

    response = api_client.post(f"{base}/items/{item['id']}/resolve-media")
    assert response.status_code == 200
    body = response.json()
    assert body["wp_media_status"] == "resolved"
    assert body["wp_media_admin_url"] == "https://www.realsite.example.com/wp-admin/upload.php?item=77"
    assert any(url.startswith("https://www.realsite.example.com/") for url in requested)


def test_resolve_item_media_of_a_missing_item_is_404(api_client: Any) -> None:
    report = _create_report(api_client)
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    response = api_client.post(
        f"{base}/items/00000000-0000-0000-0000-000000000000/resolve-media"
    )
    assert response.status_code == 404


def test_resolve_report_media_processes_unresolved_uploads_rows_only(
    api_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _create_report(api_client)
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    items = api_client.get(f"{base}/items").json()["rows"]
    uploads_rows = [row for row in items if row["url_kind"] == "uploads"]
    assert len(uploads_rows) == 2  # hero-image (row 1) + logo (row 2); pattern.svg is a theme asset

    requested = _patch_media(
        monkeypatch,
        [
            _rest_response(
                [
                    {
                        "id": 1,
                        "slug": "hero-image",
                        "alt_text": None,
                        "source_url": f"{UPLOADS}/hero-image.jpg",
                        "media_details": {"file": "2024/03/hero-image.jpg", "sizes": {}},
                    }
                ]
            ),
            _rest_response([]),  # logo row, step 1: exact path — empty
            _rest_response([]),  # logo row, step 2: slug — empty
            _rest_response([]),  # logo row, step 3: broad search — empty
        ],
    )

    response = api_client.post(f"{base}/resolve-media", json={"limit": 50})
    assert response.status_code == 200
    body = response.json()
    assert body["resolved"] == 1
    assert body["not_found"] == 1
    assert body["remaining"] == 0
    assert len(requested) == 4

    updated = api_client.get(f"{base}/items").json()["rows"]
    theme_row = next(row for row in updated if row["url_kind"] == "theme_asset")
    # Не-uploads рядок пакет узагалі не чіпав: `wp_media_status` лишається `None`.
    assert theme_row["wp_media_status"] is None


def test_resolve_report_media_of_a_missing_report_is_404(api_client: Any) -> None:
    missing = "/api/v1/tools/alt-text/reports/00000000-0000-0000-0000-000000000000/resolve-media"
    assert api_client.post(missing).status_code == 404


def test_resolve_report_media_is_idempotent_and_stops_reprocessing(
    api_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _create_report(api_client)
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    _patch_media(monkeypatch, [_rest_response([]), _rest_response([]), _rest_response([])] * 2)

    first = api_client.post(f"{base}/resolve-media").json()
    assert first["not_found"] == 2

    # Другий прогін не бачить уже вирішених рядків узагалі — жодного запиту.
    second_requested = _patch_media(monkeypatch, [])
    second = api_client.post(f"{base}/resolve-media").json()
    assert second == {
        "resolved": 0,
        "ambiguous": 0,
        "not_found": 0,
        "rest_unavailable": 0,
        "failed": 0,
        "skipped": 0,
        "remaining": 0,
    }
    assert second_requested == []


def test_items_filter_by_media_status(api_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    report = _create_report(api_client)
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    item = api_client.get(f"{base}/items").json()["rows"][0]
    _patch_media(
        monkeypatch,
        [
            _rest_response(
                [
                    {
                        "id": 42,
                        "slug": "hero-image",
                        "alt_text": None,
                        "source_url": f"{UPLOADS}/hero-image.jpg",
                        "media_details": {"file": "2024/03/hero-image.jpg", "sizes": {}},
                    }
                ]
            )
        ],
    )
    api_client.post(f"{base}/items/{item['id']}/resolve-media")

    resolved = api_client.get(f"{base}/items", params={"media_status": "resolved"}).json()
    assert [row["id"] for row in resolved["rows"]] == [item["id"]]

    unresolved = api_client.get(f"{base}/items", params={"media_status": "unresolved"}).json()
    assert item["id"] not in [row["id"] for row in unresolved["rows"]]
    # Дві решта — logo (uploads) і pattern.svg (theme_asset), обидва ще не шукали.
    assert unresolved["total"] == 2


def test_media_id_appears_in_csv_export(api_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    report = _create_report(api_client)
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    item = api_client.get(f"{base}/items").json()["rows"][0]
    _patch_media(
        monkeypatch,
        [
            _rest_response(
                [
                    {
                        "id": 42,
                        "slug": "hero-image",
                        "alt_text": None,
                        "source_url": f"{UPLOADS}/hero-image.jpg",
                        "media_details": {"file": "2024/03/hero-image.jpg", "sizes": {}},
                    }
                ]
            )
        ],
    )
    api_client.post(f"{base}/items/{item['id']}/resolve-media")

    body = api_client.get(f"{base}/export.csv").text
    assert "Media ID" in body
    assert "42" in body
    assert "resolved" in body


# --- експорт і видалення -------------------------------------------------------


def test_csv_export_carries_derivation_progress_and_verdicts(api_client: Any) -> None:
    report = _create_report(api_client)
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    item = api_client.get(f"{base}/items").json()["rows"][0]
    api_client.patch(f"{base}/items/{item['id']}", json={"status": "copied"})

    response = api_client.get(f"{base}/export.csv")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    body = response.text
    assert "Search key" in body
    assert "hero-image" in body
    assert "copied" in body


def test_csv_export_neutralizes_spreadsheet_formulas(api_client: Any) -> None:
    # Джерело — CSV: там `=…` приходить звичайним текстом (у XLSX така клітинка
    # вже є формулою, і `data_only` не дає нам її тексту).
    raw = f'Image URL,New alt\n{UPLOADS}/hero.jpg,"=HYPERLINK(""http://evil.test"")"\n'
    upload = _upload(api_client, raw.encode(), "audit.csv")
    report = api_client.post(
        "/api/v1/tools/alt-text/reports",
        json={
            "upload_id": upload.json()["upload_id"],
            "title": "Formula",
            "column_map": {"image_url": 0, "new_alt": 1},
        },
    ).json()
    body = api_client.get(f"/api/v1/tools/alt-text/reports/{report['id']}/export.csv").text
    assert "'=HYPERLINK" in body
    assert ",=HYPERLINK" not in body


def test_export_survives_a_non_ascii_report_title(api_client: Any) -> None:
    """Регресія: тире/кирилиця в назві валили відповідь 500.

    HTTP-заголовки кодуються latin-1, тож `Content-Disposition` із «—» кидав
    `UnicodeEncodeError` уже після формування тіла. Знайдено живим прогоном,
    юніт-тести з ASCII-назвами цього не бачили.
    """
    report = _create_report(api_client, title="Огляд — липень 2026")
    for suffix in ("csv", "xlsx"):
        response = api_client.get(f"/api/v1/tools/alt-text/reports/{report['id']}/export.{suffix}")
        assert response.status_code == 200, response.text
        disposition = response.headers["content-disposition"]
        # ASCII-варіант для сумісності + RFC 5987 із повною назвою.
        assert "filename=" in disposition
        assert "filename*=UTF-8''" in disposition
        disposition.encode("latin-1")


def test_xlsx_export_is_a_real_workbook(api_client: Any) -> None:
    report = _create_report(api_client)
    response = api_client.get(f"/api/v1/tools/alt-text/reports/{report['id']}/export.xlsx")
    assert response.status_code == 200
    assert response.content.startswith(b"PK\x03\x04")


def test_export_of_a_missing_report_is_404(api_client: Any) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    base = f"/api/v1/tools/alt-text/reports/{missing}"
    assert api_client.get(f"{base}/export.csv").status_code == 404
    assert api_client.get(f"{base}/export.xlsx").status_code == 404


def test_delete_report_removes_it_and_its_rows(api_client: Any) -> None:
    report = _create_report(api_client)
    base = f"/api/v1/tools/alt-text/reports/{report['id']}"
    assert api_client.delete(base).status_code == 204
    assert api_client.get(base).status_code == 404
    assert api_client.get(f"{base}/items").status_code == 404


def test_delete_missing_report_is_404(api_client: Any) -> None:
    response = api_client.delete(
        "/api/v1/tools/alt-text/reports/00000000-0000-0000-0000-000000000000"
    )
    assert response.status_code == 404
