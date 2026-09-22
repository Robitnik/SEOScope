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

    assert [t["key"] for t in body["templates"]] == ["techaudit", "gsc"]
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
