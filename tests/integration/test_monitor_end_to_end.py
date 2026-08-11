"""Site Monitor end-to-end (task-35 §5): four sequential runs against a fake
client site, driving the real `run_client_monitor` orchestration — real DB,
real diff/severity/fingerprint/storage logic. Only the network boundary
(`fetch`/DNS/TLS) is faked.

Why not a literal `http.server` bound to 127.0.0.1, as the task plan
suggests: `app.tools.http_safety.assert_public_http_url` (the same SSRF guard
`app/monitor/fetcher.py` reuses) refuses loopback/private addresses by
design (PLAN §13) — a real local listener would never pass it. The project's
own precedent for testing HTTP-touching code under this guard is dependency
injection at the fetch boundary (`app.tools.redirects.trace.trace_redirects`
takes a `fetcher=` callable); this test does the same thing one layer up,
monkeypatching `app.monitor.runner.fetch`/`dns_mod.resolve`/`tls_mod.check_certificate`
so every fetch is served from an in-memory fake site instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.db.repositories.clients import ClientRepository
from app.db.repositories.monitor import MonitorRepository
from app.monitor import dns as dns_mod
from app.monitor import runner as runner_mod
from app.monitor import tls as tls_mod
from app.monitor.fetcher import FetchResult
from app.monitor.runner import run_client_monitor
from app.monitor.tls import TlsCertInfo

pytestmark = pytest.mark.integration

BASE = "https://fakeclient.test"
N_CONTENT_PAGES = 18


class FakeSite:
    """Mutable in-memory "site" the fake fetch/DNS layer serves from."""

    def __init__(self) -> None:
        self.robots = f"User-agent: *\n\nSitemap: {BASE}/sitemap.xml\n"
        self.homepage_noindex = False
        self.txt_verification = "google-site-verification=abc123"
        self.content_urls = [f"{BASE}/page-{i}/" for i in range(N_CONTENT_PAGES)]
        self.removed_from_sitemap: set[str] = set()
        self.broken_urls: dict[str, int] = {}
        # One URL is broken from the very first run (task-35 §5 baseline scenario).
        self.broken_urls[self.content_urls[0]] = 404

    def sitemap_urls(self) -> list[str]:
        return [f"{BASE}/"] + [u for u in self.content_urls if u not in self.removed_from_sitemap]

    def page_html(self, url: str) -> str:
        if url == f"{BASE}/":
            robots_meta = '<meta name="robots" content="noindex">' if self.homepage_noindex else ""
            return (
                f"<html><head><title>Home</title>{robots_meta}"
                "<script>gtag('config', 'G-FAKE12345');</script></head>"
                "<body><h1>Welcome</h1><p>Fake client homepage.</p></body></html>"
            )
        return (
            f"<html><head><title>{url}</title></head><body><p>Content for {url}</p></body></html>"
        )

    def status_for(self, url: str) -> int:
        return self.broken_urls.get(url, 200)


def _make_fake_fetch(site: FakeSite):
    def fake_fetch(url, *, throttle, stats, timeout, max_body_bytes):
        stats.requests_made += 1
        if url == f"{BASE}/robots.txt":
            body = site.robots.encode()
        elif url == f"{BASE}/sitemap.xml":
            entries = "".join(f"<url><loc>{u}</loc></url>" for u in site.sitemap_urls())
            body = (
                '<?xml version="1.0"?><urlset '
                'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                f"{entries}</urlset>"
            ).encode()
        else:
            status = site.status_for(url)
            if status >= 400:
                stats.bytes_downloaded += 9
                return FetchResult(
                    requested_url=url,
                    final_url=url,
                    status=status,
                    hop_count=0,
                    headers={},
                    body=b"not found",
                    truncated_body=False,
                    elapsed_ms=1,
                    error=None,
                )
            body = site.page_html(url).encode()
        stats.bytes_downloaded += len(body)
        return FetchResult(
            requested_url=url,
            final_url=url,
            status=200,
            hop_count=0,
            headers={"Content-Type": "text/html; charset=utf-8"},
            body=body,
            truncated_body=False,
            elapsed_ms=1,
            error=None,
        )

    return fake_fetch


def _make_fake_resolve(site: FakeSite):
    def fake_resolve(name, record_type, *, resolvers=(), timeout=10.0):
        values: tuple[str, ...] = ()
        if record_type == "A":
            values = ("203.0.113.10",)
        elif record_type == "TXT" and not name.startswith("www."):
            values = (site.txt_verification,) if site.txt_verification else ()
        return dns_mod.DnsQueryResult(
            record_type=record_type, values=values, ttl=300, resolver="fake", available=True
        )

    return fake_resolve


def _fake_check_certificate(host: str, **kwargs) -> TlsCertInfo:
    return TlsCertInfo(
        not_after=datetime.now(UTC) + timedelta(days=300),
        issuer="Fake CA",
        subject_alt_names=(host,),
        error=None,
    )


@pytest.fixture
def fake_site(monkeypatch) -> FakeSite:
    site = FakeSite()
    monkeypatch.setattr(runner_mod, "fetch", _make_fake_fetch(site))
    monkeypatch.setattr(dns_mod, "resolve", _make_fake_resolve(site))
    monkeypatch.setattr(tls_mod, "check_certificate", _fake_check_certificate)
    return site


def _run_full(session, client, artifacts_dir):
    settings = Settings(_env_file=None)
    return run_client_monitor(
        session,
        client,
        kind="full",
        settings=settings,
        artifacts_dir=artifacts_dir,
        trigger="manual",
    )


def test_four_phase_lifecycle(temp_db, fake_site):
    with temp_db.session_scope() as session:
        client = ClientRepository(session).create(
            name="Fake Client", slug="fakeclient", domain="fakeclient.test"
        )
        session.commit()
        repo = MonitorRepository(session)

        # --- phase 1: baseline ----------------------------------------------
        outcome1 = _run_full(session, client, temp_db.artifacts_dir)
        assert outcome1.status == "succeeded"

        assert repo.list_changes(client.id, state="all", severity="critical") == []
        assert repo.list_changes(
            client.id, state="all", severity="warning", change_type="sitemap_broken_urls"
        )
        health1 = repo.sitemap_health_counts(client.id)
        assert health1["total"] == N_CONTENT_PAGES + 1  # + homepage
        assert health1["broken"] == 1

        all_pages = repo.all_pages(client.id)
        for page in [p for p in all_pages if p.status == 200]:
            version = repo.latest_page_version(page.id)
            assert version is not None
            assert version.body_blob_sha256 is not None, "baseline must store every page body"

        # --- phase 2: noindex + verification lost + one URL drops out -------
        fake_site.homepage_noindex = True
        fake_site.txt_verification = ""
        dropped = fake_site.content_urls[1]
        fake_site.removed_from_sitemap.add(dropped)

        outcome2 = _run_full(session, client, temp_db.artifacts_dir)
        assert outcome2.status == "succeeded"

        open_critical = repo.list_changes(client.id, state="open", severity="critical")
        critical_types = {c.change_type for c in open_critical}
        assert "page_noindex_appeared" in critical_types
        assert "gsc_verification_lost" in critical_types
        assert len(open_critical) == 2

        dropped_page = repo.get_page(client.id, dropped)
        assert dropped_page.in_sitemap is False
        assert dropped_page.gone_since is not None

        # --- phase 3: 30% more of the sitemap breaks -------------------------
        for url in fake_site.content_urls[2:8]:  # six more, on top of the original one
            fake_site.broken_urls[url] = 404

        outcome3 = _run_full(session, client, temp_db.artifacts_dir)
        assert outcome3.status == "succeeded"

        aggregate = repo.open_change(client.id, "sitemap_broken_urls", "fakeclient.test/sitemap")
        assert aggregate is not None
        assert aggregate.severity == "critical"
        all_aggregates = [
            c
            for c in repo.list_changes(client.id, state="all")
            if c.change_type == "sitemap_broken_urls"
        ]
        # The original `warning` (now resolved) + the escalated `critical` —
        # escalation replaces the open row, it does not stack a second one.
        assert len(all_aggregates) == 2
        assert sum(1 for c in all_aggregates if c.resolved_at is None) == 1

        per_url_broken = [
            c
            for c in repo.list_changes(client.id, state="open")
            if c.change_type in {"sitemap_url_broken", "sitemap_url_unavailable"}
        ]
        assert len(per_url_broken) == 7  # one per broken URL, not one giant blob

        # --- phase 4: everything restored -------------------------------------
        fake_site.homepage_noindex = False
        fake_site.txt_verification = "google-site-verification=abc123"
        fake_site.removed_from_sitemap.clear()
        fake_site.broken_urls.clear()

        outcome4 = _run_full(session, client, temp_db.artifacts_dir)
        assert outcome4.status == "succeeded"

        # "resolved_at everywhere" (task-35 §5) means every problem this test
        # introduced is closed — not that the feed is empty: `_resolved` info
        # markers and fresh `content_changed`/`dns_txt_changed` facts from the
        # restore itself are legitimate history, not conditions to resolve.
        assert repo.open_counts_by_severity(client.id)["critical"] == 0
        open_changes = repo.list_changes(client.id, state="open")
        open_types = {c.change_type for c in open_changes}
        assert open_types.isdisjoint(
            {
                "page_noindex_appeared",
                "gsc_verification_lost",
                "sitemap_broken_urls",
                "sitemap_url_broken",
                "sitemap_url_unavailable",
                "site_down",
            }
        )
        assert repo.sitemap_health_counts(client.id) == {
            "total": N_CONTENT_PAGES + 1,
            "ok": N_CONTENT_PAGES + 1,
            "redirects": 0,
            "broken": 0,
            "unavailable": 0,
        }

        # A page whose content never changed across all four runs gained no
        # new version on this final pass (task-35 DoD: unchanged pages create
        # no new version/blob between two full runs).
        stable_page = repo.get_page(client.id, f"{BASE}/page-10/")
        assert stable_page.version_seq == 1


def test_light_run_then_first_full_run_does_not_flag_dns_as_changed(temp_db, fake_site):
    """A client whose first-ever run is `light` never touches DNS (`kind ==
    LIGHT` skips `_process_dns` entirely); its first real DNS observation
    lands later, on a `full` run where `is_baseline_run` is already False.
    Without a per-record "is this row new" check, that first sighting reads
    as a change from "nothing" and fires a false `dns_*_changed` critical
    (live-measured 2026-08-11 on exxxoticaexpo.com's real first full run).
    """
    with temp_db.session_scope() as session:
        client = ClientRepository(session).create(
            name="Light First Co", slug="light-first-co", domain="fakeclient.test"
        )
        session.commit()
        repo = MonitorRepository(session)
        settings = Settings(_env_file=None)

        light_outcome = run_client_monitor(
            session,
            client,
            kind="light",
            settings=settings,
            artifacts_dir=temp_db.artifacts_dir,
            trigger="manual",
        )
        assert light_outcome.status == "succeeded"
        assert repo.all_dns_records(client.id) == []

        full_outcome = _run_full(session, client, temp_db.artifacts_dir)
        assert full_outcome.status == "succeeded"

        open_critical = repo.list_changes(client.id, state="open", severity="critical")
        dns_criticals = [c for c in open_critical if c.change_type.startswith("dns_")]
        assert dns_criticals == []
        assert repo.all_dns_records(client.id) != []


def test_light_run_checks_key_pages_plus_open_criticals_but_not_every_info_marker(
    temp_db, fake_site
):
    """Light runs must stay small (task-35 §3.3: "~30-40 requests/client"):
    homepage + top-GSC pages + pages with an open *critical*. Filtering
    `list_changes(state="open")` by `scope == "page"` alone is not enough —
    without a severity filter it also matches `info`-level history markers
    (`page_new`, ...) that never resolve, so once a client is past its true
    baseline (`diff_page` only short-circuits to `[]` while
    `is_baseline_run` is set) every page seen for the first time afterwards
    stays "open" forever and a light run would balloon towards the whole
    portfolio (live-measured 2026-08-11: every real client's light run
    landed at ~96-101 checked URLs — `list_changes`'s default `limit=100`
    — instead of the handful its own key-page set implies). Mirrors the
    real client shape: a `light` run is the client's true baseline (it only
    ever touches the homepage), so the `full` run right after it is *not*
    a baseline and sees its other 17 pages for the first time.
    """
    with temp_db.session_scope() as session:
        client = ClientRepository(session).create(
            name="Small Light Co", slug="small-light-co", domain="fakeclient.test"
        )
        session.commit()
        repo = MonitorRepository(session)
        settings = Settings(_env_file=None)

        baseline = run_client_monitor(
            session,
            client,
            kind="light",
            settings=settings,
            artifacts_dir=temp_db.artifacts_dir,
            trigger="manual",
        )
        assert baseline.status == "succeeded"

        full_outcome = _run_full(session, client, temp_db.artifacts_dir)
        assert full_outcome.status == "succeeded"
        # Every non-homepage page is seen for the first time on this
        # (non-baseline) full run, each leaving an open `page_new` info
        # change behind — exactly the noise the fix must not sweep up.
        page_new_open = [
            c
            for c in repo.list_changes(client.id, state="open", limit=1000)
            if c.change_type == "page_new"
        ]
        # `content_urls` holds the 18 non-homepage pages; the homepage was
        # already seen during the light baseline, so all 18 are new here.
        assert len(page_new_open) == N_CONTENT_PAGES

        non_key_url = fake_site.content_urls[3]
        repo.record_change(
            client.id,
            run_id=full_outcome.run_id,
            severity="critical",
            change_type="page_noindex_appeared",
            scope="page",
            subject=non_key_url,
            old_value="indexable",
            new_value="noindex",
        )
        session.commit()
        before = repo.get_page(client.id, non_key_url)
        assert before is not None
        checked_before = before.last_checked_at

        light_outcome = run_client_monitor(
            session,
            client,
            kind="light",
            settings=settings,
            artifacts_dir=temp_db.artifacts_dir,
            trigger="manual",
        )
        assert light_outcome.status == "succeeded"

        # Home page only (no GSC top-pages data in this test) + the one
        # page with an open critical — not the other 17 pages sitting on
        # an open `page_new` info marker.
        assert light_outcome.urls_checked == 2

        after = repo.get_page(client.id, non_key_url)
        assert after is not None
        assert after.last_checked_at != checked_before, (
            "the page with an open critical must be re-checked every light run"
        )


def test_second_run_of_an_unchanged_site_creates_no_new_versions_or_blobs(temp_db, fake_site):
    with temp_db.session_scope() as session:
        client = ClientRepository(session).create(
            name="Stable Co", slug="stable-co", domain="fakeclient.test"
        )
        session.commit()
        repo = MonitorRepository(session)

        _run_full(session, client, temp_db.artifacts_dir)
        versions_1 = sum(len(repo.page_versions(p.id)) for p in repo.all_pages(client.id))
        blobs_1 = len(list(temp_db.artifacts_dir.rglob("*.gz")))

        _run_full(session, client, temp_db.artifacts_dir)
        versions_2 = sum(len(repo.page_versions(p.id)) for p in repo.all_pages(client.id))
        blobs_2 = len(list(temp_db.artifacts_dir.rglob("*.gz")))

        assert versions_2 == versions_1
        assert blobs_2 == blobs_1


def test_run_counters_are_per_run(temp_db, fake_site):
    """task-37 F4: `changes_critical/warning/info` on the run row must
    reflect what THIS run found, not the client's current open-count total
    (the old bug: a 1-URL light run and a 609-URL full run reported the
    exact same numbers, because both read `open_counts_by_severity`)."""
    with temp_db.session_scope() as session:
        client = ClientRepository(session).create(
            name="Per Run Co", slug="per-run-co", domain="fakeclient.test"
        )
        session.commit()
        repo = MonitorRepository(session)
        settings = Settings(_env_file=None)

        # Baseline light run: only the homepage is in scope, and baseline
        # diffing is suppressed — this run finds nothing.
        light_outcome = run_client_monitor(
            session,
            client,
            kind="light",
            settings=settings,
            artifacts_dir=temp_db.artifacts_dir,
            trigger="manual",
        )
        assert light_outcome.status == "succeeded"
        light_run = repo.get_run(light_outcome.run_id)
        assert (light_run.changes_critical, light_run.changes_warning, light_run.changes_info) == (
            0,
            0,
            0,
        )

        # Non-baseline full run right after: every content page is new
        # (`page_new`, info) and the one pre-broken URL trips the sitemap
        # aggregate (warning) — real, non-zero, run-specific numbers.
        full_outcome = _run_full(session, client, temp_db.artifacts_dir)
        assert full_outcome.status == "succeeded"
        full_run = repo.get_run(full_outcome.run_id)
        assert full_run.changes_info > 0
        assert (
            full_run.changes_critical,
            full_run.changes_warning,
            full_run.changes_info,
        ) != (
            light_run.changes_critical,
            light_run.changes_warning,
            light_run.changes_info,
        )

        # And the stored counters equal exactly what `monitor_changes` holds
        # for that specific `run_id` — not a coincidence of the numbers
        # merely differing.
        actual_light = repo.count_changes_by_severity(light_outcome.run_id)
        actual_full = repo.count_changes_by_severity(full_outcome.run_id)
        assert (light_run.changes_critical, light_run.changes_warning, light_run.changes_info) == (
            actual_light["critical"],
            actual_light["warning"],
            actual_light["info"],
        )
        assert (full_run.changes_critical, full_run.changes_warning, full_run.changes_info) == (
            actual_full["critical"],
            actual_full["warning"],
            actual_full["info"],
        )


def test_urls_total_set_before_loop(temp_db, fake_site):
    """task-37 F2: `urls_total` is the real target-list size, set once
    before the crawl loop starts — a true `completed/total` model instead of
    an animated made-up percentage (frontend/AGENTS.md §9)."""
    with temp_db.session_scope() as session:
        client = ClientRepository(session).create(
            name="Total Co", slug="total-co", domain="fakeclient.test"
        )
        session.commit()
        repo = MonitorRepository(session)

        outcome = _run_full(session, client, temp_db.artifacts_dir)
        assert outcome.status == "succeeded"
        run = repo.get_run(outcome.run_id)
        assert run.urls_total == N_CONTENT_PAGES + 1  # + homepage
        assert run.urls_total == outcome.urls_checked
