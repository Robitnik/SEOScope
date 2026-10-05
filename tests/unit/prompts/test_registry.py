"""Юніти рендера Prompt Launcher (task-94): `app/prompts/{registry,render}.py`.

Використовує спільний `temp_db` (`tests/harness/db.py`, task-13/75) — той
самий harness, що інші модульні тести без своєї власної фікстури БД: лише
щоб отримати реальну sync-сесію й записати `Client`/`ClientIntegration`
через репозиторій. REST-шар (`app/api/tools_prompts.py`) тут не чіпаємо —
для нього окремий `tests/unit/api/test_tools_prompts.py`.
"""

from __future__ import annotations

import pytest
from harness.db import TempDatabase

from app.db.repositories.clients import ClientRepository
from app.prompts.registry import TEMPLATES, get_template
from app.prompts.render import render_for_client
from app.schemas.enums import Provider


def test_get_template_known_and_unknown_key() -> None:
    assert get_template("techaudit") is TEMPLATES[0]
    assert get_template("ahrefs") is TEMPLATES[1]
    assert get_template("semrush") is TEMPLATES[2]
    assert get_template("gsc") is TEMPLATES[3]
    assert get_template("does-not-exist") is None


def test_render_gsc_audit_uses_target_url_field(temp_db: TempDatabase) -> None:
    template = get_template("gsc")
    assert template is not None

    with temp_db.session_scope() as session:
        c = ClientRepository(session).create(
            name="Acme Dental", slug="acme-gsc", domain="acme.com", timezone="America/New_York"
        )
        values, rendered, blocked_reason = render_for_client(
            template, c, session, doc_host_path="/home/elon/dev/seo/AISeoTools/docs/GSC_Audit.md"
        )

    assert blocked_reason is None
    assert values == {"CLIENT": "Acme Dental", "TARGET_URL": "https://acme.com"}
    assert rendered == (
        "Read /home/elon/dev/seo/AISeoTools/docs/GSC_Audit.md and run it end to end.\n"
        "\n"
        "MODE:      gsc\n"
        "SCOPE:     full\n"
        "CLIENT:    Acme Dental\n"
        "PROPERTY:  https://acme.com\n"
    )


def test_render_uses_client_name_when_no_integration_label(temp_db: TempDatabase) -> None:
    template = get_template("techaudit")
    assert template is not None

    with temp_db.session_scope() as session:
        c = ClientRepository(session).create(
            name="Acme Dental", slug="acme", domain="acme.com", timezone="America/New_York"
        )
        values, rendered, blocked_reason = render_for_client(
            template, c, session, doc_host_path="/home/elon/dev/seo/AISeoTools/docs/TechAudit.md"
        )

    assert blocked_reason is None
    assert values == {"CLIENT": "Acme Dental", "URL": "https://acme.com"}
    assert rendered == (
        "Read /home/elon/dev/seo/AISeoTools/docs/TechAudit.md and run it end to end.\n"
        "\n"
        "MODE:    tech\n"
        "CLIENT:  Acme Dental\n"
        "URL:     https://acme.com\n"
    )


def test_render_prefers_tech_audit_label_over_ahrefs_and_name(temp_db: TempDatabase) -> None:
    template = get_template("techaudit")
    assert template is not None

    with temp_db.session_scope() as session:
        repo = ClientRepository(session)
        c = repo.create(name="Acme Dental", slug="acme2", domain="acme2.com")
        repo.upsert_integration(
            c.id, Provider.ahrefs, config={"ahrefs_client_label": "ACME_AHREFS"}
        )
        repo.upsert_integration(c.id, Provider.tech_audit, config={"client_label": "ACME"})
        values, rendered, blocked_reason = render_for_client(
            template, c, session, doc_host_path="/host/docs/TechAudit.md"
        )

    assert blocked_reason is None
    assert values["CLIENT"] == "ACME"
    assert rendered is not None
    assert "CLIENT:  ACME\n" in rendered


def test_render_falls_back_to_ahrefs_label_when_tech_audit_missing(temp_db: TempDatabase) -> None:
    template = get_template("techaudit")
    assert template is not None

    with temp_db.session_scope() as session:
        repo = ClientRepository(session)
        c = repo.create(name="Acme Dental", slug="acme3", domain="acme3.com")
        repo.upsert_integration(
            c.id, Provider.ahrefs, config={"ahrefs_client_label": "ACME_AHREFS"}
        )
        values, _rendered, blocked_reason = render_for_client(
            template, c, session, doc_host_path="/host/docs/TechAudit.md"
        )

    assert blocked_reason is None
    assert values["CLIENT"] == "ACME_AHREFS"


def test_render_ahrefs_audit_uses_bare_domain_field(temp_db: TempDatabase) -> None:
    template = get_template("ahrefs")
    assert template is not None

    with temp_db.session_scope() as session:
        c = ClientRepository(session).create(
            name="Acme Dental", slug="acme-ahrefs", domain="acme.com", timezone="America/New_York"
        )
        values, rendered, blocked_reason = render_for_client(
            template, c, session, doc_host_path="/home/elon/dev/seo/AISeoTools/docs/AhrefsAudit.md"
        )

    assert blocked_reason is None
    assert values == {"CLIENT": "Acme Dental", "DOMAIN": "acme.com"}
    assert rendered == (
        "Read /home/elon/dev/seo/AISeoTools/docs/AhrefsAudit.md and run it end to end.\n"
        "\n"
        "MODE:      ahrefs\n"
        "CLIENT:    Acme Dental\n"
        "DOMAIN:    acme.com\n"
    )


def test_render_semrush_audit_uses_bare_domain_and_its_own_mode(temp_db: TempDatabase) -> None:
    """Semrush Site Audit проєкт шукається за доменом, як і Ahrefs; MODE — той, що в
    таблиці режимів AISeoTools (`semrushaudit`), інакше агент не знайде runbook."""
    template = get_template("semrush")
    assert template is not None
    assert template.doc_relpath == "docs/SemrushAudit.md"

    with temp_db.session_scope() as session:
        c = ClientRepository(session).create(
            name="Acme Dental", slug="acme-semrush", domain="acme.com", timezone="America/New_York"
        )
        values, rendered, blocked_reason = render_for_client(
            template, c, session, doc_host_path="/home/elon/dev/seo/AISeoTools/docs/SemrushAudit.md"
        )

    assert blocked_reason is None
    assert values == {"CLIENT": "Acme Dental", "DOMAIN": "acme.com"}
    assert rendered == (
        "Read /home/elon/dev/seo/AISeoTools/docs/SemrushAudit.md and run it end to end.\n"
        "\n"
        "MODE:      semrushaudit\n"
        "CLIENT:    Acme Dental\n"
        "DOMAIN:    acme.com\n"
    )


@pytest.mark.parametrize("domain", ["", "   "])
def test_render_blocks_when_client_has_no_domain(temp_db: TempDatabase, domain: str) -> None:
    """PLAN §12: відсутнє значення НІКОЛИ не підмінюється порожнім рядком —
    клієнт без домену не має давати промт із діркою в `URL:`."""
    template = get_template("techaudit")
    assert template is not None

    with temp_db.session_scope() as session:
        c = ClientRepository(session).create(
            name="No Domain Yet", slug=f"nodomain-{len(domain)}", domain=domain
        )
        values, rendered, blocked_reason = render_for_client(
            template, c, session, doc_host_path="/host/docs/TechAudit.md"
        )

    assert rendered is None
    assert blocked_reason == "client has no domain to audit"
    assert values["URL"] is None
    # CLIENT лишається резолвленим — блокує лише поле, якого бракує.
    assert values["CLIENT"] == "No Domain Yet"
