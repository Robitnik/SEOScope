from __future__ import annotations

from datetime import UTC, datetime

from app.core.config import get_settings
from app.db.repositories.clients import ClientRepository
from app.db.repositories.web_archive import WebArchiveRepository
from app.webarchive import storage


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
    # `get_or_create_url` no longer flushes itself (task-65) — required
    # before another row's FK can reference this brand-new one in the same
    # transaction (task-63 follow-up, live-confirmed 2026-09-05).
    repo.flush()
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
    # task-70: even an untracked URL resolves `client_id`/`client_name`/
    # `capturable` when its own host matches one of our clients — the UI
    # needs to know "Archive this page now" makes sense BEFORE the click.
    body = response.json()
    assert body["found"] is False
    assert body["url"] is None
    assert body["capturable"] is True
    assert body["client_name"] == "Acme"


def test_lookup_not_found_non_client_domain(client, db):
    _create_client(db)
    response = client.get("/api/v1/archive/lookup", params={"url": "https://not-a-client.example/"})
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is False
    assert body["capturable"] is False
    assert body["client_id"] is None
    assert body["client_name"] is None


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
    assert body["status_url"] == f"/api/v1/archive/captures/{body['capture_id']}"

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


def _seed_snapshot_with_html_body(db, client_id: str, html: bytes):
    """Like `_seed_snapshot`, but with a real stored body — needed for
    `live_iframe_hosts` (task-60 §2.3), which re-reads the snapshot's HTML.
    """
    settings = get_settings()
    blob = storage.store_blob(settings.artifacts_dir, html)
    repo = WebArchiveRepository(db)
    repo.register_blob(
        blob.sha256, size_bytes=blob.size_bytes, gzip_size_bytes=blob.gzip_size_bytes,
        content_type="text/html", path=blob.path,
    )
    archive_url = repo.get_or_create_url(
        client_id, "https://acme.com/", "acme.com/", source="manual"
    )
    repo.flush()  # task-63 follow-up — see `_seed_snapshot` above
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
        raw_sha256=blob.sha256,
        body_blob_sha256=blob.sha256,
        title="Acme home", description=None, canonical=None, meta_robots=None, word_count=1,
        asset_count=0, assets_truncated=False, store_reason="first", prev_snapshot_id=None,
        last_confirmed_at=datetime(2026, 1, 1, tzinfo=UTC), confirmed_count=1,
    )
    db.commit()
    return snapshot


def test_overview_reports_asset_coverage_totals(client, db):
    repo = WebArchiveRepository(db)
    repo.register_blob(
        "a" * 64, size_bytes=3, gzip_size_bytes=3, content_type="image/jpeg", path="x"
    )
    captured = repo.get_or_create_asset("https://cdn.com/a.jpg", "cdn.com/a.jpg", kind="img")
    repo.create_asset_version(
        asset_id=captured.id, blob_sha256="a" * 64, content_type="image/jpeg",
        http_status=200, first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_confirmed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    repo.get_or_create_asset("https://cdn.com/dead.jpg", "cdn.com/dead.jpg", kind="img")
    db.commit()

    response = client.get("/api/v1/archive/overview")
    body = response.json()
    assert body["assets_referenced"] == 2
    assert body["assets_captured"] == 1


def test_snapshot_detail_reports_live_iframe_hosts(client, db):
    client_id = _create_client(db)
    html = (
        b"<html><head><title>Acme</title></head><body>"
        b'<iframe src="https://www.google.com/maps/embed?pb=abc"></iframe>'
        b'<iframe src="https://www.googletagmanager.com/ns.html?id=GTM-X"></iframe>'
        b"</body></html>"
    )
    snapshot = _seed_snapshot_with_html_body(db, client_id, html)

    response = client.get(f"/api/v1/archive/snapshots/{snapshot.id}")
    body = response.json()
    assert body["live_iframe_hosts"] == ["www.google.com"]  # GTM excluded


def test_start_capture_refuses_a_second_concurrent_run(client, db):
    client_id = _create_client(db)
    repo = WebArchiveRepository(db)
    running = repo.create_capture(
        client_id, slot_key="2026-09-04T13:30+03:00", scheduled_for=datetime.now(UTC),
        trigger="scheduled",
    )
    repo.claim_capture(running.id, owner="test")
    db.commit()

    response = client.post("/api/v1/archive/captures", params={"client_id": client_id})
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "already_running"


def test_start_capture_refuses_when_one_is_still_queued(client, db):
    """task-62 §1.3: guarding only `running` let two rapid clicks queue two
    rows for the same client before the scheduler's next tick claimed the
    first one — this is the fix, tested at the API surface.
    """
    client_id = _create_client(db)
    first = client.post("/api/v1/archive/captures", params={"client_id": client_id})
    assert first.status_code == 202

    second = client.post("/api/v1/archive/captures", params={"client_id": client_id})
    assert second.status_code == 409
    assert second.json()["detail"]["error_code"] == "already_running"


# task-80 — "Recapture": `POST /captures/force` is otherwise the same queue
# as an ordinary capture (same 404/409 guards), but the row it creates
# carries `force=True`, which is what makes the scheduler store a fresh
# snapshot even for byte-for-byte unchanged pages.
def test_start_forced_capture_enqueues_with_force_flag(client, db):
    client_id = _create_client(db)

    response = client.post("/api/v1/archive/captures/force", params={"client_id": client_id})
    assert response.status_code == 202
    body = response.json()
    assert body["capture_id"]
    assert body["status_url"] == f"/api/v1/archive/captures/{body['capture_id']}"

    listing = client.get("/api/v1/archive/captures", params={"client_id": client_id})
    captures = listing.json()
    assert len(captures) == 1
    assert captures[0]["id"] == body["capture_id"]
    assert captures[0]["scope"] == "client"
    assert captures[0]["trigger"] == "manual"
    assert captures[0]["force"] is True


def test_start_forced_capture_refuses_a_second_concurrent_run(client, db):
    client_id = _create_client(db)
    repo = WebArchiveRepository(db)
    running = repo.create_capture(
        client_id, slot_key="2026-09-04T13:30+03:00", scheduled_for=datetime.now(UTC),
        trigger="scheduled",
    )
    repo.claim_capture(running.id, owner="test")
    db.commit()

    response = client.post("/api/v1/archive/captures/force", params={"client_id": client_id})
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "already_running"


def test_start_forced_capture_unknown_client_is_404(client):
    response = client.post(
        "/api/v1/archive/captures/force", params={"client_id": "does-not-exist"}
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "client_not_found"


def test_ordinary_capture_reports_force_false(client, db):
    """The `force` field on `ArchiveCaptureDTO` is new (task-80) — an
    ordinary "Capture now" row must report `false`, not omit the field.
    """
    client_id = _create_client(db)
    started = client.post("/api/v1/archive/captures", params={"client_id": client_id})
    assert started.status_code == 202
    listing = client.get("/api/v1/archive/captures", params={"client_id": client_id})
    captures = listing.json()
    assert captures[0]["force"] is False


def test_start_url_capture_enqueues_scoped_to_that_url(client, db):
    client_id = _create_client(db)
    archive_url, _snapshot = _seed_snapshot(db, client_id)

    response = client.post("/api/v1/archive/captures/url", params={"url_id": archive_url.id})
    assert response.status_code == 202
    capture_id = response.json()["capture_id"]

    detail = client.get(f"/api/v1/archive/captures/{capture_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["scope"] == "url"
    assert body["target_url"] == archive_url.url
    assert body["status"] == "queued"


def test_start_url_capture_unknown_url_is_404(client):
    response = client.post("/api/v1/archive/captures/url", params={"url_id": "does-not-exist"})
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "url_not_found"


def test_start_url_capture_ignores_an_unrelated_client_wide_capture(client, db):
    """Same task-70 fix as the adhoc endpoint's own test above — "Refresh
    now" on one URL must not wait behind an unrelated scheduled portfolio
    pass for the same client.
    """
    client_id = _create_client(db)
    archive_url, _snapshot = _seed_snapshot(db, client_id)
    repo = WebArchiveRepository(db)
    running = repo.create_capture(
        client_id, slot_key="already", scheduled_for=datetime.now(UTC), trigger="scheduled",
    )
    repo.claim_capture(running.id, owner="test")
    db.commit()

    response = client.post("/api/v1/archive/captures/url", params={"url_id": archive_url.id})
    assert response.status_code == 202


def test_start_url_capture_refuses_a_duplicate_of_the_same_url(client, db):
    client_id = _create_client(db)
    archive_url, _snapshot = _seed_snapshot(db, client_id)
    repo = WebArchiveRepository(db)
    repo.create_capture(
        client_id, slot_key="dup", scheduled_for=datetime.now(UTC), trigger="manual",
        scope="url", archive_url_id=archive_url.id,
    )
    db.commit()

    response = client.post("/api/v1/archive/captures/url", params={"url_id": archive_url.id})
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "already_running"


def test_start_all_captures_skips_clients_already_active(client, db):
    client_a = _create_client(db, slug="acme")
    client_b = _create_client(db, slug="beta")
    repo = WebArchiveRepository(db)
    running = repo.create_capture(
        client_a, slot_key="already-running", scheduled_for=datetime.now(UTC), trigger="scheduled"
    )
    repo.claim_capture(running.id, owner="test")
    db.commit()

    response = client.post("/api/v1/archive/captures/all")
    assert response.status_code == 202
    body = response.json()
    assert client_a in body["skipped_client_ids"]
    assert len(body["capture_ids"]) == 1  # only client B got a new row

    listing = client.get("/api/v1/archive/captures", params={"client_id": client_b})
    assert len(listing.json()) == 1
    assert listing.json()[0]["status"] == "queued"


def test_capture_detail_unknown_is_404(client):
    response = client.get("/api/v1/archive/captures/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "capture_not_found"


def test_capture_detail_reports_queue_position(client, db):
    client_id = _create_client(db)
    repo = WebArchiveRepository(db)
    first = repo.create_capture(
        client_id, slot_key="a", scheduled_for=datetime.now(UTC), trigger="manual",
        now=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    )
    other_client = _create_client(db, slug="beta")
    second = repo.create_capture(
        other_client, slot_key="b", scheduled_for=datetime.now(UTC), trigger="manual",
        now=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
    )
    db.commit()

    body_first = client.get(f"/api/v1/archive/captures/{first.id}").json()
    body_second = client.get(f"/api/v1/archive/captures/{second.id}").json()
    assert body_first["queue_position"] == 1
    assert body_second["queue_position"] == 2


def test_capture_events_round_trip_and_since_filters(client, db):
    client_id = _create_client(db)
    repo = WebArchiveRepository(db)
    capture = repo.create_capture(
        client_id, slot_key="a", scheduled_for=datetime.now(UTC), trigger="manual"
    )
    repo.append_event(capture.id, "debug", stage="url", message="first")
    repo.append_event(capture.id, "warning", stage="url", message="second")
    db.commit()

    all_events = client.get(f"/api/v1/archive/captures/{capture.id}/events")
    assert [e["message"] for e in all_events.json()] == ["first", "second"]

    tail = client.get(f"/api/v1/archive/captures/{capture.id}/events", params={"since": 1})
    assert [e["message"] for e in tail.json()] == ["second"]


def test_capture_events_unknown_capture_is_404(client):
    response = client.get("/api/v1/archive/captures/does-not-exist/events")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "capture_not_found"


def test_cancel_queued_capture_finishes_it_immediately(client, db):
    client_id = _create_client(db)
    repo = WebArchiveRepository(db)
    capture = repo.create_capture(
        client_id, slot_key="a", scheduled_for=datetime.now(UTC), trigger="manual"
    )
    db.commit()

    response = client.post(f"/api/v1/archive/captures/{capture.id}/cancel")
    assert response.status_code == 200
    assert response.json() == {"capture_id": capture.id, "status": "cancelled"}


def test_cancel_running_capture_only_flags_it(client, db):
    client_id = _create_client(db)
    repo = WebArchiveRepository(db)
    capture = repo.create_capture(
        client_id, slot_key="a", scheduled_for=datetime.now(UTC), trigger="manual"
    )
    repo.claim_capture(capture.id, owner="test")
    db.commit()

    response = client.post(f"/api/v1/archive/captures/{capture.id}/cancel")
    assert response.status_code == 200
    assert response.json() == {"capture_id": capture.id, "status": "cancel_requested"}
    assert client.get(f"/api/v1/archive/captures/{capture.id}").json()["status"] == "running"


def test_cancel_unknown_capture_is_404(client):
    response = client.post("/api/v1/archive/captures/does-not-exist/cancel")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "capture_not_found"


def test_queue_view_separates_running_queued_and_recent(client, db):
    client_id = _create_client(db)
    repo = WebArchiveRepository(db)
    running = repo.create_capture(
        client_id, slot_key="a", scheduled_for=datetime.now(UTC), trigger="manual"
    )
    repo.claim_capture(running.id, owner="test")
    repo.create_capture(client_id, slot_key="b", scheduled_for=datetime.now(UTC), trigger="manual")
    db.commit()

    response = client.get("/api/v1/archive/queue")
    assert response.status_code == 200
    body = response.json()
    assert [c["id"] for c in body["running"]] == [running.id]
    assert len(body["queued"]) == 1
    assert body["queued"][0]["queue_position"] == 1
    assert len(body["recent"]) == 2


# --- task-63 follow-up: "Archive this page now" on a never-seen URL ------


def test_start_adhoc_url_capture_resolves_client_and_enqueues(client, db):
    client_id = _create_client(db)

    response = client.post(
        "/api/v1/archive/captures/url-adhoc", params={"url": "https://acme.com/new-page/"}
    )
    assert response.status_code == 202
    body = response.json()
    assert body["client_id"] == client_id
    assert body["client_name"] == "Acme"

    detail = client.get(f"/api/v1/archive/captures/{body['capture_id']}")
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["scope"] == "url"
    assert detail_body["target_url"] == "https://acme.com/new-page/"

    repo = WebArchiveRepository(db)
    archive_url = repo.get_url(body["url_id"])
    assert archive_url is not None
    assert archive_url.client_id == client_id


def test_start_adhoc_url_capture_matches_a_subdomain(client, db):
    _create_client(db)

    response = client.post(
        "/api/v1/archive/captures/url-adhoc", params={"url": "https://www.acme.com/about/"}
    )
    assert response.status_code == 202


def test_start_adhoc_url_capture_unknown_domain_is_404(client, db):
    _create_client(db)

    response = client.post(
        "/api/v1/archive/captures/url-adhoc", params={"url": "https://totally-unrelated.example/"}
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "client_not_found"


def test_start_adhoc_url_capture_ignores_an_unrelated_client_wide_capture(client, db):
    """task-70: a scheduled, `scope="client"` portfolio pass already
    `running` for this client must NOT block archiving one page of it —
    that used to be a client-WIDE lock (`has_active_capture`), and a
    portfolio pass can run for hours. `claim_capture` is the actual
    admission gate into `running` and still enforces "one running capture
    per client" regardless of how many `queued` rows exist for it.
    """
    client_id = _create_client(db)
    repo = WebArchiveRepository(db)
    running = repo.create_capture(
        client_id, slot_key="already", scheduled_for=datetime.now(UTC), trigger="scheduled",
    )
    repo.claim_capture(running.id, owner="test")
    db.commit()

    response = client.post(
        "/api/v1/archive/captures/url-adhoc", params={"url": "https://acme.com/new-page/"}
    )
    assert response.status_code == 202


def test_start_adhoc_url_capture_refuses_a_duplicate_of_the_same_url(client, db):
    client_id = _create_client(db)
    archive_url, _snapshot = _seed_snapshot(db, client_id)
    repo = WebArchiveRepository(db)
    repo.create_capture(
        client_id, slot_key="dup", scheduled_for=datetime.now(UTC), trigger="manual",
        scope="url", archive_url_id=archive_url.id,
    )
    db.commit()

    response = client.post(
        "/api/v1/archive/captures/url-adhoc", params={"url": archive_url.url}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "already_running"


def test_start_adhoc_url_capture_reuses_an_already_tracked_url(client, db):
    """A URL that's already tracked (but has no snapshot yet, or the
    operator just wants to refresh it) must not create a duplicate
    `ArchiveUrl` row — `get_or_create_url` is idempotent by its own key.
    """
    client_id = _create_client(db)
    archive_url, _snapshot = _seed_snapshot(db, client_id)

    response = client.post(
        "/api/v1/archive/captures/url-adhoc", params={"url": archive_url.url}
    )
    assert response.status_code == 202
    assert response.json()["url_id"] == archive_url.id
