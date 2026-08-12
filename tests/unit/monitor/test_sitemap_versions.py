"""Історія версій sitemap (task-35 §2 «зберігати всі версії»).

Жива регресія 2026-08-11: у базі лежало 72 рядки `monitor_resources` з
`kind="sitemap"` і **нуль** `monitor_resource_versions` — `_process_sitemaps`
оновлював стан і жодного разу не дописував версію (на відміну від
`_process_robots`). Кнопка History у блоці Sitemap була порожня завжди, і
жодну зміну sitemap не можна було продифити.
"""

from __future__ import annotations

import gzip
from datetime import UTC, datetime

from app.core.config import Settings
from app.db.repositories.clients import ClientRepository
from app.db.repositories.monitor import MonitorRepository
from app.monitor import runner as runner_mod
from app.monitor.fetcher import FetchResult, FetchStats
from app.monitor.robots import RobotsDocument
from app.tools.http_safety import HostThrottle

BASE = "https://acme.com"


def _urlset(*locs: str) -> bytes:
    body = "".join(f"<url><loc>{loc}</loc></url>" for loc in locs)
    return f'<?xml version="1.0"?><urlset>{body}</urlset>'.encode()


def _run_pass(session, client, documents, *, run_id, baseline, artifacts_dir, monkeypatch):
    def fake_fetch(url, *, throttle, stats, timeout, max_body_bytes):
        stats.requests_made += 1
        payload = documents.get(url)
        return FetchResult(
            requested_url=url,
            final_url=url,
            status=200 if payload is not None else 404,
            hop_count=0,
            headers={"Content-Type": "application/xml"},
            body=payload,
            truncated_body=False,
            elapsed_ms=1,
            error=None,
        )

    monkeypatch.setattr(runner_mod, "fetch", fake_fetch)
    return runner_mod._process_sitemaps(
        MonitorRepository(session),
        client,
        BASE,
        RobotsDocument(sitemaps=(f"{BASE}/sitemap.xml",)),
        run_id=run_id,
        throttle=HostThrottle(),
        stats=FetchStats(),
        settings=Settings(_env_file=None),
        artifacts_dir=artifacts_dir,
        is_baseline_run=baseline,
        now=datetime.now(UTC),
    )


def _versions(session, client_id):
    repo = MonitorRepository(session)
    out = []
    for resource in repo.list_resources(client_id, kind="sitemap"):
        out.extend(repo.resource_versions(resource.id, limit=50))
    return out


def test_sitemap_versions_are_recorded_and_only_on_change(session, tmp_path, monkeypatch):
    client = ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com")
    repo = MonitorRepository(session)
    art = tmp_path / "artifacts"

    # 1. Baseline — the file itself must be kept from the very first pass.
    docs = {f"{BASE}/sitemap.xml": _urlset(f"{BASE}/a/", f"{BASE}/b/")}
    run1 = repo.create_run(client.id, kind="full")
    urls = _run_pass(
        session,
        client,
        docs,
        run_id=run1.id,
        baseline=True,
        artifacts_dir=art,
        monkeypatch=monkeypatch,
    )
    assert set(urls) == {f"{BASE}/a/", f"{BASE}/b/"}
    versions = _versions(session, client.id)
    assert len(versions) == 1, "baseline must store the sitemap, not just its shape"
    assert versions[0].content_sha256, "the XML must land in the blob store"
    assert versions[0].structure_json["urls"] == [f"{BASE}/a/", f"{BASE}/b/"]
    assert versions[0].changed_fields_json == []  # nothing to compare against yet

    # 2. Unchanged pass — no second version for identical bytes.
    run2 = repo.create_run(client.id, kind="full")
    _run_pass(
        session,
        client,
        docs,
        run_id=run2.id,
        baseline=False,
        artifacts_dir=art,
        monkeypatch=monkeypatch,
    )
    assert len(_versions(session, client.id)) == 1

    # 3. A URL disappears — one new version, and the diff names what moved.
    docs = {f"{BASE}/sitemap.xml": _urlset(f"{BASE}/a/")}
    run3 = repo.create_run(client.id, kind="full")
    _run_pass(
        session,
        client,
        docs,
        run_id=run3.id,
        baseline=False,
        artifacts_dir=art,
        monkeypatch=monkeypatch,
    )
    versions = sorted(_versions(session, client.id), key=lambda v: v.observed_at)
    assert len(versions) == 2
    latest = versions[-1]
    assert latest.prev_version_id == versions[0].id, "the version chain must stay linked"
    assert set(latest.changed_fields_json) == {"entry_count", "urls"}
    assert latest.structure_json["urls"] == [f"{BASE}/a/"]


def test_gzipped_sitemap_hash_is_stable_across_passes(session, tmp_path, monkeypatch):
    """`.xml.gz` re-gzipped twice differs byte-for-byte (gzip embeds an mtime),
    so hashing the raw response would invent a new version on every run. The
    decompressed payload is what gets stored."""
    client = ClientRepository(session).create(name="Gz", slug="gz", domain="gz.com")
    repo = MonitorRepository(session)
    art = tmp_path / "artifacts"
    xml = _urlset(f"{BASE}/a/")

    for index, baseline in enumerate((True, False)):
        run = repo.create_run(client.id, kind="full")
        _run_pass(
            session,
            client,
            {f"{BASE}/sitemap.xml": gzip.compress(xml, mtime=index)},
            run_id=run.id,
            baseline=baseline,
            artifacts_dir=art,
            monkeypatch=monkeypatch,
        )

    assert len(_versions(session, client.id)) == 1
