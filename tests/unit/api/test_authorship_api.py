"""`GET /api/v1/authorship*` (task-93 plan §2 D3): admin-only batched author
lookup and facets.

`client`/`db` run with `AUTH_ENABLED=false` (see `tests/conftest.py`'s
`_auth_disabled_unless_asked`) — `require_role`/`ensure_role` both no-op
there, so these cover the endpoint's own shape/validation/headers without
needing real users. Role gating itself needs `auth_client`/`auth_db`
(`@pytest.mark.auth`), same convention as `tests/unit/api/
test_reports_api.py`'s task-93 section.
"""

from __future__ import annotations

import pytest

from app.db.repositories.clients import ClientRepository
from app.db.repositories.reports import ReportRepository
from app.db.repositories.users import UserRepository


def _create_client(db, *, slug: str = "acme") -> str:
    client = ClientRepository(db).create(
        name="Acme", slug=slug, domain=f"{slug}.example.com", timezone="UTC"
    )
    db.commit()
    return client.id


def _run(db, client_id: str, **overrides) -> str:
    run = ReportRepository(db).create_run(
        client_id, kind="monthly", source="api", params={}, period_count=1, **overrides
    )
    db.commit()
    return run.id


# --- GET /authorship (lookup) --------------------------------------------


def test_lookup_returns_the_recorded_author(client, db) -> None:
    client_id = _create_client(db)
    from app.authorship.model import Attribution

    run_id = _run(db, client_id, created_by=Attribution(kind="agent", user_id=None))

    r = client.get(f"/api/v1/authorship?resource_type=report_run&ids={run_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["resource_type"] == "report_run"
    assert body["items"] == [{"resource_id": run_id, "author": {"kind": "agent", "user": None}}]


def test_lookup_reports_unknown_for_a_row_with_no_recorded_author(client, db) -> None:
    client_id = _create_client(db)
    run_id = _run(db, client_id)

    body = client.get(f"/api/v1/authorship?resource_type=report_run&ids={run_id}").json()
    assert body["items"][0]["author"] == {"kind": "unknown", "user": None}


def test_lookup_omits_an_id_that_does_not_exist(client, db) -> None:
    client_id = _create_client(db)
    run_id = _run(db, client_id)

    body = client.get(
        f"/api/v1/authorship?resource_type=report_run&ids={run_id},does-not-exist"
    ).json()
    assert [item["resource_id"] for item in body["items"]] == [run_id]


def test_lookup_batches_across_multiple_ids_in_one_call(client, db) -> None:
    client_id = _create_client(db)
    run_a = _run(db, client_id)
    run_b = _run(db, client_id)

    body = client.get(f"/api/v1/authorship?resource_type=report_run&ids={run_a},{run_b}").json()
    assert {item["resource_id"] for item in body["items"]} == {run_a, run_b}


def test_lookup_422_for_an_unknown_resource_type(client, db) -> None:
    r = client.get("/api/v1/authorship?resource_type=client&ids=x")
    assert r.status_code == 422
    assert r.json()["detail"]["error_code"] == "invalid_resource_type"


def test_lookup_422_for_too_many_ids(client, db) -> None:
    ids = ",".join(f"id-{i}" for i in range(201))
    r = client.get(f"/api/v1/authorship?resource_type=report_run&ids={ids}")
    assert r.status_code == 422
    assert r.json()["detail"]["error_code"] == "too_many_ids"


def test_lookup_response_is_private_no_store(client, db) -> None:
    client_id = _create_client(db)
    run_id = _run(db, client_id)
    r = client.get(f"/api/v1/authorship?resource_type=report_run&ids={run_id}")
    assert r.headers["cache-control"] == "private, no-store"
    assert "Cookie" in r.headers["vary"]


# --- GET /authorship/facets ------------------------------------------------


def test_facets_groups_by_kind(client, db) -> None:
    client_id = _create_client(db)
    from app.authorship.model import Attribution

    _run(db, client_id, created_by=Attribution(kind="agent", user_id=None))
    _run(db, client_id, created_by=Attribution(kind="agent", user_id=None))
    _run(db, client_id)  # unknown

    body = client.get("/api/v1/authorship/facets?resource_type=report_run").json()
    by_kind = {f["kind"]: f["count"] for f in body["authors"]}
    assert by_kind["agent"] == 2
    assert by_kind["unknown"] == 1


def test_facets_422_for_an_unknown_resource_type(client, db) -> None:
    r = client.get("/api/v1/authorship/facets?resource_type=client")
    assert r.status_code == 422


# --- Role gating (task-93 plan §2 D3) --------------------------------------


@pytest.mark.auth
def test_member_gets_403_even_though_the_resource_exists(
    auth_client, auth_db, make_user, sign_in
) -> None:
    client_id = _create_client(auth_db)
    run_id = _run(auth_db, client_id)

    member_email, member_password = make_user("member93z@theyovth.com", role="member")
    member_headers = sign_in(member_email, member_password)

    r = auth_client.get(
        f"/api/v1/authorship?resource_type=report_run&ids={run_id}", headers=member_headers
    )
    assert r.status_code == 403


@pytest.mark.auth
def test_admin_sees_the_real_signed_in_authors_identity(
    auth_client, auth_db, make_user, sign_in
) -> None:
    client_id = _create_client(auth_db)
    owner_email, owner_password = make_user("owner93z@theyovth.com", role="member")
    owner = UserRepository(auth_db).get_by_email(owner_email)
    from app.authorship.model import Attribution

    run_id = _run(
        auth_db, client_id, created_by=Attribution(kind="user", user_id=owner.id)
    )

    admin_email, admin_password = make_user("admin93z@theyovth.com", role="admin")
    admin_headers = sign_in(admin_email, admin_password)

    body = auth_client.get(
        f"/api/v1/authorship?resource_type=report_run&ids={run_id}", headers=admin_headers
    ).json()
    author = body["items"][0]["author"]
    assert author["kind"] == "user"
    assert author["user"]["id"] == owner.id
    assert author["user"]["email"] == owner_email
    assert author["user"]["is_active"] is True
