"""Manual re-check of robots.txt / the sitemap tree (mirrors task-38 §2's
per-page check and task-51's redirects check): 404 for an unknown client,
runs synchronously against a mocked fetch, persists a resource, and writes a
`manual`-trigger run row.
"""

from __future__ import annotations

from app.db.repositories.clients import ClientRepository
from app.monitor import runner as runner_mod
from app.monitor.fetcher import FetchResult


def _create_client(db, *, slug: str = "acme") -> str:
    client = ClientRepository(db).create(
        name="Acme", slug=slug, domain=f"{slug}.com", timezone="America/New_York"
    )
    db.commit()
    return client.id


def _fake_fetch(documents: dict[str, bytes], *, content_type: str):
    def fetch(url, *, throttle, stats, timeout, max_body_bytes):
        stats.requests_made += 1
        body = documents.get(url)
        return FetchResult(
            requested_url=url,
            final_url=url,
            status=200 if body is not None else 404,
            hop_count=0,
            headers={"Content-Type": content_type},
            body=body,
            truncated_body=False,
            elapsed_ms=1,
            error=None,
        )

    return fetch


# --- robots --------------------------------------------------------------------


def test_check_robots_404_for_unknown_client(client):
    response = client.post("/api/v1/clients/nope/monitor/robots/check")
    assert response.status_code == 404


def test_check_robots_runs_synchronously_and_persists_the_resource(client, db, monkeypatch):
    client_id = _create_client(db)
    monkeypatch.setattr(
        runner_mod,
        "fetch",
        _fake_fetch(
            {"https://acme.com/robots.txt": b"User-agent: *\nDisallow: /admin/\n"},
            content_type="text/plain",
        ),
    )

    response = client.post(f"/api/v1/clients/{client_id}/monitor/robots/check")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["url"] == "https://acme.com/robots.txt"
    assert body[0]["status"] == 200

    runs = client.get(f"/api/v1/clients/{client_id}/monitor/runs").json()
    assert len(runs) == 1
    assert runs[0]["trigger"] == "manual"
    assert runs[0]["status"] == "succeeded"


# --- sitemap -------------------------------------------------------------------


def test_check_sitemap_404_for_unknown_client(client):
    response = client.post("/api/v1/clients/nope/monitor/sitemap/check")
    assert response.status_code == 404


def test_check_sitemap_defaults_to_sitemap_xml_without_a_known_robots_file(client, db, monkeypatch):
    client_id = _create_client(db)
    urlset = (
        b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://acme.com/</loc></url></urlset>"
    )
    monkeypatch.setattr(
        runner_mod,
        "fetch",
        _fake_fetch({"https://acme.com/sitemap.xml": urlset}, content_type="application/xml"),
    )

    response = client.post(f"/api/v1/clients/{client_id}/monitor/sitemap/check")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["url"] == "https://acme.com/sitemap.xml"
    assert body[0]["status"] == 200

    runs = client.get(f"/api/v1/clients/{client_id}/monitor/runs").json()
    assert len(runs) == 1
    assert runs[0]["trigger"] == "manual"


def test_check_sitemap_uses_the_url_declared_in_a_previously_stored_robots_file(
    client, db, monkeypatch
):
    client_id = _create_client(db)
    monkeypatch.setattr(
        runner_mod,
        "fetch",
        _fake_fetch(
            {"https://acme.com/robots.txt": b"Sitemap: https://acme.com/custom-sitemap.xml\n"},
            content_type="text/plain",
        ),
    )
    client.post(f"/api/v1/clients/{client_id}/monitor/robots/check")

    urlset = (
        b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://acme.com/a/</loc></url></urlset>"
    )
    monkeypatch.setattr(
        runner_mod,
        "fetch",
        _fake_fetch(
            {"https://acme.com/custom-sitemap.xml": urlset}, content_type="application/xml"
        ),
    )

    response = client.post(f"/api/v1/clients/{client_id}/monitor/sitemap/check")
    assert response.status_code == 200
    body = response.json()
    assert [r["url"] for r in body] == ["https://acme.com/custom-sitemap.xml"]
