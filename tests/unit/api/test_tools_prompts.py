"""API-тести Prompt Launcher (task-94): `app/api/tools_prompts.py`.

`aiseotools_source_path`/`aiseotools_host_path` перекриваються через
`app.dependency_overrides[get_settings_dep]` — а не через env var + reload,
бо `get_settings()` кешується `@lru_cache` і читається один раз при імпорті
`app.main` усередині фікстури `client` (`tests/unit/api/conftest.py`); до
того моменту тестовий `monkeypatch.setenv` у тілі тесту вже спізнюється.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.db.repositories.clients import ClientRepository
from app.schemas.enums import ClientStatus, Provider


def _override_settings(tmp_path: Path, *, doc_text: str | None = "TechAudit doc body") -> Any:
    from app.api.deps import get_settings_dep
    from app.core.config import get_settings
    from app.main import app as fastapi_app

    source_root = tmp_path / "aiseotools"
    (source_root / "docs").mkdir(parents=True, exist_ok=True)
    if doc_text is not None:
        (source_root / "docs" / "TechAudit.md").write_text(doc_text, encoding="utf-8")

    custom = get_settings().model_copy(
        update={
            "aiseotools_source_path": source_root,
            "aiseotools_host_path": "/home/operator/seo/AISeoTools",
        }
    )
    fastapi_app.dependency_overrides[get_settings_dep] = lambda: custom
    return get_settings_dep


@pytest.fixture(autouse=True)
def _clear_settings_override() -> Any:
    """`app.dependency_overrides` sits on the module-level FastAPI singleton —
    clean it up after every test regardless of which client fixture (`client`
    or `auth_client`) actually imported `app.main` for this test."""
    yield
    from app.api.deps import get_settings_dep
    from app.main import app as fastapi_app

    fastapi_app.dependency_overrides.pop(get_settings_dep, None)


def test_list_templates_reports_doc_available_and_host_path(client: Any, tmp_path: Path) -> None:
    _override_settings(tmp_path)

    body = client.get("/api/v1/tools/prompts").json()

    assert [t["key"] for t in body["templates"]] == [
        "techaudit", "ahrefs", "semrush", "gsc", "page_framework", "blog_framework",
    ]
    template = body["templates"][0]
    assert template["doc_available"] is True
    assert template["doc_relpath"] == "docs/TechAudit.md"
    assert template["doc_host_path"] == "/home/operator/seo/AISeoTools/docs/TechAudit.md"
    assert template["doc_bytes"] == len(b"TechAudit doc body")
    assert template["doc_updated_at"] is not None
    assert [f["key"] for f in template["fields"]] == ["CLIENT", "URL"]


def test_list_templates_reports_missing_doc_honestly(client: Any, tmp_path: Path) -> None:
    _override_settings(tmp_path, doc_text=None)

    body = client.get("/api/v1/tools/prompts").json()

    assert body["templates"][0]["doc_available"] is False
    assert body["templates"][0]["doc_updated_at"] is None
    assert body["templates"][0]["doc_bytes"] is None


def test_list_clients_renders_every_client_with_collectable_flag(
    client: Any, db: Any, tmp_path: Path
) -> None:
    _override_settings(tmp_path)
    repo = ClientRepository(db)
    with_domain = repo.create(name="Shoreline", slug="shb", domain="shorelinedentalstudio.com")
    repo.upsert_integration(with_domain.id, Provider.tech_audit, config={"client_label": "SHB"})
    repo.create(name="No Domain Client", slug="nodomain", domain="")
    paused = repo.create(name="Paused Client", slug="paused", domain="paused.com")
    repo.set_status(paused.id, ClientStatus.paused)
    disabled = repo.create(name="Disabled Client", slug="disabled", domain="disabled.com")
    repo.update(disabled.id, enabled=False)
    db.commit()

    body = client.get("/api/v1/tools/prompts/techaudit/clients").json()

    by_slug = {row["slug"]: row for row in body["rows"]}
    # Усі чотири присутні (`list_all`, не `list_collectable`) — фронт групує
    # за `collectable`, не за наявністю в списку.
    assert set(by_slug) == {"shb", "nodomain", "paused", "disabled"}
    assert by_slug["shb"]["collectable"] is True
    assert by_slug["nodomain"]["collectable"] is True
    assert by_slug["paused"]["collectable"] is False
    assert by_slug["disabled"]["collectable"] is False

    shb = by_slug["shb"]
    assert shb["values"] == {"CLIENT": "SHB", "URL": "https://shorelinedentalstudio.com"}
    assert shb["blocked_reason"] is None
    assert shb["rendered"] == (
        "Read /home/operator/seo/AISeoTools/docs/TechAudit.md and run it end to end.\n"
        "\n"
        "MODE:    tech\n"
        "CLIENT:  SHB\n"
        "URL:     https://shorelinedentalstudio.com\n"
    )

    nodomain = by_slug["nodomain"]
    assert nodomain["rendered"] is None
    assert nodomain["blocked_reason"] == "client has no domain to audit"
    assert nodomain["values"]["URL"] is None


def test_list_clients_unknown_template_404(client: Any) -> None:
    r = client.get("/api/v1/tools/prompts/does-not-exist/clients")
    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "template_not_found"


def test_source_returns_text_and_truncation_flag(client: Any, tmp_path: Path) -> None:
    _override_settings(tmp_path, doc_text="short body")

    r = client.get("/api/v1/tools/prompts/techaudit/source")

    assert r.status_code == 200
    assert r.json() == {"text": "short body", "truncated": False}


def test_source_404_when_file_not_mounted(client: Any, tmp_path: Path) -> None:
    _override_settings(tmp_path, doc_text=None)

    r = client.get("/api/v1/tools/prompts/techaudit/source")

    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "prompt_file_not_found"


def test_source_unknown_template_404(client: Any) -> None:
    r = client.get("/api/v1/tools/prompts/does-not-exist/source")
    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "template_not_found"


# ── ролі (task-71): тул read-only, усі три бачать однакове ──────────────────


@pytest.mark.auth
def test_all_three_roles_can_read_prompt_endpoints(
    auth_client: Any, auth_db: Any, make_user: Any, sign_in: Any, tmp_path: Path
) -> None:
    from app.api.deps import get_settings_dep
    from app.core.config import get_settings
    from app.main import app as fastapi_app

    source_root = tmp_path / "aiseotools"
    (source_root / "docs").mkdir(parents=True, exist_ok=True)
    (source_root / "docs" / "TechAudit.md").write_text("body", encoding="utf-8")
    custom = get_settings().model_copy(
        update={"aiseotools_source_path": source_root, "aiseotools_host_path": "/host"}
    )
    fastapi_app.dependency_overrides[get_settings_dep] = lambda: custom
    try:
        for role in ("admin", "member", "viewer"):
            email, password = make_user(f"qa-prompts-{role}@theyovth.com", role=role)
            headers = sign_in(email, password)
            assert auth_client.get("/api/v1/tools/prompts", headers=headers).status_code == 200
            assert (
                auth_client.get(
                    "/api/v1/tools/prompts/techaudit/clients", headers=headers
                ).status_code
                == 200
            )
    finally:
        fastapi_app.dependency_overrides.pop(get_settings_dep, None)


# ── шаблони зі стратегії (task-102) ──────────────────────────────────────────

import app.api.tools_prompts as prompts_module  # noqa: E402
from app.integrations.google_api.drive_client import (  # noqa: E402
    GOOGLE_SHEET_MIME,
    DriveClient,
    DriveFile,
)
from app.integrations.google_api.sheets_client import (  # noqa: E402
    CellLink,
    SheetCell,
    SheetGrid,
    SheetsClient,
)

STRATEGY_ID = "1UKjvBsyiNxgPDpseDJxPdNjwZ78HYOYtcaeIuT8PmdQ"


class _FakeDrive(DriveClient):
    def __init__(self, files: dict[str, DriveFile]) -> None:
        self._files = files

    def about_user(self) -> str | None:
        return "seo@theyovth.com"

    def get_file(self, file_id: str) -> DriveFile | None:
        return self._files.get(file_id)


class _FakeSheets(SheetsClient):
    def __init__(self, grid: tuple[SheetGrid, ...]) -> None:
        self._grid = grid

    def get_grid(self, spreadsheet_id: str, *, links_only: bool = False) -> tuple[SheetGrid, ...]:
        return self._grid


def _row(*values: str | None) -> tuple[SheetCell, ...]:
    return tuple(SheetCell(v, ()) for v in values)


def _grid() -> tuple[SheetGrid, ...]:
    chip = CellLink(form="chip", uri="https://docs.google.com/document/d/fw1")
    pages = SheetGrid(
        title="Q42026", index=1,
        rows=(
            _row(
                "Page Status", "Page Name", "URL", "Primary Key", "Title", "Framework / Copy Link"
            ),
            _row("OPTIMIZE PAGE", "Queens Menu", "https://grillpointnyc.com/menu/queens/",
                 "grill point queens menu", "Queens Menu Title", None),
            (SheetCell("OPTIMIZE PAGE", ()), SheetCell("Home", ()),
             SheetCell("https://grillpointnyc.com/", ()), SheetCell("grill point nyc", ()),
             SheetCell(None, ()), SheetCell("GRP_PAGE_FRAMEWORK_HOME", (chip,))),
        ),
    )
    blogs = SheetGrid(
        title="Blogs", index=2,
        rows=(
            _row("Article Topic", "Keyword", "Title", "Anchor Text", "Internal Link URL"),
            _row("Hummus Pairings", "what to eat with hummus", "Hummus T", "our menu",
                 "https://grillpointnyc.com/menu/"),
            _row(None, "hummus dip", None, None, None),
        ),
    )
    structure = SheetGrid(
        title="Structure", index=0,
        rows=(_row("Page Status", "Intent", "Page Name", "URL", "Comments"),
              _row("OPTIMIZE PAGE", "Commercial", "Home", "https://grillpointnyc.com/", "x")),
    )
    return (structure, pages, blogs)


def _install_google(monkeypatch: Any, *, files: dict[str, DriveFile] | None = None) -> None:
    file = DriveFile(
        id=STRATEGY_ID, name="GRP_SEOStrategy_Q42026", mime_type=GOOGLE_SHEET_MIME,
        web_view_link="https://docs.google.com/spreadsheets/d/x", modified_time=None, drive_id=None,
    )
    clients = prompts_module._Clients(
        drive=_FakeDrive(files if files is not None else {STRATEGY_ID: file}),
        sheets=_FakeSheets(_grid()),
    )
    monkeypatch.setattr(prompts_module, "_open_clients", lambda: clients)


def _seed_grp(db: Any) -> Any:
    repo = ClientRepository(db)
    grp = repo.create(name="Grill Point", slug="grp", domain="grillpointnyc.com")
    repo.upsert_integration(grp.id, Provider.tech_audit, config={"client_label": "GRP"})
    db.commit()
    return grp


def test_templates_expose_kind_and_existing_ones_stay_client_kind(
    client: Any, tmp_path: Path
) -> None:
    _override_settings(tmp_path)

    templates = {t["key"]: t for t in client.get("/api/v1/tools/prompts").json()["templates"]}

    assert {templates[k]["kind"] for k in ("techaudit", "ahrefs", "semrush", "gsc")} == {"client"}
    assert templates["page_framework"]["kind"] == "strategy"
    assert templates["page_framework"]["strategy_role"] == "page"
    assert templates["blog_framework"]["strategy_role"] == "blog"
    assert templates["page_framework"]["fields"] == []
    assert templates["blog_framework"]["doc_relpath"] == "docs/BlogFramework.md"


def test_strategy_link_gives_page_rows_with_resolved_client(
    client: Any, db: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    _override_settings(tmp_path)
    grp = _seed_grp(db)
    _install_google(monkeypatch)

    r = client.post(
        "/api/v1/tools/prompts/page_framework/strategy",
        json={"source": f"https://docs.google.com/spreadsheets/d/{STRATEGY_ID}/edit?usp=sharing"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["client"] == {
        "id": grp.id, "name": "Grill Point", "label": "GRP", "resolved_by": "domain",
    }
    assert body["sheet"] == "Q42026"
    assert {s["name"]: (s["role"], s["selectable"]) for s in body["sheets"]} == {
        "Structure": ("structure", False), "Q42026": ("page", True), "Blogs": ("blog", False),
    }
    queens, home = body["rows"]
    assert queens["mode"] == "existing" and queens["has_framework"] is False
    assert queens["row_key"] == "https://grillpointnyc.com/menu/queens/"
    assert queens["prefilled"] == ["Primary Key", "Title"]
    assert queens["blocked_reason"] is None
    assert queens["rendered"].startswith(
        "Read /home/operator/seo/AISeoTools/docs/PageFramework.md and run it end to end.\n\n"
        "MODE:    existing\nINPUT:   https://grillpointnyc.com/menu/queens/\nCLIENT:  GRP\n"
    )
    assert home["has_framework"] is True
    assert home["framework_link"] == "https://docs.google.com/document/d/fw1"
    assert body["prefillable"][0] == "Primary Key"


def test_blog_template_picks_the_blog_sheet_and_can_switch_sheets(
    client: Any, db: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    _override_settings(tmp_path)
    _seed_grp(db)
    _install_google(monkeypatch)

    body = client.post(
        "/api/v1/tools/prompts/blog_framework/strategy", json={"source": STRATEGY_ID}
    ).json()

    assert body["sheet"] == "Blogs"
    (article,) = body["rows"]
    assert article["title"] == "Hummus Pairings" and article["mode"] is None
    assert "Keywords: what to eat with hummus, hummus dip\n" in article["rendered"]
    assert "Internal links: https://grillpointnyc.com/menu/\n" in article["rendered"]

    bad = client.post(
        "/api/v1/tools/prompts/blog_framework/strategy",
        json={"source": STRATEGY_ID, "sheet": "Structure"},
    )
    assert (bad.status_code, bad.json()["detail"]["error_code"]) == (400, "sheet_not_selectable")
    unknown = client.post(
        "/api/v1/tools/prompts/blog_framework/strategy",
        json={"source": STRATEGY_ID, "sheet": "Nope"},
    )
    assert unknown.json()["detail"]["error_code"] == "unknown_sheet"


def test_unresolved_client_blocks_rows_until_one_is_picked(
    client: Any, db: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    _override_settings(tmp_path)
    other = ClientRepository(db).create(name="Other Co", slug="other", domain="other.com")
    db.commit()
    _install_google(monkeypatch)
    url = "/api/v1/tools/prompts/page_framework/strategy"

    body = client.post(url, json={"source": STRATEGY_ID}).json()
    assert body["client"] is None
    assert {r["rendered"] for r in body["rows"]} == {None}
    assert {r["blocked_reason"] for r in body["rows"]} == {"client is not resolved — pick one"}

    picked = client.post(url, json={"source": STRATEGY_ID, "client_id": other.id}).json()
    assert picked["client"]["resolved_by"] == "manual"
    assert all(r["rendered"] for r in picked["rows"])


def test_strategy_endpoint_error_codes(client: Any, tmp_path: Path, monkeypatch: Any) -> None:
    _override_settings(tmp_path)
    _install_google(monkeypatch, files={})
    url = "/api/v1/tools/prompts/page_framework/strategy"

    inaccessible = client.post(url, json={"source": STRATEGY_ID})
    assert (inaccessible.status_code, inaccessible.json()["detail"]["error_code"]) == (
        404, "file_not_accessible",
    )
    garbage = client.post(url, json={"source": "hello world"})
    assert garbage.json()["detail"]["error_code"] == "unrecognized_source"
    outside = client.post(url, json={"source": "/etc/passwd.xlsx"})
    assert outside.json()["detail"]["error_code"] == "path_outside_aiseotools"
    unknown = client.post("/api/v1/tools/prompts/nope/strategy", json={"source": STRATEGY_ID})
    assert unknown.status_code == 404


def test_template_kind_mismatch_is_a_400_both_ways(client: Any, tmp_path: Path) -> None:
    _override_settings(tmp_path)

    strategy_as_clients = client.get("/api/v1/tools/prompts/page_framework/clients")
    client_as_strategy = client.post(
        "/api/v1/tools/prompts/techaudit/strategy", json={"source": STRATEGY_ID}
    )

    assert strategy_as_clients.status_code == 400
    assert strategy_as_clients.json()["detail"]["error_code"] == "wrong_template_kind"
    assert client_as_strategy.status_code == 400
    assert client_as_strategy.json()["detail"]["error_code"] == "wrong_template_kind"


def test_local_strategies_listing(client: Any, tmp_path: Path) -> None:
    _override_settings(tmp_path)
    folder = tmp_path / "aiseotools" / "out" / "GRP_2026-08-26_blog_strategy"
    folder.mkdir(parents=True)
    (folder / "GRP_BLOG_STRATEGY_Q32026.xlsx").write_bytes(b"x")
    (folder / "GRP_STRATEGY_CHECK.xlsx").write_bytes(b"x")

    files = client.get("/api/v1/tools/prompts/strategy/local").json()["files"]

    assert [f["name"] for f in files] == ["GRP_BLOG_STRATEGY_Q32026.xlsx"]
    assert files[0]["path_host"].startswith("/home/operator/seo/AISeoTools/out/")


@pytest.mark.auth
def test_strategy_load_needs_member_but_viewer_still_reads_templates(
    auth_client: Any, auth_db: Any, make_user: Any, sign_in: Any, tmp_path: Path
) -> None:
    from app.api.deps import get_settings_dep
    from app.core.config import get_settings
    from app.main import app as fastapi_app

    source_root = tmp_path / "aiseotools"
    (source_root / "docs").mkdir(parents=True, exist_ok=True)
    (source_root / "docs" / "PageFramework.md").write_text("body", encoding="utf-8")
    custom = get_settings().model_copy(
        update={"aiseotools_source_path": source_root, "aiseotools_host_path": "/host"}
    )
    fastapi_app.dependency_overrides[get_settings_dep] = lambda: custom
    try:
        outcomes = {}
        for role in ("admin", "member", "viewer"):
            email, password = make_user(f"qa-strategy-{role}@theyovth.com", role=role)
            headers = sign_in(email, password)
            assert auth_client.get("/api/v1/tools/prompts", headers=headers).status_code == 200
            outcomes[role] = auth_client.post(
                "/api/v1/tools/prompts/page_framework/strategy",
                json={"source": "out/missing.xlsx"}, headers=headers,
            ).status_code
    finally:
        fastapi_app.dependency_overrides.pop(get_settings_dep, None)

    # 404 = дійшли до хендлера (файлу немає); 403 = зупинено роллю.
    assert outcomes == {"admin": 404, "member": 404, "viewer": 403}
