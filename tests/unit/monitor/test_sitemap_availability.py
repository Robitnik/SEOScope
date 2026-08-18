"""`sitemap_lost` vs `sitemap_unavailable` (task-38 follow-up).

Live regression 2026-08-13/14 on hearwellservices.com: a `/sitemap.xml` that
plugin-redirects to the site's real sitemap had one flaky check (fetch-level
failure, never a confirmed 404) and the monitor reported `critical` "Sitemap
lost" — which "resolved" itself on the very next scheduled check, ~12h later.
`_process_sitemaps` must only ever emit `sitemap_lost` when the server itself
answered 404; every other failure to fetch/parse the file is `sitemap_unavailable`
(warning, self-healing), same principle as `dns_unavailable`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.config import Settings
from app.db.repositories.clients import ClientRepository
from app.db.repositories.monitor import MonitorRepository
from app.monitor import runner as runner_mod
from app.monitor.fetcher import FetchResult, FetchStats
from app.monitor.robots import RobotsDocument
from app.tools.http_safety import HostThrottle

BASE = "https://acme.com"
SITEMAP_URL = f"{BASE}/sitemap.xml"


def _urlset(*locs: str) -> bytes:
    body = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return f'<?xml version="1.0"?><urlset>{body}</urlset>'.encode()


def _run_pass(session, client, result_for_url, *, run_id, baseline, artifacts_dir, monkeypatch):
    def fake_fetch(url, *, throttle, stats, timeout, max_body_bytes):
        stats.requests_made += 1
        return result_for_url(url)

    monkeypatch.setattr(runner_mod, "fetch", fake_fetch)
    return runner_mod._process_sitemaps(
        MonitorRepository(session),
        client,
        BASE,
        RobotsDocument(sitemaps=(SITEMAP_URL,)),
        run_id=run_id,
        throttle=HostThrottle(),
        stats=FetchStats(),
        settings=Settings(_env_file=None),
        artifacts_dir=artifacts_dir,
        is_baseline_run=baseline,
        now=datetime.now(UTC),
    )


def _healthy(url):
    return FetchResult(
        requested_url=url,
        final_url=url,
        status=200,
        hop_count=0,
        headers={"Content-Type": "application/xml"},
        body=_urlset(f"{BASE}/a/"),
        truncated_body=False,
        elapsed_ms=1,
        error=None,
    )


def _confirmed_404(url):
    return FetchResult(
        requested_url=url,
        final_url=url,
        status=404,
        hop_count=0,
        headers={},
        body=b"",
        truncated_body=False,
        elapsed_ms=1,
        error=None,
    )


def _fetch_failed(url):
    """Timeout/connection-error shape: no status, no body — never a 404."""
    return FetchResult(
        requested_url=url,
        final_url=url,
        status=None,
        hop_count=0,
        headers={},
        body=None,
        truncated_body=False,
        elapsed_ms=1,
        error="timeout",
        error_detail="timed out after 20s",
    )


def _open_changes(session, client_id):
    return MonitorRepository(session).list_changes(client_id, state="open")


def test_confirmed_404_opens_sitemap_lost_critical(session, tmp_path, monkeypatch):
    client = ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com")
    art = tmp_path / "artifacts"
    repo = MonitorRepository(session)

    def run_pass(result_for_url, *, run_id, baseline):
        return _run_pass(
            session, client, result_for_url, run_id=run_id, baseline=baseline,
            artifacts_dir=art, monkeypatch=monkeypatch,
        )

    run_pass(_healthy, run_id=repo.create_run(client.id, kind="full").id, baseline=True)
    run_pass(_confirmed_404, run_id=repo.create_run(client.id, kind="full").id, baseline=False)

    changes = _open_changes(session, client.id)
    assert [c.change_type for c in changes] == ["sitemap_lost"]
    assert changes[0].severity == "critical"
    resource = repo.get_resource(client.id, SITEMAP_URL)
    assert changes[0].related_id == resource.id


def test_fetch_failure_opens_sitemap_unavailable_warning_not_lost(session, tmp_path, monkeypatch):
    client = ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com")
    art = tmp_path / "artifacts"
    repo = MonitorRepository(session)

    def run_pass(result_for_url, *, run_id, baseline):
        return _run_pass(
            session, client, result_for_url, run_id=run_id, baseline=baseline,
            artifacts_dir=art, monkeypatch=monkeypatch,
        )

    run_pass(_healthy, run_id=repo.create_run(client.id, kind="full").id, baseline=True)
    run_pass(_fetch_failed, run_id=repo.create_run(client.id, kind="full").id, baseline=False)

    changes = _open_changes(session, client.id)
    assert [c.change_type for c in changes] == ["sitemap_unavailable"]
    assert changes[0].severity == "warning"


def test_transient_fetch_failure_self_heals_on_next_healthy_check(session, tmp_path, monkeypatch):
    client = ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com")
    art = tmp_path / "artifacts"
    repo = MonitorRepository(session)

    def run_pass(result_for_url, *, run_id, baseline):
        return _run_pass(
            session, client, result_for_url, run_id=run_id, baseline=baseline,
            artifacts_dir=art, monkeypatch=monkeypatch,
        )

    run_pass(_healthy, run_id=repo.create_run(client.id, kind="full").id, baseline=True)
    run_pass(_fetch_failed, run_id=repo.create_run(client.id, kind="full").id, baseline=False)
    assert [c.change_type for c in _open_changes(session, client.id)] == ["sitemap_unavailable"]

    run_pass(_healthy, run_id=repo.create_run(client.id, kind="full").id, baseline=False)

    # The `_resolved` marker itself stays visible (it's a historical fact,
    # not an ongoing condition — same as `dns_unavailable_resolved`/
    # `sitemap_lost_resolved` elsewhere), but neither `sitemap_lost` nor
    # `sitemap_unavailable` may still be open.
    open_types = {c.change_type for c in _open_changes(session, client.id)}
    assert open_types.isdisjoint({"sitemap_lost", "sitemap_unavailable"})
    resolved = [
        c
        for c in repo.list_changes(client.id, state="all")
        if c.change_type == "sitemap_unavailable_resolved"
    ]
    assert len(resolved) == 1
    assert resolved[0].severity == "info"
    assert resolved[0].severity == "info"
