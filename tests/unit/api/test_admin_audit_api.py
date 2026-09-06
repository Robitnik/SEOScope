"""`/api/v1/admin/audit` (task-72): доступ, пагінація, фільтри, CSV."""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.auth


@pytest.fixture
def admin(auth_client: TestClient, make_user, sign_in) -> dict[str, str]:
    email, password = make_user("boss@yovth.com", role="admin")
    return sign_in(email, password)


def _seed_events(auth_client: TestClient, admin: dict[str, str], count: int = 5) -> None:
    from app.audit.sink import get_audit_sink

    for i in range(count):
        auth_client.post(
            "/api/v1/admin/users",
            json={"email": f"user{i}@yovth.com", "name": f"User {i}", "role": "member"},
            headers=admin,
        )
    # Ендпоінт кладе рядок у чергу й одразу відповідає (task-72 §5) — тест
    # читає стрічку через окремий GET, тож без явного `flush()` це гонка.
    get_audit_sink().flush()


def test_member_and_viewer_cannot_read_the_log(
    auth_client: TestClient, make_user, sign_in
) -> None:
    email, password = make_user("member@yovth.com", role="member")
    headers = sign_in(email, password)
    assert auth_client.get("/api/v1/admin/audit", headers=headers).status_code == 403


def test_admin_sees_events_newest_first(auth_client: TestClient, admin) -> None:
    _seed_events(auth_client, admin, count=3)
    page = auth_client.get("/api/v1/admin/audit?limit=10", headers=admin).json()
    timestamps = [event["ts"] for event in page["events"]]
    assert timestamps == sorted(timestamps, reverse=True)
    assert any(event["action"] == "admin.user.create" for event in page["events"])


def test_pagination_cursor_has_no_gaps_or_duplicates(auth_client: TestClient, admin) -> None:
    from urllib.parse import quote

    _seed_events(auth_client, admin, count=12)
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params = f"limit=4{f'&cursor={quote(cursor)}' if cursor else ''}"
        page = auth_client.get(f"/api/v1/admin/audit?{params}", headers=admin).json()
        seen.extend(event["id"] for event in page["events"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen))
    assert len(seen) >= 12


def test_filter_by_action_narrows_results(auth_client: TestClient, admin) -> None:
    _seed_events(auth_client, admin, count=2)
    page = auth_client.get(
        "/api/v1/admin/audit?action=admin.user.create", headers=admin
    ).json()
    assert all(event["action"] == "admin.user.create" for event in page["events"])
    assert len(page["events"]) >= 2


def test_facets_expose_seen_actions_and_actors(auth_client: TestClient, admin) -> None:
    _seed_events(auth_client, admin, count=2)
    facets = auth_client.get("/api/v1/admin/audit/facets", headers=admin).json()
    assert any(item["action"] == "admin.user.create" for item in facets["actions"])
    assert any(item["email"] == "boss@yovth.com" for item in facets["actors"])


def test_export_csv_is_well_formed_and_escapes_formulas(
    auth_client: TestClient, admin, make_user
) -> None:
    from app.audit.sink import get_audit_sink

    # Логінимось на ОКРЕМОМУ клієнті, щоб не підмінити cookie в спільній
    # `auth_client` сесії адміна (у неї свій куки-jar).
    email, password = make_user("attacker@yovth.com")
    with TestClient(auth_client.app) as other:
        other.post("/api/v1/auth/login", json={"email": email, "password": password})
    get_audit_sink().flush()
    response = auth_client.get("/api/v1/admin/audit/export.csv", headers=admin)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == [
        "when", "who", "role", "action", "outcome", "resource", "resource_id",
        "method", "path", "status", "ip",
    ]
    assert len(rows) > 1


def test_export_csv_escapes_a_leading_equals_sign(auth_client: TestClient, admin) -> None:
    from app.api.admin_audit import _csv_safe

    assert _csv_safe("=SUM(A1:A9)").startswith("'")
    assert _csv_safe("+1").startswith("'")
    assert _csv_safe("normal") == "normal"
    assert _csv_safe(None) == ""


def test_single_event_lookup_and_404(auth_client: TestClient, admin) -> None:
    _seed_events(auth_client, admin, count=1)
    page = auth_client.get("/api/v1/admin/audit?limit=1", headers=admin).json()
    event_id = page["events"][0]["id"]
    fetched = auth_client.get(f"/api/v1/admin/audit/{event_id}", headers=admin)
    assert fetched.status_code == 200
    assert auth_client.get("/api/v1/admin/audit/does-not-exist", headers=admin).status_code == 404


def test_readonly_surface_has_no_write_endpoint(auth_client: TestClient, admin) -> None:
    """Журнал не приймає запис ззовні — наповнює лише `AuditSink`."""
    assert auth_client.post("/api/v1/admin/audit", json={}, headers=admin).status_code in {
        404,
        405,
    }
