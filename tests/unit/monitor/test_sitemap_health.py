"""§3.4.1 integration: `sitemap_health_counts` + the aggregate change fires
even on a client's very first run (task-35 §5) — unlike every other change
type in this task.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.config import Settings
from app.db.repositories.clients import ClientRepository
from app.db.repositories.monitor import MonitorRepository
from app.monitor.runner import _sitemap_aggregate


def _seed_client(session) -> str:
    return ClientRepository(session).create(name="Acme", slug="acme", domain="acme.com").id


def _seed_page(repo, client_id, url, status, *, in_sitemap=True):
    page = repo.get_or_create_page(client_id, url)
    repo.upsert_page_state(
        page,
        status=status,
        final_url=url,
        hop_count=0,
        indexable=(status == 200),
        meta_robots=None,
        x_robots_tag=None,
        canonical=None,
        title=None,
        description=None,
        h1=None,
        word_count=1,
        analytics_ids=[],
        signals_sha256="s",
        text_sha256="t",
        raw_sha256="r",
        in_sitemap=in_sitemap,
        now=datetime.now(UTC),
    )
    return page


def test_sitemap_health_counts_classify_by_status(session):
    client_id = _seed_client(session)
    repo = MonitorRepository(session)
    _seed_page(repo, client_id, "https://acme.com/ok/", 200)
    _seed_page(repo, client_id, "https://acme.com/redir/", 301)
    _seed_page(repo, client_id, "https://acme.com/broken/", 404)
    _seed_page(repo, client_id, "https://acme.com/down/", 503)
    _seed_page(repo, client_id, "https://acme.com/not-in-sitemap/", 200, in_sitemap=False)
    session.commit()

    counts = repo.sitemap_health_counts(client_id)
    assert counts == {"total": 4, "ok": 1, "redirects": 1, "broken": 1, "unavailable": 1}


def test_exxxoticaexpo_like_ratio_produces_a_warning_summary_on_baseline(session):
    # Live regression (task-35 §1.1): 2 broken out of a sample must warn, and
    # must appear even though this is the client's very first run.
    client_id = _seed_client(session)
    repo = MonitorRepository(session)
    for i in range(38):
        _seed_page(repo, client_id, f"https://acme.com/ok-{i}/", 200)
    _seed_page(repo, client_id, "https://acme.com/gone-1/", 301)
    _seed_page(repo, client_id, "https://acme.com/gone-2/", 301)
    session.commit()

    run = repo.create_run(client_id, kind="full")
    session.commit()
    settings = Settings(_env_file=None)
    _sitemap_aggregate(
        repo,
        ClientRepository(session).get(client_id),
        run_id=run.id,
        settings=settings,
        now=datetime.now(UTC),
    )
    session.commit()

    changes = repo.list_changes(client_id, state="all")
    aggregate = next(c for c in changes if c.change_type == "sitemap_broken_urls")
    assert aggregate.severity == "warning"
    assert aggregate.details_json["broken_total"] == 2


def test_clean_sitemap_produces_no_aggregate_change(session):
    client_id = _seed_client(session)
    repo = MonitorRepository(session)
    for i in range(10):
        _seed_page(repo, client_id, f"https://acme.com/ok-{i}/", 200)
    session.commit()

    run = repo.create_run(client_id, kind="full")
    session.commit()
    settings = Settings(_env_file=None)
    _sitemap_aggregate(
        repo,
        ClientRepository(session).get(client_id),
        run_id=run.id,
        settings=settings,
        now=datetime.now(UTC),
    )
    session.commit()

    changes = repo.list_changes(client_id, state="all")
    assert not any(c.change_type == "sitemap_broken_urls" for c in changes)


def test_aggregate_self_heals_when_broken_urls_are_fixed(session):
    client_id = _seed_client(session)
    repo = MonitorRepository(session)
    for i in range(38):
        _seed_page(repo, client_id, f"https://acme.com/ok-{i}/", 200)
    _seed_page(repo, client_id, "https://acme.com/gone-1/", 301)
    _seed_page(repo, client_id, "https://acme.com/gone-2/", 301)
    session.commit()
    settings = Settings(_env_file=None)

    run1 = repo.create_run(client_id, kind="full")
    session.commit()
    _sitemap_aggregate(
        repo,
        ClientRepository(session).get(client_id),
        run_id=run1.id,
        settings=settings,
        now=datetime.now(UTC),
    )
    session.commit()
    assert repo.open_change(client_id, "sitemap_broken_urls", "acme.com/sitemap") is not None

    # Fix both broken URLs.
    _seed_page(repo, client_id, "https://acme.com/gone-1/", 200)
    _seed_page(repo, client_id, "https://acme.com/gone-2/", 200)
    session.commit()

    run2 = repo.create_run(client_id, kind="full")
    session.commit()
    _sitemap_aggregate(
        repo,
        ClientRepository(session).get(client_id),
        run_id=run2.id,
        settings=settings,
        now=datetime.now(UTC),
    )
    session.commit()

    assert repo.open_change(client_id, "sitemap_broken_urls", "acme.com/sitemap") is None
    resolved = [
        c
        for c in repo.list_changes(client_id, state="all")
        if c.change_type == "sitemap_broken_urls_resolved"
    ]
    assert len(resolved) == 1


def test_aggregate_escalates_from_warning_to_critical_in_place(session):
    """An already-open change whose severity gets worse must be re-recorded
    at the new severity, not silently stay at whichever level first tripped
    it (the dedup-by-open-change guard is not a severity freeze)."""
    client_id = _seed_client(session)
    repo = MonitorRepository(session)
    for i in range(38):
        _seed_page(repo, client_id, f"https://acme.com/ok-{i}/", 200)
    _seed_page(repo, client_id, "https://acme.com/gone-1/", 301)
    _seed_page(repo, client_id, "https://acme.com/gone-2/", 301)
    session.commit()
    settings = Settings(_env_file=None)

    run1 = repo.create_run(client_id, kind="full")
    session.commit()
    _sitemap_aggregate(
        repo,
        ClientRepository(session).get(client_id),
        run_id=run1.id,
        settings=settings,
        now=datetime.now(UTC),
    )
    session.commit()
    first = repo.open_change(client_id, "sitemap_broken_urls", "acme.com/sitemap")
    assert first is not None and first.severity == "warning"

    # Break far more of the sitemap — now well past the critical threshold.
    for i in range(30):
        _seed_page(repo, client_id, f"https://acme.com/ok-{i}/", 500)
    session.commit()

    run2 = repo.create_run(client_id, kind="full")
    session.commit()
    _sitemap_aggregate(
        repo,
        ClientRepository(session).get(client_id),
        run_id=run2.id,
        settings=settings,
        now=datetime.now(UTC),
    )
    session.commit()

    escalated = repo.open_change(client_id, "sitemap_broken_urls", "acme.com/sitemap")
    assert escalated is not None
    assert escalated.severity == "critical"
    assert escalated.id != first.id
    # The old warning-level row is resolved, not left dangling open.
    all_changes = repo.list_changes(client_id, state="all")
    stale = next(c for c in all_changes if c.id == first.id)
    assert stale.resolved_at is not None
