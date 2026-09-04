"""API-level tests for the replay route (task-59 §1.5/§1.6) — the single
most safety-critical surface in the archive: an unrewritten reference must
never silently fall through to the live internet, and every response must
carry the replay's own CSP (not the app-wide default).
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.config import get_settings
from app.db.repositories.clients import ClientRepository
from app.db.repositories.web_archive import WebArchiveRepository
from app.webarchive import storage
from app.webarchive.fingerprint import content_fingerprint
from app.webarchive.replay import format_ts
from app.webarchive.urls import normalize_url

PAGE_HTML = (
    b"<html><head><title>Acme</title></head><body>"
    b'<a href="/other">Other</a><img src="https://cdn.example.com/logo.png">'
    b"</body></html>"
)


def _seed_page(
    db,
    *,
    url: str = "https://acme.com/",
    body: bytes = PAGE_HTML,
    content_type: str = "text/html",
) -> tuple[str, str]:
    client = ClientRepository(db).create(
        name="Acme", slug="acme", domain="acme.com", timezone="America/New_York"
    )
    db.commit()
    settings = get_settings()
    blob = storage.store_blob(settings.artifacts_dir, body)
    repo = WebArchiveRepository(db)
    repo.register_blob(
        blob.sha256, size_bytes=blob.size_bytes, gzip_size_bytes=blob.gzip_size_bytes,
        content_type=content_type, path=blob.path,
    )
    archive_url = repo.get_or_create_url(client.id, url, normalize_url(url), source="manual")
    observed_at = datetime(2026, 9, 4, 13, 30, tzinfo=UTC)
    repo.create_snapshot(
        archive_url_id=archive_url.id,
        client_id=client.id,
        capture_id=None,
        observed_at=observed_at,
        http_status=200,
        final_url=url,
        headers_json={"Content-Type": content_type, "Set-Cookie": "session=leak-me"},
        content_type=content_type,
        content_fingerprint=content_fingerprint(body.decode("utf-8", errors="replace")),
        raw_sha256=blob.sha256,
        body_blob_sha256=blob.sha256,
        title="Acme",
        description=None,
        canonical=None,
        meta_robots=None,
        word_count=1,
        asset_count=0,
        assets_truncated=False,
        store_reason="first",
        prev_snapshot_id=None,
        last_confirmed_at=observed_at,
        confirmed_count=1,
    )
    db.commit()
    return format_ts(observed_at), url


def test_replay_page_rewrites_external_reference_and_sets_security_headers(client, db):
    ts, url = _seed_page(db)
    response = client.get(f"/archive/r/{ts}/{url}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert f"/archive/r/{ts}/https://cdn.example.com/logo.png" in response.text
    assert f"/archive/r/{ts}/https://acme.com/other" in response.text
    # The external host appears ONLY as the tail of our own replay path —
    # never as a bare, directly-fetchable reference (task-59 §1.6).
    idx = response.text.index("cdn.example.com")
    assert f"/archive/r/{ts}/" in response.text[max(0, idx - 60) : idx]

    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "cdn.example.com" not in csp
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["access-control-allow-origin"] == "*"
    # `Set-Cookie` must never survive from capture to replay (PLAN §13).
    assert "set-cookie" not in {k.lower() for k in response.headers.keys()}


def test_replay_injects_shim_script(client, db):
    ts, url = _seed_page(db)
    response = client.get(f"/archive/r/{ts}/{url}")
    assert "/archive/static/shim.js" in response.text
    assert f'data-ts="{ts}"' in response.text


def test_replay_miss_returns_404_and_records_it(client, db):
    response = client.get("/archive/r/20260904133000/https://never-captured.example.com/")
    assert response.status_code == 404
    assert response.headers["content-security-policy"]  # still carries the replay CSP

    # A second miss on the SAME url increments the same row rather than
    # creating a duplicate (task-59 §1.6: our own honest list of gaps).
    client.get("/archive/r/20260904133000/https://never-captured.example.com/")

    from sqlalchemy import select

    from app.db.models import ArchiveReplayMiss

    rows = list(db.scalars(select(ArchiveReplayMiss)))
    assert len(rows) == 1
    assert rows[0].hit_count == 2


def test_replay_resolves_nearest_snapshot_before_requested_time(client, db):
    ts, url = _seed_page(db)
    later_ts = format_ts(datetime(2026, 9, 4, 19, 30, tzinfo=UTC))
    response = client.get(f"/archive/r/{later_ts}/{url}")
    assert response.status_code == 200
    assert "Acme" in response.text


def test_shim_script_served_with_cors_header(client):
    response = client.get("/archive/static/shim.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["access-control-allow-origin"] == "*"
    assert "__archiveNav" in response.text


def test_replay_serves_binary_page_row_as_raw_asset(client, db):
    """Live incident (task-59, 2026-09-04): a gallery lightbox link got
    queued and captured as a "page" whose real content is an image. Even
    for an already-bad row like this, replay must serve the raw bytes with
    their real content-type — never run it through `rewrite_html` +
    shim-injection and label it `text/html`, which is what produced a
    broken `<img>` in the browser.
    """
    webp_bytes = b"RIFF\x00\x00\x00\x00WEBPVP8 not-a-real-page"
    ts, url = _seed_page(
        db, url="https://acme.com/gallery/photo-scaled.webp",
        body=webp_bytes, content_type="image/webp",
    )
    response = client.get(f"/archive/r/{ts}/{url}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.content == webp_bytes
    assert "/archive/static/shim.js" not in response.text


def test_replay_options_preflight_answered(client, db):
    ts, url = _seed_page(db)
    response = client.options(f"/archive/r/{ts}/{url}")
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "*"
