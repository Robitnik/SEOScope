"""API-level tests for the URL Inspector's server-backed address (task-88
follow-up) — `GET /url/<the url being inspected>`.

Like `test_archive_view_api.py`, this route hands back the SAME frontend
shell every other route serves, so what's tested here is only what is
specific to it: the address alone must resolve to a page, the inspected
url must survive the trip **including its own query string**, and the tab
title must never name a client the page itself wouldn't.
"""

from __future__ import annotations

import urllib.parse

from app.db.repositories.clients import ClientRepository

URL = "https://www.acme.com/locations/chicago/"


def _seed_client(db, *, slug: str = "acme", domain: str = "acme.com") -> str:
    client_id = ClientRepository(db).create(
        name="ACME", slug=slug, domain=domain, timezone="America/New_York"
    ).id
    db.commit()
    return client_id


def test_serves_the_frontend_app_shell(client, db):
    _seed_client(db)
    response = client.get(f"/url/{URL}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    # The SPA's own root element — the same shell as every other route,
    # never a bespoke server-rendered page (PLAN §12: one renderer per DTO).
    assert 'id="url-inspector-page-root"' in response.text


def test_title_names_the_resolved_client(client, db):
    _seed_client(db)
    response = client.get(f"/url/{URL}")

    assert "<title>/locations/chicago/ — ACME — URL Inspector</title>" in response.text


def test_title_degrades_when_no_client_matches_the_host(client, db):
    # Not an error: inspecting a URL nobody owns is legitimate, and the page
    # itself shows `unknown_client` honestly. The tab just says less.
    response = client.get("/url/https://nobody-here.example/x/")

    assert response.status_code == 200
    assert "<title>/x/ — URL Inspector</title>" in response.text


def test_an_unknown_url_still_returns_200_not_404(client, db):
    """Unlike the archive viewer, there is no "not captured" state to report
    — any URL is a legitimate thing to ask about."""
    response = client.get("/url/https://nobody-here.example/never-seen")

    assert response.status_code == 200
    assert 'id="url-inspector-page-root"' in response.text


def test_the_inspected_urls_own_query_string_survives(client, db):
    """The single rule this route lives by: a `?` on the incoming request
    belongs to the INSPECTED url, never to this route. LAXTON's
    `?taxonomy=…&term=…` pages are real, distinct pages — dropping the query
    would silently inspect a different one.
    """
    _seed_client(db, slug="laxton", domain="laxton.se")
    response = client.get("/url/https://laxton.se/a/?taxonomy=x&term=1")

    assert response.status_code == 200
    assert (
        '<link rel="canonical" href="/url/https://laxton.se/a/?taxonomy=x&amp;term=1">'
        in response.text
    )


def test_canonical_points_back_at_this_same_address(client, db):
    _seed_client(db)
    response = client.get(f"/url/{URL}")

    assert f'<link rel="canonical" href="/url/{URL}">' in response.text


def test_a_percent_encoded_url_lands_on_the_same_page(client, db):
    """Raw is the form this app writes, but a url that arrives encoded (a
    chat client that "helpfully" escaped it, an old link) must not become a
    different page."""
    _seed_client(db)
    encoded = urllib.parse.quote(URL, safe="")
    response = client.get(f"/url/{encoded}")

    assert response.status_code == 200
    assert "<title>/locations/chicago/ — ACME — URL Inspector</title>" in response.text


def test_a_malformed_url_does_not_500(client, db):
    """Garbage in the path is a bad link, not a server fault — the shell
    still renders and the page's own validation explains the problem."""
    response = client.get("/url/not-a-url-at-all")

    assert response.status_code == 200
    assert 'id="url-inspector-page-root"' in response.text


def test_the_shell_is_never_cached(client, db):
    """The title is personalized per URL, so a cached copy would show the
    previous page's client. The Cloudflare tunnel in front of this app is
    known to cache aggressively (session note: it cached .js/.css for 4h
    over the app's own headers), which is exactly why this is asserted."""
    _seed_client(db)
    response = client.get(f"/url/{URL}")

    assert "no-cache" in response.headers.get("cache-control", "")
