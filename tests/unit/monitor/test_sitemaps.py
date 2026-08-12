"""Sitemap parsing (task-35 §5): index -> children, `.gz`, cyclic references,
the safety-valve cap, and URL-set diffing.
"""

from __future__ import annotations

import gzip

from app.core.config import Settings
from app.monitor import runner as runner_mod
from app.monitor.fetcher import FetchResult, FetchStats
from app.monitor.sitemaps import diff_url_sets, maybe_decompress, parse_sitemap
from app.tools.http_safety import HostThrottle


def _urlset(*urls: str) -> bytes:
    entries = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>'.encode()


def _index(*children: str) -> bytes:
    entries = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in children)
    return f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</sitemapindex>'.encode()


def test_index_recurses_into_children(monkeypatch):
    documents = {
        "https://x.com/sitemap_index.xml": _index(
            "https://x.com/sitemap1.xml", "https://x.com/sitemap2.xml"
        ),
        "https://x.com/sitemap1.xml": _urlset("https://x.com/a/", "https://x.com/b/"),
        "https://x.com/sitemap2.xml": _urlset("https://x.com/c/"),
    }

    def fake_fetch(url, *, throttle, stats, timeout, max_body_bytes):
        stats.requests_made += 1
        return FetchResult(
            requested_url=url,
            final_url=url,
            status=200,
            hop_count=0,
            headers={"Content-Type": "application/xml"},
            body=documents[url],
            truncated_body=False,
            elapsed_ms=1,
            error=None,
        )

    monkeypatch.setattr(runner_mod, "fetch", fake_fetch)
    settings = Settings(_env_file=None)
    tree = runner_mod._fetch_sitemap_tree(
        ["https://x.com/sitemap_index.xml"],
        throttle=HostThrottle(),
        stats=FetchStats(),
        settings=settings,
    )
    assert set(tree) == set(documents)
    # `_fetch_sitemap_tree` returns `_SitemapFetch` (document + the decompressed
    # bytes + status), not the bare document: the bytes are what gets stored as
    # a version of the file.
    assert tree["https://x.com/sitemap_index.xml"].doc.kind == "index"
    assert tree["https://x.com/sitemap1.xml"].body == documents["https://x.com/sitemap1.xml"]
    assert {e.loc for e in tree["https://x.com/sitemap1.xml"].doc.entries} == {
        "https://x.com/a/",
        "https://x.com/b/",
    }


def test_cyclic_index_does_not_loop_forever(monkeypatch):
    # A points to B, B points back to A.
    documents = {
        "https://x.com/a.xml": _index("https://x.com/b.xml"),
        "https://x.com/b.xml": _index("https://x.com/a.xml"),
    }

    def fake_fetch(url, *, throttle, stats, timeout, max_body_bytes):
        stats.requests_made += 1
        return FetchResult(
            requested_url=url,
            final_url=url,
            status=200,
            hop_count=0,
            headers={},
            body=documents[url],
            truncated_body=False,
            elapsed_ms=1,
            error=None,
        )

    monkeypatch.setattr(runner_mod, "fetch", fake_fetch)
    settings = Settings(_env_file=None)
    tree = runner_mod._fetch_sitemap_tree(
        ["https://x.com/a.xml"], throttle=HostThrottle(), stats=FetchStats(), settings=settings
    )
    # Terminates, and visits each file exactly once.
    assert set(tree) == {"https://x.com/a.xml", "https://x.com/b.xml"}


def test_safety_valve_caps_the_number_of_sitemap_files_fetched(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(url, *, throttle, stats, timeout, max_body_bytes):
        calls["n"] += 1
        next_url = f"https://x.com/s{calls['n']}.xml"
        return FetchResult(
            requested_url=url,
            final_url=url,
            status=200,
            hop_count=0,
            headers={},
            body=_index(next_url),
            truncated_body=False,
            elapsed_ms=1,
            error=None,
        )

    monkeypatch.setattr(runner_mod, "fetch", fake_fetch)
    settings = Settings(_env_file=None)
    tree = runner_mod._fetch_sitemap_tree(
        ["https://x.com/s0.xml"],
        throttle=HostThrottle(),
        stats=FetchStats(),
        settings=settings,
        max_files=5,
    )
    assert len(tree) <= 5


def test_gz_sitemap_is_transparently_decompressed():
    raw = _urlset("https://x.com/a/")
    compressed = gzip.compress(raw)
    decompressed = maybe_decompress(compressed, url="https://x.com/sitemap.xml.gz")
    doc = parse_sitemap(decompressed)
    assert doc.kind == "urlset"
    assert len(doc.entries) == 1


def test_diff_url_sets_reports_added_and_removed():
    added, removed = diff_url_sets({"a", "b"}, {"b", "c"})
    assert added == {"c"}
    assert removed == {"a"}


def test_ring_traversal_prioritizes_key_pages_then_open_issues_then_oldest_checked(session):
    from datetime import UTC, datetime, timedelta

    from app.db.repositories.clients import ClientRepository
    from app.db.repositories.monitor import MonitorRepository

    client_id = ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com").id
    repo = MonitorRepository(session)
    now = datetime.now(UTC)

    old_page = repo.get_or_create_page(client_id, "https://acme.com/old/")
    repo.upsert_page_state(
        old_page,
        status=200,
        final_url=old_page.url,
        hop_count=0,
        indexable=True,
        meta_robots=None,
        x_robots_tag=None,
        canonical=None,
        title=None,
        description=None,
        h1=None,
        word_count=1,
        analytics_ids=[],
        signals_sha256="s1",
        text_sha256="t1",
        raw_sha256="r1",
        in_sitemap=True,
        now=now - timedelta(days=10),
    )
    recent_page = repo.get_or_create_page(client_id, "https://acme.com/recent/")
    repo.upsert_page_state(
        recent_page,
        status=200,
        final_url=recent_page.url,
        hop_count=0,
        indexable=True,
        meta_robots=None,
        x_robots_tag=None,
        canonical=None,
        title=None,
        description=None,
        h1=None,
        word_count=1,
        analytics_ids=[],
        signals_sha256="s2",
        text_sha256="t2",
        raw_sha256="r2",
        in_sitemap=True,
        now=now,
    )
    key_page = repo.get_or_create_page(client_id, "https://acme.com/", is_key_page=True)
    repo.upsert_page_state(
        key_page,
        status=200,
        final_url=key_page.url,
        hop_count=0,
        indexable=True,
        meta_robots=None,
        x_robots_tag=None,
        canonical=None,
        title=None,
        description=None,
        h1=None,
        word_count=1,
        analytics_ids=[],
        signals_sha256="s3",
        text_sha256="t3",
        raw_sha256="r3",
        in_sitemap=True,
        now=now,
    )
    session.commit()

    ordered = repo.pages_in_ring_order(client_id)
    # Key page always first; between the two non-key pages, oldest-checked first.
    assert ordered[0].url == key_page.url
    assert ordered[1].url == old_page.url
    assert ordered[2].url == recent_page.url
