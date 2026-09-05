"""Antibot-block handling (task-50 §A/§B) — a blocked fetch must never be
recorded as a real page observation (PLAN §12: absence of data != a value),
and the run must aggregate every blocked URL into one `monitor_blocked`
change rather than one row per URL (the LIVE 2026-08-19 alphastyle.store
defect this task fixes: 304 of 355 pages recorded as real content).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.db.repositories.clients import ClientRepository
from app.db.repositories.monitor import MonitorRepository
from app.monitor import runner as runner_mod
from app.monitor.fetcher import FetchResult, classify_block
from app.monitor.runner import effective_fetch_mode, run_client_monitor

BASE = "https://acme.com"


def _ok(body: bytes = b"<html><head><title>Hi</title></head><body>hi</body></html>") -> FetchResult:
    return FetchResult(
        requested_url=BASE,
        final_url=BASE,
        status=200,
        hop_count=0,
        headers={"Content-Type": "text/html"},
        body=body,
        truncated_body=False,
        elapsed_ms=1,
        error=None,
    )


def _blocked() -> FetchResult:
    return FetchResult(
        requested_url=BASE,
        final_url=BASE,
        status=429,
        hop_count=0,
        headers={},
        body=b"<title>Verifying your connection...</title>",
        truncated_body=False,
        elapsed_ms=1,
        error="blocked",
        retry_after_seconds=360.0,
    )


def _fake_fetch(*, robots="", sitemap_urls, blocked_urls):
    """Robots/sitemap always answer clean; page fetches for `blocked_urls`
    come back blocked, everything else succeeds — mirrors the live incident
    where robots.txt/sitemap.xml answered fine and only the bulk of PAGE
    fetches hit the wall (task-50 §0.6).
    """

    def fetch(url, *, throttle, stats, timeout, max_body_bytes):
        stats.requests_made += 1
        if url == f"{BASE}/robots.txt":
            return FetchResult(
                requested_url=url,
                final_url=url,
                status=200,
                hop_count=0,
                headers={},
                body=robots.encode(),
                truncated_body=False,
                elapsed_ms=1,
                error=None,
            )
        if url == f"{BASE}/sitemap.xml":
            entries = "".join(f"<url><loc>{u}</loc></url>" for u in sitemap_urls)
            body = (
                '<?xml version="1.0"?><urlset '
                'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                f"{entries}</urlset>"
            ).encode()
            return FetchResult(
                requested_url=url,
                final_url=url,
                status=200,
                hop_count=0,
                headers={},
                body=body,
                truncated_body=False,
                elapsed_ms=1,
                error=None,
            )
        if url in blocked_urls:
            result = _blocked()
            return FetchResult(**{**result.__dict__, "requested_url": url, "final_url": url})
        result = _ok()
        return FetchResult(**{**result.__dict__, "requested_url": url, "final_url": url})

    return fetch


def test_a_blocked_page_never_creates_a_page_row(session, tmp_path, monkeypatch):
    client = ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com")
    session.commit()
    repo = MonitorRepository(session)

    # `_key_pages` always includes the homepage regardless of the sitemap
    # (task-35 §1.5), so it must also be blocked for every checked URL this
    # run to actually be blocked.
    page_url = f"{BASE}/blocked-page/"
    monkeypatch.setattr(
        runner_mod,
        "fetch",
        _fake_fetch(sitemap_urls=[page_url], blocked_urls={page_url, f"{BASE}/"}),
    )

    outcome = run_client_monitor(
        session,
        client,
        kind="full",
        settings=Settings(_env_file=None),
        artifacts_dir=tmp_path / "artifacts",
        trigger="manual",
    )

    assert (
        repo.get_page(client.id, page_url) is None
    ), "a URL never actually observed must not get a page row"
    assert outcome.status == "failed"  # every page fetched this run was blocked
    run = repo.get_run(outcome.run_id)
    assert run.error_code == "blocked_by_protection"
    assert run.notes_json["blocked_count"] == 2


def test_a_blocked_refetch_never_touches_the_existing_page_state(session, tmp_path, monkeypatch):
    client = ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com")
    session.commit()
    repo = MonitorRepository(session)
    page_url = f"{BASE}/"

    # Run 1: clean baseline — real title/status recorded.
    monkeypatch.setattr(
        runner_mod, "fetch", _fake_fetch(sitemap_urls=[page_url], blocked_urls=set())
    )
    run_client_monitor(
        session,
        client,
        kind="full",
        settings=Settings(_env_file=None),
        artifacts_dir=tmp_path / "artifacts",
        trigger="manual",
    )
    before = repo.get_page(client.id, page_url)
    assert before.status == 200
    assert before.title == "Hi"
    failures_before = before.consecutive_failures

    # Run 2: same page now blocked — state must be untouched, not corrupted
    # into "title disappeared" the way the live incident recorded it.
    monkeypatch.setattr(
        runner_mod, "fetch", _fake_fetch(sitemap_urls=[page_url], blocked_urls={page_url})
    )
    run_client_monitor(
        session,
        client,
        kind="full",
        settings=Settings(_env_file=None),
        artifacts_dir=tmp_path / "artifacts",
        trigger="manual",
    )
    after = repo.get_page(client.id, page_url)
    assert after.status == 200
    assert after.title == "Hi"
    assert after.consecutive_failures == failures_before
    assert len(repo.page_versions(after.id, limit=10)) == 1, "no new version from a blocked fetch"


def test_blocked_pages_produce_one_aggregate_change_not_one_per_page(
    session, tmp_path, monkeypatch
):
    client = ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com")
    session.commit()
    repo = MonitorRepository(session)
    urls = [f"{BASE}/p{i}/" for i in range(5)]
    blocked = set(urls) | {f"{BASE}/"}  # + homepage: always a key page (task-35 §1.5)

    # Streak-abort is exercised by its own test below — raise the threshold
    # here so all 6 targets actually get processed.
    settings = Settings(_env_file=None, monitor_block_streak_abort=100)
    monkeypatch.setattr(runner_mod, "fetch", _fake_fetch(sitemap_urls=urls, blocked_urls=blocked))
    run_client_monitor(
        session,
        client,
        kind="full",
        settings=settings,
        artifacts_dir=tmp_path / "artifacts",
        trigger="manual",
    )

    changes = repo.list_changes(client.id, state="open", limit=100)
    blocked_changes = [c for c in changes if c.change_type == "monitor_blocked"]
    assert len(blocked_changes) == 1
    assert blocked_changes[0].severity == "warning"
    assert blocked_changes[0].scope == "site"
    assert blocked_changes[0].details_json["blocked_count"] == 6
    # None of the per-page sitemap-status noise the live incident produced.
    assert not [c for c in changes if c.change_type.startswith("sitemap_url_")]


def test_monitor_blocked_self_heals_on_a_clean_run(session, tmp_path, monkeypatch):
    client = ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com")
    session.commit()
    repo = MonitorRepository(session)
    url = f"{BASE}/p/"

    monkeypatch.setattr(runner_mod, "fetch", _fake_fetch(sitemap_urls=[url], blocked_urls={url}))
    run_client_monitor(
        session,
        client,
        kind="full",
        settings=Settings(_env_file=None),
        artifacts_dir=tmp_path / "artifacts",
        trigger="manual",
    )
    assert repo.open_change(client.id, "monitor_blocked", f"{BASE}/") is not None

    monkeypatch.setattr(runner_mod, "fetch", _fake_fetch(sitemap_urls=[url], blocked_urls=set()))
    run_client_monitor(
        session,
        client,
        kind="full",
        settings=Settings(_env_file=None),
        artifacts_dir=tmp_path / "artifacts",
        trigger="manual",
    )
    assert repo.open_change(client.id, "monitor_blocked", f"{BASE}/") is None
    resolved = [
        c
        for c in repo.list_changes(client.id, state="all", limit=100)
        if c.change_type == "monitor_blocked_resolved"
    ]
    assert len(resolved) == 1


def test_a_block_streak_aborts_the_run_early(session, tmp_path, monkeypatch):
    """task-58 §2.2: a sliding-window RATIO replaced the old exact
    `consecutive_blocked` streak count — "consecutive" stopped meaning
    anything once several page workers can each finish a page at once. With
    every target blocked, the ratio window (20 results, 80% threshold by
    default) fills with nothing but blocks and the run must still stop well
    short of the full 51 targets — just not at an exact count, since a few
    workers can each have one more URL already in flight when the shared
    stop signal lands.
    """
    client = ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com")
    session.commit()
    urls = [f"{BASE}/p{i}/" for i in range(50)]
    blocked = set(urls) | {f"{BASE}/"}  # + homepage: always a key page (task-35 §1.5)

    settings = Settings(_env_file=None, monitor_block_ratio_abort=0.8)
    monkeypatch.setattr(runner_mod, "fetch", _fake_fetch(sitemap_urls=urls, blocked_urls=blocked))
    outcome = run_client_monitor(
        session,
        client,
        kind="full",
        settings=settings,
        artifacts_dir=tmp_path / "artifacts",
        trigger="manual",
    )

    assert outcome.truncated is True
    assert outcome.urls_checked < 51, "must stop well short of the full target list"


def test_a_partially_blocked_run_still_reports_the_real_data_it_gathered(
    session, tmp_path, monkeypatch
):
    client = ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com")
    session.commit()
    repo = MonitorRepository(session)
    ok_urls = [f"{BASE}/ok-{i}/" for i in range(3)]
    blocked_urls = {f"{BASE}/blocked-{i}/" for i in range(2)}

    settings = Settings(_env_file=None)
    monkeypatch.setattr(
        runner_mod,
        "fetch",
        _fake_fetch(sitemap_urls=ok_urls + list(blocked_urls), blocked_urls=blocked_urls),
    )
    outcome = run_client_monitor(
        session,
        client,
        kind="full",
        settings=settings,
        artifacts_dir=tmp_path / "artifacts",
        trigger="manual",
    )

    assert outcome.status == "succeeded"
    run = repo.get_run(outcome.run_id)
    assert run.error_code == "partially_blocked"
    assert run.notes_json["blocked_count"] == 2
    for url in ok_urls:
        assert repo.get_page(client.id, url) is not None


def test_classify_block_matches_the_live_cloudflare_shopify_interstitial():
    """The exact body the live 429 incident stored — none of the original
    seven markers matched it (task-50 §0.3); this is the regression guard.
    """
    from app.monitor.fetcher import Headers

    body = (
        b"<title>Verifying your connection...</title>"
        b'<div class="cf-injected-html"><div class="main">'
        b"window._cf_chl_opt = {};"
    )
    error, retry_after = classify_block(429, Headers(), body)
    assert error == "blocked"

    error, retry_after = classify_block(403, Headers({"Retry-After": "360"}), b"anything")
    assert error == "blocked"
    assert retry_after == 360.0

    error, _ = classify_block(200, Headers(), b"<html>a normal page</html>")
    assert error is None


def test_effective_fetch_mode_resolution():
    """task-58 §1.3: escalation moved from a whole-run, time-sticky choice
    (task-50 §B4/§B5) to a per-PAGE decision remade inside `run_client_
    monitor` on every run — `effective_fetch_mode` no longer has anything to
    resolve beyond a straight passthrough of the operator's own setting.
    `blocked_detected_at` no longer feeds back into this at all (still
    written, for visibility only — see `test_auto_mode_escalates_settings_
    after_a_run_confirms_a_block` below).
    """
    from app.db.models import MonitorSettings

    now = datetime.now(UTC)
    assert effective_fetch_mode(None, now=now) == "auto"
    assert effective_fetch_mode(MonitorSettings(fetch_mode="http"), now=now) == "http"
    assert effective_fetch_mode(MonitorSettings(fetch_mode="browser"), now=now) == "browser"
    assert effective_fetch_mode(MonitorSettings(fetch_mode="auto"), now=now) == "auto"

    # A recent OR a stale block leaves the resolution exactly the same now —
    # no more sticky window to fall in or out of.
    recently_blocked = MonitorSettings(
        fetch_mode="auto", blocked_detected_at=now - timedelta(days=2)
    )
    assert effective_fetch_mode(recently_blocked, now=now) == "auto"
    stale_block = MonitorSettings(fetch_mode="auto", blocked_detected_at=now - timedelta(days=20))
    assert effective_fetch_mode(stale_block, now=now) == "auto"


def test_auto_mode_escalates_settings_after_a_run_confirms_a_block(session, tmp_path, monkeypatch):
    client = ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com")
    session.commit()
    repo = MonitorRepository(session)
    url = f"{BASE}/p/"
    repo.update_settings(client.id, fetch_mode="auto")
    session.commit()

    monkeypatch.setattr(runner_mod, "fetch", _fake_fetch(sitemap_urls=[url], blocked_urls={url}))
    run_client_monitor(
        session,
        client,
        kind="full",
        settings=Settings(_env_file=None),
        artifacts_dir=tmp_path / "artifacts",
        trigger="manual",
    )

    settings_row = repo.get_settings(client.id)
    assert settings_row.blocked_detected_at is not None


def test_explicit_http_mode_never_auto_escalates(session, tmp_path, monkeypatch):
    client = ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com")
    session.commit()
    repo = MonitorRepository(session)
    url = f"{BASE}/p/"
    repo.update_settings(client.id, fetch_mode="http")
    session.commit()

    monkeypatch.setattr(runner_mod, "fetch", _fake_fetch(sitemap_urls=[url], blocked_urls={url}))
    run_client_monitor(
        session,
        client,
        kind="full",
        settings=Settings(_env_file=None),
        artifacts_dir=tmp_path / "artifacts",
        trigger="manual",
    )

    settings_row = repo.get_settings(client.id)
    assert (
        settings_row.blocked_detected_at is None
    ), "an explicit operator override (§B6) must never be silently overridden"
