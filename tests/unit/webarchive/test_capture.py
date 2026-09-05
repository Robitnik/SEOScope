"""Capture pipeline (task-59 §3) integration tests: a fake `fetch()` swapped
into `app.webarchive.capture`'s own namespace, a real temp SQLite session,
real on-disk blob storage (`tmp_path`) — everything except the network.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.db.repositories.clients import ClientRepository
from app.db.repositories.web_archive import WebArchiveRepository
from app.monitor.fetcher import FetchResult
from app.tools.http_safety import HostThrottle
from app.webarchive import capture as capturemod

PAGE_URL = "https://acme.example.com/"
PAGE_KEY = "acme.example.com/"
HTML_V1 = b"<html><head><title>Acme</title></head><body><p>Hello world</p></body></html>"
HTML_V2 = b"<html><head><title>Acme</title></head><body><p>Hello world, updated</p></body></html>"


def _client(session):
    client = ClientRepository(session).create(
        name="Acme", slug="acme", domain="acme.example.com", timezone="America/New_York"
    )
    session.commit()
    return client


def _result(url, body, *, status=200, final_url=None, headers=None, error=None):
    return FetchResult(
        requested_url=url,
        final_url=final_url or url,
        status=status,
        hop_count=0,
        headers=headers or {},
        body=body,
        truncated_body=False,
        elapsed_ms=1,
        error=error,
    )


class FakeFetch:
    """Routes by URL to a canned `FetchResult`; records call counts per URL."""

    def __init__(self):
        self.responses: dict[str, FetchResult] = {}
        self.calls: dict[str, int] = {}

    def set(self, url, result):
        self.responses[url] = result

    def __call__(self, url, *, throttle, stats, timeout=20.0, max_body_bytes=0,
                 method="GET", extra_headers=None):
        self.calls[url] = self.calls.get(url, 0) + 1
        return self.responses[url]


@pytest.fixture
def fake_fetch(monkeypatch):
    fake = FakeFetch()
    monkeypatch.setattr(capturemod, "fetch", fake)
    return fake


@pytest.fixture
def settings(artifacts_dir):
    return Settings(
        _env_file=None,
        artifacts_dir=artifacts_dir,
        # 0 so tests never pay HostThrottle's real min-interval sleep.
        archive_host_min_interval_seconds=0.0,
    )


def _throttle():
    return HostThrottle(min_interval_seconds=0.0)


def _capture_one(
    session, repo, client, archive_url, *, settings, artifacts_dir, now, capture_assets=False,
    write_controller=None,
):
    capture = repo.create_capture(
        client.id, slot_key=f"manual:{now.isoformat()}", scheduled_for=now, trigger="manual"
    )
    session.commit()
    controller = write_controller or _OwnController()
    result = capturemod.capture_url(
        session, repo, client, archive_url,
        capture_id=capture.id, settings=settings, artifacts_dir=artifacts_dir,
        throttle=_throttle(), stats=capturemod.FetchStats(), now=now, capture_assets=capture_assets,
        write_controller=controller,
    )
    session.commit()
    return result


class _OwnController:
    """A throwaway `ArchiveWriteController` for tests that don't otherwise
    need one: same `submit(session, fn)` shape, but applies the write
    immediately on the calling thread — there is only ever one caller in
    these single-threaded tests, so real cross-thread serialization buys
    nothing and starting/stopping a background thread per call is wasted
    setup. Tests that care about the controller's OWN behavior use the real
    `write_controller` fixture instead.
    """

    def submit(self, session, fn):
        from app.webarchive.db_retry import commit_with_retry

        return commit_with_retry(session, fn)


def _seed(session):
    client = _client(session)
    repo = WebArchiveRepository(session)
    archive_url = repo.get_or_create_url(client.id, PAGE_URL, PAGE_KEY, source="manual")
    session.commit()
    return client, repo, archive_url


def test_first_capture_creates_snapshot_with_reason_first(
    session, artifacts_dir, settings, fake_fetch
):
    client, repo, archive_url = _seed(session)
    fake_fetch.set(PAGE_URL, _result(PAGE_URL, HTML_V1, headers={"Content-Type": "text/html"}))

    result = _capture_one(
        session, repo, client, archive_url,
        settings=settings, artifacts_dir=artifacts_dir, now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert result.outcome == "new"
    snapshot = repo.latest_snapshot(archive_url.id)
    assert snapshot is not None
    assert snapshot.store_reason == "first"
    assert snapshot.title == "Acme"
    assert snapshot.confirmed_count == 1
    assert repo.get_change_by_snapshot(snapshot.id) is None  # first capture never alerts


def test_second_capture_unchanged_confirms_without_new_snapshot(
    session, artifacts_dir, settings, fake_fetch
):
    client, repo, archive_url = _seed(session)
    fake_fetch.set(PAGE_URL, _result(PAGE_URL, HTML_V1, headers={"Content-Type": "text/html"}))
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    _capture_one(
        session, repo, client, archive_url, settings=settings, artifacts_dir=artifacts_dir, now=t0
    )

    result = _capture_one(
        session, repo, client, archive_url,
        settings=settings, artifacts_dir=artifacts_dir, now=t0 + timedelta(hours=6),
    )

    assert result.outcome == "unchanged"
    snapshots = repo.list_snapshots_for_url(archive_url.id)
    assert len(snapshots) == 1
    assert snapshots[0].confirmed_count == 2
    assert snapshots[0].last_confirmed_at == t0 + timedelta(hours=6)


def test_changed_content_writes_new_snapshot_and_a_change_row(
    session, artifacts_dir, settings, fake_fetch
):
    client, repo, archive_url = _seed(session)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    fake_fetch.set(PAGE_URL, _result(PAGE_URL, HTML_V1, headers={"Content-Type": "text/html"}))
    _capture_one(
        session, repo, client, archive_url, settings=settings, artifacts_dir=artifacts_dir, now=t0
    )

    fake_fetch.set(PAGE_URL, _result(PAGE_URL, HTML_V2, headers={"Content-Type": "text/html"}))
    result = _capture_one(
        session, repo, client, archive_url,
        settings=settings, artifacts_dir=artifacts_dir, now=t0 + timedelta(hours=12),
    )

    assert result.outcome == "new"
    snapshots = repo.list_snapshots_for_url(archive_url.id)  # newest first
    assert len(snapshots) == 2
    assert snapshots[0].store_reason == "changed"
    assert snapshots[0].prev_snapshot_id == snapshots[1].id
    change = repo.get_change_by_snapshot(snapshots[0].id)
    assert change is not None
    assert "text" in change.change_kinds_json


def test_weekly_refresh_writes_snapshot_without_change_row(
    session, artifacts_dir, settings, fake_fetch
):
    client, repo, archive_url = _seed(session)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    fake_fetch.set(PAGE_URL, _result(PAGE_URL, HTML_V1, headers={"Content-Type": "text/html"}))
    _capture_one(
        session, repo, client, archive_url, settings=settings, artifacts_dir=artifacts_dir, now=t0
    )

    # Same bytes, 8 days later — past `archive_refresh_after_days` (7) — must
    # still write a fresh anchor snapshot even though nothing changed.
    result = _capture_one(
        session, repo, client, archive_url,
        settings=settings, artifacts_dir=artifacts_dir, now=t0 + timedelta(days=8),
    )

    assert result.outcome == "new"
    snapshots = repo.list_snapshots_for_url(archive_url.id)
    assert len(snapshots) == 2
    assert snapshots[0].store_reason == "weekly_refresh"
    assert repo.get_change_by_snapshot(snapshots[0].id) is None


def test_blocked_result_writes_no_snapshot(session, artifacts_dir, settings, fake_fetch):
    client, repo, archive_url = _seed(session)
    fake_fetch.set(PAGE_URL, _result(PAGE_URL, None, status=429, error="blocked"))

    result = _capture_one(
        session, repo, client, archive_url,
        settings=settings, artifacts_dir=artifacts_dir, now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert result.outcome == "blocked"
    assert repo.latest_snapshot(archive_url.id) is None


def test_failed_fetch_writes_no_snapshot_and_does_not_crash(
    session, artifacts_dir, settings, fake_fetch
):
    client, repo, archive_url = _seed(session)
    fake_fetch.set(PAGE_URL, _result(PAGE_URL, None, status=None, error="timeout"))

    result = _capture_one(
        session, repo, client, archive_url,
        settings=settings, artifacts_dir=artifacts_dir, now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert result.outcome == "failed"
    assert repo.latest_snapshot(archive_url.id) is None


def test_asset_is_not_rechecked_within_revalidate_window(
    session, artifacts_dir, settings, fake_fetch
):
    asset_url = "https://acme.example.com/logo.png"
    html_v1 = b'<html><head><title>Acme</title></head><body><img src="/logo.png"></body></html>'
    html_v2 = (
        b'<html><head><title>Acme</title></head><body><img src="/logo.png"><p>new</p></body></html>'
    )
    client, repo, archive_url = _seed(session)
    t0 = datetime(2026, 1, 1, tzinfo=UTC)

    fake_fetch.set(PAGE_URL, _result(PAGE_URL, html_v1, headers={"Content-Type": "text/html"}))
    fake_fetch.set(asset_url, _result(asset_url, b"PNGDATA", headers={"Content-Type": "image/png"}))
    result1 = _capture_one(
        session, repo, client, archive_url,
        settings=settings, artifacts_dir=artifacts_dir, now=t0, capture_assets=True,
    )
    assert result1.outcome == "new"
    assert fake_fetch.calls[asset_url] == 1

    # Page unchanged the next day — assets are not touched at all (per
    # design: an unchanged page's assets are, by definition, unchanged too).
    result2 = _capture_one(
        session, repo, client, archive_url,
        settings=settings, artifacts_dir=artifacts_dir,
        now=t0 + timedelta(days=1), capture_assets=True,
    )
    assert result2.outcome == "unchanged"
    assert fake_fetch.calls[asset_url] == 1

    # Page changes 2 days in — still within the asset's 7-day revalidate
    # window, so the SAME asset is reused without a re-request.
    fake_fetch.set(PAGE_URL, _result(PAGE_URL, html_v2, headers={"Content-Type": "text/html"}))
    result3 = _capture_one(
        session, repo, client, archive_url,
        settings=settings, artifacts_dir=artifacts_dir,
        now=t0 + timedelta(days=2), capture_assets=True,
    )
    assert result3.outcome == "new"
    assert fake_fetch.calls[asset_url] == 1

    # 9 days after the asset was last checked — past the revalidate window —
    # it gets checked again exactly once more.
    html_v3 = html_v2.replace(b"new", b"new again")
    fake_fetch.set(PAGE_URL, _result(PAGE_URL, html_v3, headers={"Content-Type": "text/html"}))
    result4 = _capture_one(
        session, repo, client, archive_url,
        settings=settings, artifacts_dir=artifacts_dir,
        now=t0 + timedelta(days=11), capture_assets=True,
    )
    assert result4.outcome == "new"
    assert fake_fetch.calls[asset_url] == 2


def test_run_client_capture_isolates_one_url_crash(
    session, artifacts_dir, settings, monkeypatch, write_controller, session_scope
):
    client = _client(session)
    repo = WebArchiveRepository(session)
    repo.get_or_create_url(
        client.id, "https://acme.example.com/a", "acme.example.com/a", source="manual"
    )
    repo.get_or_create_url(
        client.id, "https://acme.example.com/b", "acme.example.com/b", source="manual"
    )
    session.commit()

    def _raising_fetch(url, **kwargs):
        if url.endswith("/a"):
            raise RuntimeError("boom")
        return _result(url, HTML_V1, headers={"Content-Type": "text/html"})

    monkeypatch.setattr(capturemod, "fetch", _raising_fetch)
    capture = repo.create_capture(
        client.id, slot_key="manual:isolation", scheduled_for=datetime.now(UTC), trigger="manual"
    )
    session.commit()

    outcome = capturemod.run_client_capture(
        session, client, capture_id=capture.id, settings=settings, artifacts_dir=artifacts_dir,
        write_controller=write_controller, session_scope=session_scope,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert outcome.urls_total == 2
    assert outcome.urls_failed == 1
    assert outcome.urls_new == 1
    assert outcome.status == "partial"


def test_run_client_capture_refetches_each_url_by_id(
    session, artifacts_dir, settings, fake_fetch, monkeypatch, write_controller, session_scope
):
    """Live incident (task-59, 2026-09-04): a rollback on one cell expires
    every ORM object already loaded in the session, including OTHER
    not-yet-processed `ArchiveUrl` rows — silently corrupting cells that
    never actually failed. The fix re-fetches a fresh row by id at the top
    of every iteration; this asserts that refetch actually happens once per
    URL, not that the loop merely reuses whatever it already had in hand.
    """
    url_a, url_b = "https://acme.example.com/a", "https://acme.example.com/b"
    client = _client(session)
    repo = WebArchiveRepository(session)
    repo.get_or_create_url(client.id, url_a, "acme.example.com/a", source="manual")
    repo.get_or_create_url(client.id, url_b, "acme.example.com/b", source="manual")
    session.commit()
    fake_fetch.set(url_a, _result(url_a, HTML_V1, headers={"Content-Type": "text/html"}))
    fake_fetch.set(url_b, _result(url_b, HTML_V1, headers={"Content-Type": "text/html"}))
    capture = repo.create_capture(
        client.id, slot_key="manual:refetch", scheduled_for=datetime.now(UTC), trigger="manual"
    )
    session.commit()

    calls: list[str] = []
    original_get_url = WebArchiveRepository.get_url

    def _spy(self, url_id):
        calls.append(url_id)
        return original_get_url(self, url_id)

    monkeypatch.setattr(WebArchiveRepository, "get_url", _spy)

    outcome = capturemod.run_client_capture(
        session, client, capture_id=capture.id, settings=settings, artifacts_dir=artifacts_dir,
        write_controller=write_controller, session_scope=session_scope,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert outcome.urls_new == 2
    assert len(calls) == 2
    assert len(set(calls)) == 2  # each URL id fetched exactly once, freshly


def test_one_crashing_asset_does_not_fail_the_whole_page(
    session, artifacts_dir, settings, monkeypatch
):
    """Live incident (task-59, 2026-09-04): an asset URL that crashed
    `urllib` (before `app.webarchive.urls._ascii_safe` existed) took the
    ENTIRE page down with it. One asset is its own cell within the page
    (PLAN §9.1's isolation principle applied one level deeper) — the page
    must still capture successfully, with just that one asset missing.
    """
    html = (
        b'<html><head><title>Acme</title></head><body>'
        b'<img src="/good.png"><img src="/bad.png"></body></html>'
    )
    client, repo, archive_url = _seed(session)

    def _fetch(url, **kwargs):
        if url == PAGE_URL:
            return _result(PAGE_URL, html, headers={"Content-Type": "text/html"})
        if url.endswith("/bad.png"):
            raise UnicodeEncodeError("ascii", "x", 0, 1, "boom")
        return _result(url, b"PNGDATA", headers={"Content-Type": "image/png"})

    monkeypatch.setattr(capturemod, "fetch", _fetch)

    result = _capture_one(
        session, repo, client, archive_url,
        settings=settings, artifacts_dir=artifacts_dir,
        now=datetime(2026, 1, 1, tzinfo=UTC), capture_assets=True,
    )

    assert result.outcome == "new"  # the PAGE still captured successfully
    assert result.assets_failed == 1
    assert result.assets_fetched == 1
    snapshot = repo.latest_snapshot(archive_url.id)
    assert snapshot is not None
    assert snapshot.title == "Acme"


def test_binary_content_type_deletes_url_instead_of_storing_page_snapshot(
    session, artifacts_dir, settings, fake_fetch
):
    """Live incident (task-59, 2026-09-04): EFFYS's gallery wraps each
    thumbnail in `<a href="...-scaled.webp">` for a lightbox popup, which
    got queued into the page frontier by an earlier capture pass (before
    `is_asset_like_url` existed) and re-fetched here as if it were a page.
    The response is a real image — this must never be stored as a "page"
    snapshot, since a page-shaped row for the same URL would shadow the
    correctly-captured `archive_assets` entry at replay time.
    """
    client, repo, archive_url = _seed(session)
    fake_fetch.set(
        PAGE_URL, _result(PAGE_URL, b"\x89PNG\r\n\x1a\n...", headers={"Content-Type": "image/webp"})
    )

    result = _capture_one(
        session, repo, client, archive_url,
        settings=settings, artifacts_dir=artifacts_dir, now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert result.outcome == "failed"
    assert repo.latest_snapshot(archive_url.id) is None
    assert repo.get_url(archive_url.id) is None  # dropped from the page frontier entirely


def test_discover_same_host_links_skips_asset_like_urls(session):
    client, repo, archive_url = _seed(session)

    capturemod._discover_same_host_links(
        repo, client, archive_url,
        frozenset(
            {
                "https://acme.example.com/gallery/photo-scaled.webp",  # lightbox target
                "https://acme.example.com/about-us",  # a real page
            }
        ),
    )
    session.commit()

    assert repo.find_url(client.id, "acme.example.com/about-us") is not None
    assert repo.find_url(client.id, "acme.example.com/gallery/photo-scaled.webp") is None


def test_has_running_capture_blocks_a_second_manual_run(session):
    client, repo, _archive_url = _seed(session)
    running = repo.create_capture(
        client.id, slot_key="2026-09-04T13:30+03:00", scheduled_for=datetime.now(UTC),
        trigger="scheduled",
    )
    repo.claim_capture(running.id, owner="test")
    session.commit()

    assert repo.has_running_capture(client.id) is True

    repo.finish_capture(running.id, status="ok")
    session.commit()
    assert repo.has_running_capture(client.id) is False


def test_css_link_body_is_expanded_for_background_and_font_urls(
    session, artifacts_dir, settings, fake_fetch
):
    """task-60 §1.2 — the fix for the reported bug (plan §0.1): a
    background image (and a font) referenced ONLY from a fetched CSS
    file's own body was never discovered before this queue existed.
    """
    css_url = "https://acme.example.com/style.css"
    bg_url = "https://acme.example.com/bg.jpg"
    font_url = "https://acme.example.com/fonts/acme.woff2"
    html = (
        b'<html><head><title>Acme</title>'
        b'<link rel="stylesheet" href="/style.css"></head><body></body></html>'
    )
    css_body = (
        b".hero{background:url(/bg.jpg)}"
        b"@font-face{font-family:'Acme';src:url(/fonts/acme.woff2)}"
    )
    client, repo, archive_url = _seed(session)
    fake_fetch.set(PAGE_URL, _result(PAGE_URL, html, headers={"Content-Type": "text/html"}))
    fake_fetch.set(css_url, _result(css_url, css_body, headers={"Content-Type": "text/css"}))
    fake_fetch.set(bg_url, _result(bg_url, b"JPEGDATA", headers={"Content-Type": "image/jpeg"}))
    fake_fetch.set(
        font_url, _result(font_url, b"WOFF2DATA", headers={"Content-Type": "font/woff2"})
    )

    result = _capture_one(
        session, repo, client, archive_url,
        settings=settings, artifacts_dir=artifacts_dir,
        now=datetime(2026, 1, 1, tzinfo=UTC), capture_assets=True,
    )

    assert result.outcome == "new"
    assert fake_fetch.calls[bg_url] == 1
    assert fake_fetch.calls[font_url] == 1
    bg_asset = repo.get_asset_by_key("acme.example.com/bg.jpg")
    font_asset = repo.get_asset_by_key("acme.example.com/fonts/acme.woff2")
    assert bg_asset is not None and repo.latest_asset_version(bg_asset.id) is not None
    assert font_asset is not None and repo.latest_asset_version(font_asset.id) is not None


def test_css_body_is_expanded_at_most_once_per_page_even_with_aliased_refs(
    session, artifacts_dir, settings, fake_fetch, monkeypatch
):
    """Two `<link>` tags reference the same stylesheet under URL strings
    that only collapse to one asset AFTER `normalize_url` strips a
    tracking parameter — `discover_html_assets` dedups by exact string,
    not by normalized key, so both surface as separate initial queue
    entries. The css body must still only be parsed for nested assets
    once per page (task-60 §1.2, §3 step 2).
    """
    css_url = "https://acme.example.com/style.css"
    html = (
        b'<html><head><title>Acme</title>'
        b'<link rel="stylesheet" href="/style.css">'
        b'<link rel="stylesheet" href="/style.css?utm_source=aliased">'
        b"</head><body></body></html>"
    )
    css_body = b".hero{background:url(/bg.jpg)}"
    client, repo, archive_url = _seed(session)
    fake_fetch.set(PAGE_URL, _result(PAGE_URL, html, headers={"Content-Type": "text/html"}))
    fake_fetch.set(css_url, _result(css_url, css_body, headers={"Content-Type": "text/css"}))
    fake_fetch.set(
        "https://acme.example.com/bg.jpg",
        _result(
            "https://acme.example.com/bg.jpg", b"JPEGDATA", headers={"Content-Type": "image/jpeg"}
        ),
    )
    calls = {"n": 0}
    original = capturemod.assetsmod.discover_css_assets

    def _spy(css_text, base_url):
        calls["n"] += 1
        return original(css_text, base_url)

    monkeypatch.setattr(capturemod.assetsmod, "discover_css_assets", _spy)

    _capture_one(
        session, repo, client, archive_url,
        settings=settings, artifacts_dir=artifacts_dir,
        now=datetime(2026, 1, 1, tzinfo=UTC), capture_assets=True,
    )

    assert calls["n"] == 1
    assert fake_fetch.calls[css_url] == 1


def test_backfill_css_assets_dry_run_reports_missing_without_fetching(
    session, artifacts_dir, settings, fake_fetch
):
    """task-60 §1.3 — a stored snapshot from BEFORE the discovery fix has
    an inline `<style>` background the old capture never found; the
    backfill's dry run must count it without writing anything.
    """
    client, repo, archive_url = _seed(session)
    html = (
        b'<html><head><title>Acme</title><style>'
        b'.hero{background-image:url("https://acme.example.com/old-bg.jpg")}'
        b"</style></head><body></body></html>"
    )
    fake_fetch.set(PAGE_URL, _result(PAGE_URL, html, headers={"Content-Type": "text/html"}))
    _capture_one(
        session, repo, client, archive_url,
        settings=settings, artifacts_dir=artifacts_dir,
        now=datetime(2026, 1, 1, tzinfo=UTC), capture_assets=False,
    )

    outcome = capturemod.backfill_css_assets(
        session, repo, client_id=None, settings=settings, artifacts_dir=artifacts_dir,
        dry_run=True, now=datetime(2026, 1, 2, tzinfo=UTC),
    )

    assert outcome.scanned_snapshots == 1
    assert outcome.missing_by_kind.get("img") == 1
    assert outcome.missing_total == 1
    assert repo.get_asset_by_key("acme.example.com/old-bg.jpg") is None  # nothing written
    assert "https://acme.example.com/old-bg.jpg" not in fake_fetch.calls


def test_backfill_css_assets_fetches_missing_and_registers_versions(
    session, artifacts_dir, settings, fake_fetch
):
    client, repo, archive_url = _seed(session)
    bg_url = "https://acme.example.com/old-bg.jpg"
    html = (
        b'<html><head><title>Acme</title><style>'
        b'.hero{background-image:url("https://acme.example.com/old-bg.jpg")}'
        b"</style></head><body></body></html>"
    )
    fake_fetch.set(PAGE_URL, _result(PAGE_URL, html, headers={"Content-Type": "text/html"}))
    _capture_one(
        session, repo, client, archive_url,
        settings=settings, artifacts_dir=artifacts_dir,
        now=datetime(2026, 1, 1, tzinfo=UTC), capture_assets=False,
    )
    fake_fetch.set(bg_url, _result(bg_url, b"JPEGDATA", headers={"Content-Type": "image/jpeg"}))

    outcome = capturemod.backfill_css_assets(
        session, repo, client_id=None, settings=settings, artifacts_dir=artifacts_dir,
        dry_run=False, now=datetime(2026, 1, 2, tzinfo=UTC),
    )
    session.commit()

    assert outcome.fetched == 1
    assert outcome.failed == 0
    asset = repo.get_asset_by_key("acme.example.com/old-bg.jpg")
    assert asset is not None
    assert repo.latest_asset_version(asset.id) is not None


def test_backfill_css_assets_scopes_to_one_client(session, artifacts_dir, settings, fake_fetch):
    client_a, repo, archive_url_a = _seed(session)
    client_b = ClientRepository(session).create(
        name="Beta", slug="beta", domain="beta.example.com", timezone="America/New_York"
    )
    session.commit()
    url_b = "https://beta.example.com/"
    archive_url_b = repo.get_or_create_url(client_b.id, url_b, "beta.example.com/", source="manual")
    session.commit()

    html_a = (
        b'<html><head><title>A</title><style>.h{background:url(https://acme.example.com/a.jpg)}'
        b"</style></head><body></body></html>"
    )
    html_b = (
        b'<html><head><title>B</title><style>.h{background:url(https://beta.example.com/b.jpg)}'
        b"</style></head><body></body></html>"
    )
    fake_fetch.set(PAGE_URL, _result(PAGE_URL, html_a, headers={"Content-Type": "text/html"}))
    fake_fetch.set(url_b, _result(url_b, html_b, headers={"Content-Type": "text/html"}))

    _capture_one(
        session, repo, client_a, archive_url_a,
        settings=settings, artifacts_dir=artifacts_dir,
        now=datetime(2026, 1, 1, tzinfo=UTC), capture_assets=False,
    )
    _capture_one(
        session, repo, client_b, archive_url_b,
        settings=settings, artifacts_dir=artifacts_dir,
        now=datetime(2026, 1, 1, tzinfo=UTC), capture_assets=False,
    )

    outcome = capturemod.backfill_css_assets(
        session, repo, client_id=client_a.id, settings=settings, artifacts_dir=artifacts_dir,
        dry_run=True, now=datetime(2026, 1, 2, tzinfo=UTC),
    )

    assert outcome.scanned_snapshots == 1  # only client A's own snapshot
    assert outcome.missing_total == 1
    assert "img" in outcome.missing_by_kind


def test_capture_writes_urls_total_before_visiting_any_url(
    session, artifacts_dir, settings, fake_fetch, write_controller, session_scope
):
    """The hub can only animate real progress against a denominator, and it
    has to exist from the first second — a 500-URL client is otherwise
    indistinguishable from a wedged one for the better part of an hour.
    """
    client, repo, archive_url = _seed(session)
    fake_fetch.set(PAGE_URL, _result(PAGE_URL, HTML_V1, headers={"Content-Type": "text/html"}))
    capture = repo.create_capture(
        client.id, slot_key="progress", scheduled_for=datetime(2026, 1, 1, tzinfo=UTC),
        trigger="manual",
    )
    repo.claim_capture(capture.id, owner="test")
    session.commit()

    capturemod.run_client_capture(
        session, client, capture_id=capture.id, settings=settings,
        artifacts_dir=artifacts_dir, write_controller=write_controller,
        session_scope=session_scope, now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    stored = repo.get_capture(capture.id)
    assert stored.urls_total == 1
    assert stored.urls_captured == 1


def test_run_client_capture_writes_a_per_url_event(
    session, artifacts_dir, settings, fake_fetch, write_controller, session_scope
):
    """task-62 §0.5/§2.4: before this, `run_client_capture` logged nothing
    anywhere — the only per-URL trace of a capture was an `ArchiveSnapshot`
    row, and only for URLs whose content actually changed.
    """
    client, repo, archive_url = _seed(session)
    fake_fetch.set(PAGE_URL, _result(PAGE_URL, HTML_V1, headers={"Content-Type": "text/html"}))
    capture = repo.create_capture(
        client.id, slot_key="manual:events", scheduled_for=datetime(2026, 1, 1, tzinfo=UTC),
        trigger="manual",
    )
    session.commit()

    capturemod.run_client_capture(
        session, client, capture_id=capture.id, settings=settings, artifacts_dir=artifacts_dir,
        write_controller=write_controller, session_scope=session_scope,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    events = repo.events_since(capture.id, 0)
    assert len(events) == 1
    assert events[0].seq == 1
    assert PAGE_URL in events[0].message
    assert "new" in events[0].message
    assert events[0].level == "debug"


def test_run_client_capture_only_url_id_captures_exactly_one(
    session, artifacts_dir, settings, fake_fetch, write_controller, session_scope
):
    """task-62 §1.1/§2.7: the per-page "Refresh now" button — one cell,
    seconds, none of the client's OTHER URLs touched.
    """
    url_a, url_b = "https://acme.example.com/a", "https://acme.example.com/b"
    client = _client(session)
    repo = WebArchiveRepository(session)
    archive_url_a = repo.get_or_create_url(client.id, url_a, "acme.example.com/a", source="manual")
    repo.get_or_create_url(client.id, url_b, "acme.example.com/b", source="manual")
    session.commit()
    fake_fetch.set(url_a, _result(url_a, HTML_V1, headers={"Content-Type": "text/html"}))
    fake_fetch.set(url_b, _result(url_b, HTML_V1, headers={"Content-Type": "text/html"}))
    capture = repo.create_capture(
        client.id, slot_key="manual:one-url", scheduled_for=datetime(2026, 1, 1, tzinfo=UTC),
        trigger="manual", scope="url", archive_url_id=archive_url_a.id,
    )
    session.commit()

    outcome = capturemod.run_client_capture(
        session, client, capture_id=capture.id, settings=settings, artifacts_dir=artifacts_dir,
        write_controller=write_controller, session_scope=session_scope,
        now=datetime(2026, 1, 1, tzinfo=UTC), only_url_id=archive_url_a.id,
    )

    assert outcome.urls_total == 1
    assert outcome.urls_new == 1
    assert url_a in fake_fetch.calls
    assert url_b not in fake_fetch.calls  # the client's other URL, untouched


def test_run_client_capture_stops_when_cancel_requested(
    session, artifacts_dir, settings, fake_fetch, write_controller, session_scope
):
    """Cooperative cancellation (task-62 §1.6) — checked once per URL via a
    FRESH session (`_make_cancel_checker`), not the capture's own long-lived
    one: this project's sessionmaker is `expire_on_commit=False`
    (`app/db/session.py`), so re-reading `cancel_requested` on the SAME
    session would keep returning the stale cached row and never see a flag
    set by a different session (here, simulating the API's cancel request).
    """
    url_a, url_b = "https://acme.example.com/a", "https://acme.example.com/b"
    client = _client(session)
    repo = WebArchiveRepository(session)
    repo.get_or_create_url(client.id, url_a, "acme.example.com/a", source="manual")
    repo.get_or_create_url(client.id, url_b, "acme.example.com/b", source="manual")
    session.commit()
    fake_fetch.set(url_a, _result(url_a, HTML_V1, headers={"Content-Type": "text/html"}))
    fake_fetch.set(url_b, _result(url_b, HTML_V1, headers={"Content-Type": "text/html"}))
    capture = repo.create_capture(
        client.id, slot_key="manual:cancel", scheduled_for=datetime(2026, 1, 1, tzinfo=UTC),
        trigger="manual",
    )
    session.commit()
    repo.claim_capture(capture.id, owner="test")
    session.commit()
    assert repo.request_cancel(capture.id) == "cancel_requested"
    session.commit()

    outcome = capturemod.run_client_capture(
        session, client, capture_id=capture.id, settings=settings, artifacts_dir=artifacts_dir,
        write_controller=write_controller, session_scope=session_scope,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert outcome.status == "cancelled"
    assert outcome.urls_captured == 0
    assert fake_fetch.calls == {}  # stopped before touching either URL


def test_run_client_capture_many_clients_in_parallel_never_locks_the_database(
    artifacts_dir, settings, fake_fetch, write_controller, session_scope
):
    """Regression, live 2026-09-05: the per-URL loop called `commit_with_retry`
    directly instead of `write_controller.submit`, so N clients capturing at
    once — each its own thread, each its own session, exactly
    `ArchiveScheduler._submit`'s real shape — put the exact pre-task-62 lock
    storm right back and crashed most of a live portfolio's captures with
    `database is locked` inside one tick. A single-session test cannot catch
    this class of bug at all; it needs genuine concurrent writers against a
    real file-backed engine, one per client, sharing one controller.
    """
    n_clients = 6
    urls_per_client = 20
    now = datetime(2026, 1, 1, tzinfo=UTC)
    jobs: list[tuple[object, str]] = []
    with session_scope() as setup_session:
        for i in range(n_clients):
            client = ClientRepository(setup_session).create(
                name=f"Client{i}", slug=f"client{i}", domain=f"client{i}.example.com",
                timezone="America/New_York",
            )
            repo = WebArchiveRepository(setup_session)
            for j in range(urls_per_client):
                url = f"https://client{i}.example.com/page-{j}"
                repo.get_or_create_url(
                    client.id, url, f"client{i}.example.com/page-{j}", source="manual"
                )
                fake_fetch.set(url, _result(url, HTML_V1, headers={"Content-Type": "text/html"}))
            capture = repo.create_capture(
                client.id, slot_key=f"manual:{i}", scheduled_for=now, trigger="manual"
            )
            setup_session.commit()
            jobs.append((client, capture.id))

    errors: list[BaseException] = []
    outcomes: dict[str, object] = {}

    def _worker(client, capture_id) -> None:
        try:
            with session_scope() as own_session:
                outcomes[capture_id] = capturemod.run_client_capture(
                    own_session, client, capture_id=capture_id, settings=settings,
                    artifacts_dir=artifacts_dir, write_controller=write_controller,
                    session_scope=session_scope, now=now,
                )
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(c, cid)) for c, cid in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == []
    assert len(outcomes) == n_clients
    for outcome in outcomes.values():
        assert outcome.status == "ok"
        assert outcome.urls_new == urls_per_client
