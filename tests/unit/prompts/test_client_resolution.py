"""Юніти визначення клієнта для шаблонів зі стратегії (task-102 §3.4)."""

from __future__ import annotations

from harness.db import TempDatabase

from app.db.repositories.clients import ClientRepository
from app.prompts.render import resolve_client
from app.schemas.enums import Provider


def _seed(session: object) -> tuple[str, str]:
    repo = ClientRepository(session)  # type: ignore[arg-type]
    grp = repo.create(
        name="Grill Point", slug="grp", domain="grillpointnyc.com", timezone="America/New_York"
    )
    other = repo.create(
        name="Other Co", slug="other", domain="other.com", timezone="America/New_York"
    )
    repo.upsert_integration(grp.id, Provider.tech_audit, config={"client_label": "GRP"})
    return grp.id, other.id


def test_domain_of_the_rows_wins_and_www_is_ignored(temp_db: TempDatabase) -> None:
    with temp_db.session_scope() as session:
        grp_id, _ = _seed(session)
        resolved = resolve_client(
            session,
            file_name="whatever",
            urls=[
                "https://www.grillpointnyc.com/menu/",
                "https://grillpointnyc.com/",
                "https://cdn.other-site.com/x",
            ],
            client_id=None,
        )

    assert resolved.resolved_by == "domain"
    assert resolved.client is not None and resolved.client.id == grp_id
    assert resolved.label == "GRP"  # кодове ім'я з tech_audit, а не Client.name


def test_filename_prefix_matches_the_client_label(temp_db: TempDatabase) -> None:
    with temp_db.session_scope() as session:
        grp_id, _ = _seed(session)
        resolved = resolve_client(
            session,
            file_name="grp_SEOStrategy_Q42026",
            urls=["https://unknown.example/"],
            client_id=None,
        )

    assert resolved.resolved_by == "filename_prefix"
    assert resolved.client is not None and resolved.client.id == grp_id


def test_manual_choice_beats_domain(temp_db: TempDatabase) -> None:
    with temp_db.session_scope() as session:
        _, other_id = _seed(session)
        resolved = resolve_client(
            session, file_name="GRP_x", urls=["https://grillpointnyc.com/"], client_id=other_id
        )

    assert resolved.resolved_by == "manual"
    assert resolved.client is not None and resolved.client.id == other_id


def test_nothing_matches_gives_none_not_a_guess(temp_db: TempDatabase) -> None:
    with temp_db.session_scope() as session:
        _seed(session)
        resolved = resolve_client(
            session,
            file_name="ZZZ_strategy",
            urls=["https://nobody.example/"],
            client_id="no-such-id",
        )

    assert resolved.resolved_by == "none"
    assert resolved.client is None and resolved.label is None
