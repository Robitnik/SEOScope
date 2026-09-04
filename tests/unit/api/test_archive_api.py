from __future__ import annotations

from datetime import UTC, datetime

from app.db.repositories.clients import ClientRepository
from app.db.repositories.web_archive import WebArchiveRepository


def _create_client(db, *, slug: str = "acme") -> str:
    client = ClientRepository(db).create(
        name="Acme", slug=slug, domain=f"{slug}.com", timezone="America/New_York"
    )
    db.commit()
    return client.id


def _seed_snapshot(db, client_id: str):
    repo = WebArchiveRepository(db)
    archive_url = repo.get_or_create_url(
        client_id, "https://acme.com/", "acme.com/", source="manual"
    )
    snapshot = repo.create_snapshot(
        archive_url_id=archive_url.id,
        client_id=client_id,
        capture_id=None,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        http_status=200,
        final_url="https://acme.com/",
        headers_json={"Content-Type": "text/html"},
        content_type="text/html",
        content_fingerprint="deadbeef",
        raw_sha256="a" * 64,
        body_blob_sha256=None,
        title="Acme home",
        description="Welcome",
        canonical="https://acme.com/",
        meta_robots=None,
        word_count=42,
        asset_count=0,
        assets_truncated=False,
        store_reason="first",
        prev_snapshot_id=None,
        last_confirmed_at=datetime(2026, 1, 1, tzinfo=UTC),
        confirmed_count=1,
    )
    db.commit()
    return archive_url, snapshot


def test_overview_on_empty_portfolio(client):
    response = client.get("/api/v1/archive/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["clients"] == []
    assert body["urls_total"] == 0
    assert body["snapshots_total"] == 0


def test_lookup_not_found(client, db):
    _create_client(db)
    response = client.get("/api/v1/archive/lookup", params={"url": "https://acme.com/nope"})
    assert response.status_code == 200
    assert response.json() == {"found": False, "url": None}


def test_lookup_and_timeline_after_seeding(client, db):
    client_id = _create_client(db)
    archive_url, snapshot = _seed_snapshot(db, client_id)

    lookup = client.get("/api/v1/archive/lookup", params={"url": "https://www.acme.com/"})
    assert lookup.status_code == 200
    body = lookup.json()
    assert body["found"] is True
    assert body["url"]["id"] == archive_url.id
    assert body["url"]["client_name"] == "Acme"

    timeline = client.get(f"/api/v1/archive/urls/{archive_url.id}/snapshots")
    assert timeline.status_code == 200
    payload = timeline.json()
    assert payload["url"]["url"] == "https://acme.com/"
    assert len(payload["snapshots"]) == 1
    assert payload["snapshots"][0]["id"] == snapshot.id
    assert payload["snapshots"][0]["store_reason"] == "first"
    assert payload["snapshots"][0]["ts"] == "20260101000000"


def test_timeline_unknown_url_is_404(client):
    response = client.get("/api/v1/archive/urls/does-not-exist/snapshots")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "url_not_found"


def test_snapshot_detail_and_diff_no_previous(client, db):
    client_id = _create_client(db)
    _archive_url, snapshot = _seed_snapshot(db, client_id)

    detail = client.get(f"/api/v1/archive/snapshots/{snapshot.id}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["title"] == "Acme home"
    assert payload["view_url"] == "/archive/r/20260101000000/https://acme.com/"
    assert payload["prev_snapshot_id"] is None
    assert payload["next_snapshot_id"] is None

    diff = client.get(f"/api/v1/archive/snapshots/{snapshot.id}/diff")
    assert diff.status_code == 404
    assert diff.json()["detail"]["error_code"] == "no_previous_snapshot"


def test_snapshot_detail_unknown_is_404(client):
    response = client.get("/api/v1/archive/snapshots/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "snapshot_not_found"


def test_start_capture_enqueues_and_lists(client, db):
    client_id = _create_client(db)

    response = client.post("/api/v1/archive/captures", params={"client_id": client_id})
    assert response.status_code == 202
    body = response.json()
    assert body["capture_id"]
    assert body["status_url"] == "/api/v1/archive/captures"

    listing = client.get("/api/v1/archive/captures", params={"client_id": client_id})
    assert listing.status_code == 200
    captures = listing.json()
    assert len(captures) == 1
    assert captures[0]["id"] == body["capture_id"]
    assert captures[0]["status"] == "queued"
    assert captures[0]["trigger"] == "manual"


def test_start_capture_unknown_client_is_404(client):
    response = client.post("/api/v1/archive/captures", params={"client_id": "does-not-exist"})
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "client_not_found"


def test_changes_feed_empty(client, db):
    _create_client(db)
    response = client.get("/api/v1/archive/changes")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_start_capture_refuses_a_second_concurrent_run(client, db):
    client_id = _create_client(db)
    repo = WebArchiveRepository(db)
    running = repo.create_capture(
        client_id, slot_key="2026-09-04T13:30+03:00", scheduled_for=datetime.now(UTC),
        trigger="scheduled",
    )
    repo.claim_capture(running.id)
    db.commit()

    response = client.post("/api/v1/archive/captures", params={"client_id": client_id})
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "already_running"
