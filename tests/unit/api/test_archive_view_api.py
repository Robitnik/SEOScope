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


def _seed_snapshot(
    db, *, url: str = "https://acme.com/about/", slug: str = "acme"
) -> tuple[str, str, str]:
    client_id = ClientRepository(db).create(
        name="Acme", slug=slug, domain=f"{slug}.internal", timezone="America/New_York"
    ).id
    db.commit()
    settings = get_settings()
    blob = storage.store_blob(settings.artifacts_dir, PAGE_HTML)
    repo = WebArchiveRepository(db)
    repo.register_blob(
        blob.sha256, size_bytes=blob.size_bytes, gzip_size_bytes=blob.gzip_size_bytes,
        content_type="text/html", path=blob.path,
    )
    archive_url = repo.get_or_create_url(client_id, url, normalize_url(url), source="manual")
    # `get_or_create_url` no longer flushes itself (task-65) — required
    # before another row's FK can reference this brand-new one in the same
    # transaction (task-63 follow-up, live-confirmed 2026-09-05).
    repo.flush()
    observed_at = datetime(2026, 9, 4, 13, 30, tzinfo=UTC)
    snapshot = repo.create_snapshot(
        archive_url_id=archive_url.id, client_id=client_id, capture_id=None,
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


def test_view_shell_404s_when_the_url_segment_has_no_captured_version(client, db):
    """task-70: the address's own `url` segment is the actual identity of
    this route — a `snapshot_id` that belongs to a DIFFERENT, real page
    must never let that page stand in for one that was never captured.
    This is the exact bug report: a redirect once left an unrelated
    snapshot rendering under a bogus url's address.
    """
    snapshot_id, ts, _url = _seed_snapshot(db)
    response = client.get(
        f"/archive/view/snapshot/{snapshot_id}/{ts}/https://acme.com/never-captured"
    )
    assert response.status_code == 404
    assert 'id="archive-view-page-root"' in response.text
    # No canonical link is emitted for a genuine miss — nothing to point at.
    assert "rel=\"canonical\"" not in response.text


def test_view_shell_canonicalizes_to_the_real_page_on_a_url_mismatch(client, db):
    """The requested `url` names a DIFFERENT page than `snapshot_id` — that
    page wins, and the shell's canonical points at its own resolved
    snapshot, not the one named in the address.
    """
    snapshot_id, ts, _url = _seed_snapshot(db, url="https://acme.com/about/")
    other_id, other_ts, other_url = _seed_snapshot(
        db, url="https://acme.com/pricing/", slug="acme-pricing"
    )

    response = client.get(f"/archive/view/snapshot/{snapshot_id}/{ts}/{other_url}")

    assert response.status_code == 200
    canonical = (
        f'<link rel="canonical" href="/archive/view/snapshot/{other_id}/{other_ts}/{other_url}">'
    )
    assert canonical in response.text


def test_view_shell_preserves_a_query_string_url(client, db):
    """A LAXTON-style `?taxonomy=...` page is a real, distinct page — the
    query belongs to the ARCHIVED url, not to this route's own params.
    """
    snapshot_id, ts, url = _seed_snapshot(db, url="https://acme.com/shop/?taxonomy=color&term=red")
    response = client.get(f"/archive/view/snapshot/{snapshot_id}/{ts}/{url}")

    assert response.status_code == 200
    assert "taxonomy=color" in response.text
