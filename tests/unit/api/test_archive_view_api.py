"""API-level tests for the replay viewer's server-backed address (task-63
§1.2) — `GET /archive/view/snapshot/<id>/<ts>/<url>`.

This route hands back the SAME frontend shell every other route serves
(the SPA's own client-side router takes it from there); the only thing
tested here is what's specific to this route: the URL alone must resolve
to a page, and a dead id must fail honestly rather than 500 or serve a
misleading title.
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

PAGE_HTML = b"<html><head><title>Acme</title></head><body>hi</body></html>"


def _seed_snapshot(db, *, url: str = "https://acme.com/about/") -> tuple[str, str, str]:
    client = ClientRepository(db).create(
        name="Acme", slug="acme", domain="acme.com", timezone="America/New_York"
    )
    db.commit()
    settings = get_settings()
    blob = storage.store_blob(settings.artifacts_dir, PAGE_HTML)
    repo = WebArchiveRepository(db)
    repo.register_blob(
        blob.sha256, size_bytes=blob.size_bytes, gzip_size_bytes=blob.gzip_size_bytes,
        content_type="text/html", path=blob.path,
    )
    archive_url = repo.get_or_create_url(client.id, url, normalize_url(url), source="manual")
    observed_at = datetime(2026, 9, 4, 13, 30, tzinfo=UTC)
    snapshot = repo.create_snapshot(
        archive_url_id=archive_url.id, client_id=client.id, capture_id=None,
        observed_at=observed_at, http_status=200, final_url=url,
        headers_json={}, content_type="text/html",
        content_fingerprint=content_fingerprint(PAGE_HTML.decode()),
        raw_sha256=blob.sha256, body_blob_sha256=blob.sha256,
        title="Acme", description=None, canonical=None, meta_robots=None, word_count=1,
        asset_count=0, assets_truncated=False, store_reason="first", prev_snapshot_id=None,
        last_confirmed_at=observed_at, confirmed_count=1,
    )
    db.commit()
    return snapshot.id, format_ts(observed_at), url


def test_view_shell_serves_the_frontend_app_for_a_real_snapshot(client, db):
    snapshot_id, ts, url = _seed_snapshot(db)
    response = client.get(f"/archive/view/snapshot/{snapshot_id}/{ts}/{url}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    # The SPA's own root element must be present — this is the same shell
    # every other route serves, not a bespoke server-rendered page.
    assert 'id="archive-view-page-root"' in response.text


def test_view_shell_sets_a_page_specific_title_and_canonical(client, db):
    snapshot_id, ts, url = _seed_snapshot(db)
    response = client.get(f"/archive/view/snapshot/{snapshot_id}/{ts}/{url}")

    assert url in response.text
    canonical = f'<link rel="canonical" href="/archive/view/snapshot/{snapshot_id}/{ts}/{url}">'
    assert canonical in response.text


def test_view_shell_accepts_a_percent_encoded_url(client, db):
    snapshot_id, ts, url = _seed_snapshot(db)
    import urllib.parse

    encoded = urllib.parse.quote(url, safe="")
    response = client.get(f"/archive/view/snapshot/{snapshot_id}/{ts}/{encoded}")

    assert response.status_code == 200
    assert 'id="archive-view-page-root"' in response.text


def test_view_shell_404s_honestly_for_an_unknown_snapshot(client, db):
    response = client.get(
        "/archive/view/snapshot/00000000-0000-0000-0000-000000000000/20260904133000/https://acme.com/"
    )
    assert response.status_code == 404
    # Still the real shell — the SPA's own client-side error state takes it
    # from here (PLAN §12's "chesni stany", not a bare server error page).
    assert 'id="archive-view-page-root"' in response.text


def test_view_shell_preserves_a_query_string_url(client, db):
    """A LAXTON-style `?taxonomy=...` page is a real, distinct page — the
    query belongs to the ARCHIVED url, not to this route's own params.
    """
    snapshot_id, ts, url = _seed_snapshot(db, url="https://acme.com/shop/?taxonomy=color&term=red")
    response = client.get(f"/archive/view/snapshot/{snapshot_id}/{ts}/{url}")

    assert response.status_code == 200
    assert "taxonomy=color" in response.text
