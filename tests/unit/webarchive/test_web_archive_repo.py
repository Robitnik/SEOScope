"""Repository-level tests (task-60) for the new query methods that back
the backfill CLI (§1.3) and the hub's Asset coverage tile (§1.4).
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.db.repositories.clients import ClientRepository
from app.db.repositories.web_archive import WebArchiveRepository


def _client(session, *, slug: str = "acme"):
    client = ClientRepository(session).create(
        name="Acme", slug=slug, domain=f"{slug}.com", timezone="America/New_York"
    )
    session.commit()
    return client


def _snapshot(session, repo, client, *, url: str = "https://acme.com/"):
    archive_url = repo.get_or_create_url(
        client.id, url, url.replace("https://", ""), source="manual"
    )
    snapshot = repo.create_snapshot(
        archive_url_id=archive_url.id,
        client_id=client.id,
        capture_id=None,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        http_status=200,
        final_url=url,
        headers_json={},
        content_type="text/html",
        content_fingerprint="deadbeef",
        raw_sha256="a" * 64,
        body_blob_sha256=None,
        title=None, description=None, canonical=None, meta_robots=None, word_count=1,
        asset_count=0, assets_truncated=False, store_reason="first", prev_snapshot_id=None,
        last_confirmed_at=datetime(2026, 1, 1, tzinfo=UTC), confirmed_count=1,
    )
    session.commit()
    return archive_url, snapshot


def test_all_snapshots_scopes_by_client(session):
    repo = WebArchiveRepository(session)
    client_a = _client(session, slug="acme")
    client_b = _client(session, slug="beta")
    _snapshot(session, repo, client_a, url="https://acme.com/")
    _snapshot(session, repo, client_b, url="https://beta.com/")

    assert len(repo.all_snapshots()) == 2
    assert len(repo.all_snapshots(client_a.id)) == 1
    assert repo.all_snapshots(client_a.id)[0].client_id == client_a.id


def test_css_assets_for_client_only_returns_css_that_clients_own_snapshots_link(session):
    repo = WebArchiveRepository(session)
    client_a = _client(session, slug="acme")
    client_b = _client(session, slug="beta")
    _archive_url_a, snapshot_a = _snapshot(session, repo, client_a, url="https://acme.com/")
    _archive_url_b, snapshot_b = _snapshot(session, repo, client_b, url="https://beta.com/")

    css_a = repo.get_or_create_asset("https://cdn.com/a.css", "cdn.com/a.css", kind="css")
    css_b = repo.get_or_create_asset("https://cdn.com/b.css", "cdn.com/b.css", kind="css")
    img = repo.get_or_create_asset("https://cdn.com/logo.png", "cdn.com/logo.png", kind="img")
    repo.link_snapshot_asset(
        snapshot_id=snapshot_a.id, asset_id=css_a.id, asset_version_id=None, role="css"
    )
    repo.link_snapshot_asset(
        snapshot_id=snapshot_a.id, asset_id=img.id, asset_version_id=None, role="img"
    )
    repo.link_snapshot_asset(
        snapshot_id=snapshot_b.id, asset_id=css_b.id, asset_version_id=None, role="css"
    )
    session.commit()

    result = repo.css_assets_for_client(client_a.id)
    assert {a.id for a in result} == {css_a.id}  # neither client B's css nor the img asset
    assert {a.id for a in repo.all_css_assets()} == {css_a.id, css_b.id}


def test_asset_coverage_totals_counts_referenced_vs_captured(session):
    repo = WebArchiveRepository(session)
    repo.register_blob(
        "a" * 64, size_bytes=3, gzip_size_bytes=3, content_type="image/jpeg", path="x"
    )
    captured = repo.get_or_create_asset("https://cdn.com/a.jpg", "cdn.com/a.jpg", kind="img")
    repo.create_asset_version(
        asset_id=captured.id, blob_sha256="a" * 64, content_type="image/jpeg",
        http_status=200, first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_confirmed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    # never fetched — no version, so it counts as referenced but not captured
    repo.get_or_create_asset("https://cdn.com/dead.jpg", "cdn.com/dead.jpg", kind="img")
    session.commit()

    totals = repo.asset_coverage_totals()
    assert totals == {"referenced": 2, "captured": 1}
