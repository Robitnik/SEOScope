"""API-тести Strategy Package Check (task-90 §4.7): `_open_clients` фейкана
на кожному тесті — жоден тест цього файлу не торкається реального Drive/
Docs/Sheets API чи мережі."""

from __future__ import annotations

import io
from typing import Any

from openpyxl import load_workbook

import app.api.tools_strategy_package as router_module
from app.api.tools_strategy_package import _Clients
from app.integrations.google_api import GoogleApiConfigError
from app.integrations.google_api.docs_client import DocsClient, DocTab
from app.integrations.google_api.drive_client import (
    GOOGLE_DOC_MIME,
    GOOGLE_SHEET_MIME,
    DriveClient,
    DriveFile,
    DriveFilePage,
)
from app.integrations.google_api.sheets_client import CellLink, SheetCell, SheetGrid, SheetsClient

STRATEGY_ID = "strat1"
FRAMEWORK_ID = "fw1"


def _para(text: str) -> dict[str, object]:
    return {
        "paragraph": {"paragraphStyle": {}, "elements": [{"textRun": {"content": text}}]}
    }


def _table(*rows: tuple[list[str], list[str]]) -> dict[str, object]:
    table_rows = []
    for label_paras, value_paras in rows:
        table_rows.append({
            "tableCells": [
                {"content": [_para(t) for t in label_paras]},
                {"content": [_para(t) for t in value_paras]},
            ]
        })
    return {"table": {"tableRows": table_rows}}


class _FakeDriveClient(DriveClient):
    def __init__(
        self,
        files: dict[str, DriveFile],
        *,
        about: str | None = "seo@theyovth.com",
        search_results: list[DriveFile] | None = None,
    ) -> None:
        self._files = files
        self._about = about
        self._search_results = search_results or []

    def about_user(self) -> str | None:
        return self._about

    def get_file(self, file_id: str) -> DriveFile | None:
        return self._files.get(file_id)

    def list_files(
        self, *, query: str, page_size: int = 25, page_token: str | None = None
    ) -> DriveFilePage:
        return DriveFilePage(files=tuple(self._search_results), next_page_token=None)

    def download_media(self, file_id: str) -> bytes:
        raise NotImplementedError


class _FakeSheetsClient(SheetsClient):
    def __init__(self, grids: dict[str, tuple[SheetGrid, ...]]) -> None:
        self._grids = grids

    def get_grid(self, spreadsheet_id: str, *, links_only: bool = False) -> tuple[SheetGrid, ...]:
        return self._grids.get(spreadsheet_id, ())


class _FakeDocsClient(DocsClient):
    def __init__(self, by_id: dict[str, tuple[DocTab, ...] | None]) -> None:
        self._by_id = by_id

    def get_document(self, document_id: str) -> tuple[DocTab, ...] | None:
        return self._by_id.get(document_id)


def _strategy_file() -> DriveFile:
    return DriveFile(
        id=STRATEGY_ID, name="ACME_SEO_STRATEGY", mime_type=GOOGLE_SHEET_MIME,
        web_view_link=None, modified_time=None, drive_id=None,
    )


def _framework_file() -> DriveFile:
    return DriveFile(
        id=FRAMEWORK_ID, name="ACME_FRAMEWORK_HOME", mime_type=GOOGLE_DOC_MIME,
        web_view_link=None, modified_time=None, drive_id=None,
    )


def _strategy_grid() -> tuple[SheetGrid, ...]:
    chip = CellLink(
        form="chip", uri=f"https://docs.google.com/document/d/{FRAMEWORK_ID}",
        mime_type="application/vnd.google-apps.kix",
    )
    header = [
        SheetCell("Page Name", ()), SheetCell("URL", ()), SheetCell("Title", ()),
        SheetCell("Description", ()), SheetCell("H1", ()), SheetCell("Framework Link", ()),
    ]
    row = [
        SheetCell("Home", ()), SheetCell("https://example.com/", ()),
        SheetCell("Home Title", ()), SheetCell("Home description text.", ()),
        SheetCell("Home H1", ()), SheetCell("ACME_FRAMEWORK_HOME", (chip,)),
    ]
    return (SheetGrid(title="Strategy", index=0, rows=(tuple(header), tuple(row))),)


def _framework_tabs() -> tuple[DocTab, ...]:
    tab = DocTab(
        tab_id="t.0", title="Framework", path=("Framework",),
        body_content=(_table((["H1"], ["Home H1"])),),
    )
    return (tab,)


def _install_fake_clients(
    monkeypatch: Any,
    *,
    files: dict[str, DriveFile] | None = None,
    grids: dict[str, tuple[SheetGrid, ...]] | None = None,
    docs: dict[str, tuple[DocTab, ...] | None] | None = None,
    search_results: list[DriveFile] | None = None,
    about: str | None = "seo@theyovth.com",
) -> None:
    clients = _Clients(
        drive=_FakeDriveClient(files or {}, about=about, search_results=search_results),
        docs=_FakeDocsClient(docs or {}),
        sheets=_FakeSheetsClient(grids or {}),
    )
    monkeypatch.setattr(router_module, "_open_clients", lambda: clients)


# ── /resolve ──────────────────────────────────────────────────────────────────


def test_resolve_invalid_url_returns_400(client: Any) -> None:
    r = client.post("/api/v1/tools/strategy-package/resolve", json={"url_or_id": "not a url"})
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "invalid_drive_url"


def test_resolve_returns_file_and_kind(client: Any, monkeypatch: Any) -> None:
    _install_fake_clients(monkeypatch, files={STRATEGY_ID: _strategy_file()})
    url = f"https://docs.google.com/spreadsheets/d/{STRATEGY_ID}/edit"
    r = client.post("/api/v1/tools/strategy-package/resolve", json={"url_or_id": url})
    assert r.status_code == 200
    body = r.json()
    assert body["file"]["id"] == STRATEGY_ID
    assert body["kind"] == "spreadsheet"


def test_resolve_inaccessible_file_gives_404_with_share_hint(client: Any, monkeypatch: Any) -> None:
    _install_fake_clients(monkeypatch, files={}, about="seo@theyovth.com")
    url = f"https://docs.google.com/spreadsheets/d/{STRATEGY_ID}/edit"
    r = client.post("/api/v1/tools/strategy-package/resolve", json={"url_or_id": url})
    assert r.status_code == 404
    assert "seo@theyovth.com" in r.json()["detail"]["message"]


def test_resolve_when_drive_not_configured_gives_503(client: Any, monkeypatch: Any) -> None:
    # Patched one level down (not `_open_clients` itself) so the router's
    # own `GoogleApiConfigError -> 503` mapping inside `_open_clients` is
    # what actually runs, not a test-duplicated copy of it.
    def _raise(settings: Any = None) -> Any:
        raise GoogleApiConfigError("no refresh token")

    monkeypatch.setattr(router_module, "open_google_api_session", _raise)
    url = f"https://docs.google.com/spreadsheets/d/{STRATEGY_ID}/edit"
    r = client.post("/api/v1/tools/strategy-package/resolve", json={"url_or_id": url})
    assert r.status_code == 503
    assert r.json()["detail"]["error_code"] == "drive_not_configured"


# ── /drive/files ──────────────────────────────────────────────────────────────


def test_browse_drive_returns_files(client: Any, monkeypatch: Any) -> None:
    _install_fake_clients(monkeypatch, search_results=[_strategy_file()])
    r = client.get("/api/v1/tools/strategy-package/drive/files?q=ACME")
    assert r.status_code == 200
    assert [f["id"] for f in r.json()["files"]] == [STRATEGY_ID]


# ── /inspect ──────────────────────────────────────────────────────────────────


def test_inspect_returns_sheets_and_candidates(client: Any, monkeypatch: Any) -> None:
    _install_fake_clients(
        monkeypatch,
        files={STRATEGY_ID: _strategy_file()},
        grids={STRATEGY_ID: _strategy_grid()},
    )
    r = client.post("/api/v1/tools/strategy-package/inspect", json={"file_id": STRATEGY_ID})
    assert r.status_code == 200
    body = r.json()
    assert body["source_name"] == "ACME_SEO_STRATEGY"
    assert len(body["sheets"]) == 1
    assert body["sheets"][0]["load_bearing"] is True
    assert len(body["candidates"]) == 1
    candidate = body["candidates"][0]
    assert candidate["document_id"] == FRAMEWORK_ID
    assert candidate["origin"] == "strategy_link"
    assert candidate["strategy_ref"] == "Strategy!row 2"


def test_inspect_missing_file_gives_404(client: Any, monkeypatch: Any) -> None:
    _install_fake_clients(monkeypatch, files={})
    r = client.post("/api/v1/tools/strategy-package/inspect", json={"file_id": "ghost"})
    assert r.status_code == 404


# ── /check + /reports ───────────────────────────────────────────────────────


def _check_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "file_id": STRATEGY_ID,
        "frameworks": [{
            "document_id": FRAMEWORK_ID, "document_name": "ACME_FRAMEWORK_HOME",
            "origin": "strategy_link", "sheet": "Strategy", "row": 2,
            "strategy_ref": "Strategy!row 2",
        }],
        "check_site": False, "check_published": False,
    }
    body.update(overrides)
    return body


def test_check_persists_and_returns_package(client: Any, monkeypatch: Any) -> None:
    _install_fake_clients(
        monkeypatch,
        files={STRATEGY_ID: _strategy_file(), FRAMEWORK_ID: _framework_file()},
        grids={STRATEGY_ID: _strategy_grid()},
        docs={FRAMEWORK_ID: _framework_tabs()},
    )
    r = client.post("/api/v1/tools/strategy-package/check", json=_check_body())
    assert r.status_code == 200
    body = r.json()
    assert body["source_name"] == "ACME_SEO_STRATEGY"
    assert body["framework_count"] == 1
    assert len(body["frameworks"]) == 1
    outcome = body["frameworks"][0]
    assert outcome["state"] == "checked"
    assert outcome["source"]["document_id"] == FRAMEWORK_ID
    # Home title/H1 match between Frame and the strategy row -> zero drift.
    assert not any(f["check"] == "sync-title-drift" for f in outcome["findings"])
    assert "id" in body
    # task-91: comparison matrix rides along on the same /check response —
    # one matched row (the framework's strategy row + its Frame values).
    assert len(body["matrix"]) == 1
    row = body["matrix"][0]
    assert row["kind"] == "matched"
    assert row["strategy"]["title"]["state"] == "value"
    # The fixture's Framework table only recognizes an H1 field (no Title/
    # Description) — the matrix must say so honestly (absent, not a false
    # "value" or a silently-dropped column).
    assert row["framework"]["title"]["state"] == "absent"
    assert row["framework"]["h1"]["state"] == "value"
    assert row["verdicts"]["h1"]["state"] == "match"


def test_check_with_no_frameworks_still_has_a_strategy_only_matrix_row(
    client: Any, monkeypatch: Any
) -> None:
    _install_fake_clients(
        monkeypatch, files={STRATEGY_ID: _strategy_file()}, grids={STRATEGY_ID: _strategy_grid()},
    )
    r = client.post("/api/v1/tools/strategy-package/check", json=_check_body(frameworks=[]))
    assert r.status_code == 200
    body = r.json()
    assert len(body["matrix"]) == 1
    assert body["matrix"][0]["kind"] == "strategy_only"
    assert body["matrix"][0]["framework"] is None


def test_check_with_no_frameworks_still_checks_strategy(client: Any, monkeypatch: Any) -> None:
    _install_fake_clients(
        monkeypatch, files={STRATEGY_ID: _strategy_file()}, grids={STRATEGY_ID: _strategy_grid()},
    )
    r = client.post("/api/v1/tools/strategy-package/check", json=_check_body(frameworks=[]))
    assert r.status_code == 200
    body = r.json()
    assert body["framework_count"] == 0
    assert body["frameworks"] == []


def test_check_then_list_then_get(client: Any, monkeypatch: Any) -> None:
    _install_fake_clients(
        monkeypatch,
        files={STRATEGY_ID: _strategy_file(), FRAMEWORK_ID: _framework_file()},
        grids={STRATEGY_ID: _strategy_grid()},
        docs={FRAMEWORK_ID: _framework_tabs()},
    )
    checked = client.post("/api/v1/tools/strategy-package/check", json=_check_body())
    package_id = checked.json()["id"]

    listed = client.get("/api/v1/tools/strategy-package/reports")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["packages"][0]["id"] == package_id

    detail = client.get(f"/api/v1/tools/strategy-package/reports/{package_id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == package_id
    assert len(detail.json()["frameworks"]) == 1
    # The matrix survives the persist/retrieve round trip through JSON.
    assert len(detail.json()["matrix"]) == 1


def test_get_unknown_package_gives_404(client: Any) -> None:
    r = client.get("/api/v1/tools/strategy-package/reports/does-not-exist")
    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "package_not_found"


def test_check_unknown_sheet_gives_400(client: Any, monkeypatch: Any) -> None:
    _install_fake_clients(
        monkeypatch, files={STRATEGY_ID: _strategy_file()}, grids={STRATEGY_ID: _strategy_grid()},
    )
    r = client.post(
        "/api/v1/tools/strategy-package/check",
        json=_check_body(sheets=["Nonexistent Sheet"], frameworks=[]),
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "unknown_sheet"


def test_check_document_source_gives_400(client: Any, monkeypatch: Any) -> None:
    """The resolved file is a Google Doc, not a strategy spreadsheet."""
    doc_as_source = DriveFile(
        id="doc1", name="Some Doc", mime_type=GOOGLE_DOC_MIME,
        web_view_link=None, modified_time=None, drive_id=None,
    )
    _install_fake_clients(monkeypatch, files={"doc1": doc_as_source})
    r = client.post(
        "/api/v1/tools/strategy-package/check", json=_check_body(file_id="doc1", frameworks=[])
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "unsupported_source"


# ── /reports/{id}/matrix.xlsx (task-91 §3.5) ────────────────────────────────


def test_matrix_xlsx_download_reads_the_persisted_matrix(client: Any, monkeypatch: Any) -> None:
    _install_fake_clients(
        monkeypatch,
        files={STRATEGY_ID: _strategy_file(), FRAMEWORK_ID: _framework_file()},
        grids={STRATEGY_ID: _strategy_grid()},
        docs={FRAMEWORK_ID: _framework_tabs()},
    )
    checked = client.post("/api/v1/tools/strategy-package/check", json=_check_body())
    package_id = checked.json()["id"]

    r = client.get(f"/api/v1/tools/strategy-package/reports/{package_id}/matrix.xlsx")
    assert r.status_code == 200
    assert r.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "attachment" in r.headers["content-disposition"]
    assert r.content[:2] == b"PK"
    workbook = load_workbook(io.BytesIO(r.content))
    assert workbook.sheetnames == ["Comparison", "Mismatches"]


def test_matrix_xlsx_unknown_package_gives_404(client: Any) -> None:
    r = client.get("/api/v1/tools/strategy-package/reports/does-not-exist/matrix.xlsx")
    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "package_not_found"
