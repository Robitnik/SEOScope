"""API-level tests for the replay route (task-59 §1.5/§1.6) — the single
most safety-critical surface in the archive: an unrewritten reference must
never silently fall through to the live internet, and every response must
carry the replay's own CSP (not the app-wide default).
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.config import get_settings
from app.db.repositories.clients import ClientRepository
from app.db.repositories.web_archive import WebArchiveRepository
from app.webarchive import storage
from app.webarchive.fingerprint import content_fingerprint
from app.webarchive.replay import format_ts
from app.webarchive.urls import normalize_url

PAGE_HTML = (
    b"<html><head><title>Acme</title></head><body>"
    b'<a href="/other">Other</a><img src="https://cdn.example.com/logo.png">'
    b"</body></html>"
)


def _seed_page(
    db,
    *,
    url: str = "https://acme.com/",
    body: bytes = PAGE_HTML,
    content_type: str = "text/html",
) -> tuple[str, str]:
    client = ClientRepository(db).create(
        name="Acme", slug="acme", domain="acme.com", timezone="America/New_York"
    )
    db.commit()
    settings = get_settings()
    blob = storage.store_blob(settings.artifacts_dir, body)
    repo = WebArchiveRepository(db)
    repo.register_blob(
        blob.sha256, size_bytes=blob.size_bytes, gzip_size_bytes=blob.gzip_size_bytes,
        content_type=content_type, path=blob.path,
    )
    archive_url = repo.get_or_create_url(client.id, url, normalize_url(url), source="manual")
    # `get_or_create_url` no longer flushes itself (task-65) — required
    # before another row's FK can reference this brand-new one in the same
    # transaction (task-63 follow-up, live-confirmed 2026-09-05).
    repo.flush()
    observed_at = datetime(2026, 9, 4, 13, 30, tzinfo=UTC)
    repo.create_snapshot(
        archive_url_id=archive_url.id,
        client_id=client.id,
        capture_id=None,
        observed_at=observed_at,
        http_status=200,
        final_url=url,
        headers_json={"Content-Type": content_type, "Set-Cookie": "session=leak-me"},
        content_type=content_type,
        content_fingerprint=content_fingerprint(body.decode("utf-8", errors="replace")),
        raw_sha256=blob.sha256,
        body_blob_sha256=blob.sha256,
        title="Acme",
        description=None,
        canonical=None,
        meta_robots=None,
        word_count=1,
        asset_count=0,
        assets_truncated=False,
        store_reason="first",
        prev_snapshot_id=None,
        last_confirmed_at=observed_at,
        confirmed_count=1,
    )
    db.commit()
    return format_ts(observed_at), url


def test_replay_page_rewrites_external_reference_and_sets_security_headers(client, db):
    ts, url = _seed_page(db)
    response = client.get(f"/archive/r/{ts}/{url}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert f"/archive/r/{ts}/https://cdn.example.com/logo.png" in response.text
    assert f"/archive/r/{ts}/https://acme.com/other" in response.text
    # The external host appears ONLY as the tail of our own replay path —
    # never as a bare, directly-fetchable reference (task-59 §1.6).
    idx = response.text.index("cdn.example.com")
    assert f"/archive/r/{ts}/" in response.text[max(0, idx - 60) : idx]

    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "cdn.example.com" not in csp
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["access-control-allow-origin"] == "*"
    # `Set-Cookie` must never survive from capture to replay (PLAN §13).
    assert "set-cookie" not in {k.lower() for k in response.headers.keys()}


def test_replay_injects_shim_script(client, db):
    ts, url = _seed_page(db)
    response = client.get(f"/archive/r/{ts}/{url}")
    assert "/archive/static/shim.js" in response.text
    assert f'data-ts="{ts}"' in response.text


def test_replay_miss_returns_404_and_records_it(client, db):
    # `sec-fetch-dest: iframe` — the request shape the replay VIEWER's own
    # sandboxed iframe actually sends (task-63 follow-up): a plain 404 with
    # this module's own CSP is exactly right there, unlike a bare top-level
    # hit (covered separately below), which redirects to a friendlier page.
    headers = {"sec-fetch-dest": "iframe"}
    response = client.get(
        "/archive/r/20260904133000/https://never-captured.example.com/", headers=headers
    )
    assert response.status_code == 404
    assert response.headers["content-security-policy"]  # still carries the replay CSP

    # A second miss on the SAME url increments the same row rather than
    # creating a duplicate (task-59 §1.6: our own honest list of gaps).
    # The write itself now runs as a `BackgroundTask` (task-63 follow-up —
    # a busy writer previously made this same request hang for the FULL
    # SQLite `busy_timeout`, ~30s, live-measured), which the in-process
    # TestClient transport still runs before `client.get` returns here.
    client.get(
        "/archive/r/20260904133000/https://never-captured.example.com/", headers=headers
    )

    from sqlalchemy import select

    from app.db.models import ArchiveReplayMiss

    rows = list(db.scalars(select(ArchiveReplayMiss)))
    assert len(rows) == 1
    assert rows[0].hit_count == 2


def test_replay_miss_on_a_top_level_navigation_redirects_to_the_hub(client, db):
    """Live-reported 2026-09-05: opening a rewritten in-page link in a new
    tab bypasses the SPA viewer entirely and lands on this raw backend
    route directly — a bare 404 with nothing to do about it. Redirects to
    the hub's own lookup instead, which already has the "Not archived
    (yet)" / "Archive this page now" UX this task built.
    """
    response = client.get(
        "/archive/r/20260904133000/https://never-captured.example.com/",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == (
        "/#/archive?url=https%3A%2F%2Fnever-captured.example.com%2F"
    )


def test_replay_page_miss_inside_iframe_is_a_styled_404_with_a_capture_button(client, db):
    """task-70: a page-shaped miss hit FROM the replay iframe itself (an
    in-page link to an uncaptured page) used to be a bare `text/plain`
    body with nothing to act on. It's still a plain 404 (never a redirect
    — that would escape the sandbox), but now a self-contained HTML page
    whose own button posts `__archiveMiss` to the parent for the viewer's
    chrome to react to.
    """
    headers = {"sec-fetch-dest": "iframe"}
    response = client.get(
        "/archive/r/20260904133000/https://never-captured.example.com/", headers=headers
    )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-security-policy"]
    assert "__archiveMiss" in response.text
    assert "archive-miss-capture" in response.text


def test_replay_miss_on_an_asset_like_url_never_redirects(client, db):
    """An image/css/js the rewriter genuinely lost stays a plain 404 even
    on a top-level hit — "archive this page" makes no sense for a
    sub-resource, and this exact route is what the viewer's OWN iframe
    fetches for every subresource, where a redirect would silently escape
    the replay sandbox.
    """
    response = client.get(
        "/archive/r/20260904133000/https://never-captured.example.com/style.css",
        follow_redirects=False,
    )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/plain")


def test_replay_resolves_nearest_snapshot_before_requested_time(client, db):
    ts, url = _seed_page(db)
    later_ts = format_ts(datetime(2026, 9, 4, 19, 30, tzinfo=UTC))
    response = client.get(f"/archive/r/{later_ts}/{url}")
    assert response.status_code == 200
    assert "Acme" in response.text


def test_shim_script_served_with_cors_header(client):
    response = client.get("/archive/static/shim.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["access-control-allow-origin"] == "*"
    assert "__archiveNav" in response.text


def test_replay_serves_binary_page_row_as_raw_asset(client, db):
    """Live incident (task-59, 2026-09-04): a gallery lightbox link got
    queued and captured as a "page" whose real content is an image. Even
    for an already-bad row like this, replay must serve the raw bytes with
    their real content-type — never run it through `rewrite_html` +
    shim-injection and label it `text/html`, which is what produced a
    broken `<img>` in the browser.
    """
    webp_bytes = b"RIFF\x00\x00\x00\x00WEBPVP8 not-a-real-page"
    ts, url = _seed_page(
        db, url="https://acme.com/gallery/photo-scaled.webp",
        body=webp_bytes, content_type="image/webp",
    )
    response = client.get(f"/archive/r/{ts}/{url}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.content == webp_bytes
    assert "/archive/static/shim.js" not in response.text


def test_replay_options_preflight_answered(client, db):
    ts, url = _seed_page(db)
    response = client.options(f"/archive/r/{ts}/{url}")
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "*"


# --- task-60 §2: iframes left live ------------------------------------------


def test_replay_csp_allows_https_iframes_by_default(client, db):
    """The ONE external-origin exception in the whole replay CSP (task-60
    §2) — everything else stays `'self'`-only.
    """
    ts, url = _seed_page(db)
    response = client.get(f"/archive/r/{ts}/{url}")
    csp = response.headers["content-security-policy"]
    assert "frame-src https:" in csp
    assert "default-src 'self'" in csp


def test_live_iframe_src_survives_replay_untouched(client, db):
    """A real map embed: the user's own decision (task-60 §2) — left
    pointing at its live host instead of being archived.
    """
    html = (
        b"<html><head><title>Acme</title></head><body>"
        b'<iframe src="https://www.google.com/maps/embed?pb=abc"></iframe>'
        b"</body></html>"
    )
    ts, url = _seed_page(db, body=html)
    response = client.get(f"/archive/r/{ts}/{url}")
    assert 'src="https://www.google.com/maps/embed?pb=abc"' in response.text
    assert "/archive/r/" not in response.text.split("<iframe")[1].split("</iframe>")[0]


def test_analytics_iframe_still_gets_rewritten_and_blocked_at_replay(client, db):
    """349 of the portfolio's iframes are GTM's own noscript pixel
    (task-60 plan §0.6/§2.2) — must never be left live, or every archived-
    snapshot view sends a real hit into the client's own GA account.
    """
    html = (
        b"<html><head><title>Acme</title></head><body>"
        b'<iframe src="https://www.googletagmanager.com/ns.html?id=GTM-X"></iframe>'
        b"</body></html>"
    )
    ts, url = _seed_page(db, body=html)
    response = client.get(f"/archive/r/{ts}/{url}")
    assert f"/archive/r/{ts}/https://www.googletagmanager.com/ns.html?id=GTM-X" in response.text


def test_shim_js_no_longer_patches_iframe_src_setter(client):
    """`HTMLIFrameElement` must be absent from the patched-setter list, and
    `setAttribute`'s own IFRAME exception must be present (task-60 §2.1) —
    otherwise a JS-driven lazy-load embed (41 of the portfolio's iframes
    use `data-lazy-src`, task-60 plan §0.6) still gets funneled through
    `/archive/r/...` and 404s.
    """
    response = client.get("/archive/static/shim.js")
    assert "[window.HTMLIFrameElement" not in response.text  # not in the patched-setter list
    assert 'this.tagName === "IFRAME"' in response.text


# --- task-63 §3.1: shim rewrites a reference to THIS APP's own origin -----


def test_replay_injects_self_origin_for_the_shim(client, db):
    """`toReplayUrl` (shim.js) needs to know THIS app's own origin to spot
    a page's own JS (e.g. LAB.js resolving its base off `location.href`)
    accidentally building a reference to it instead of the archived site.
    """
    ts, url = _seed_page(db)
    response = client.get(f"/archive/r/{ts}/{url}")
    assert 'data-self-origin="http://testserver"' in response.text


def test_shim_js_contains_self_origin_rewrite_branch(client) -> None:
    """The actual rewrite runs in a browser (verified end-to-end via
    Playwright); this is the same string-level contract check the rest of
    this file already uses for shim.js — the SELF_ORIGIN branch and its
    `/archive/` exemption must both be present in what gets served.
    """
    response = client.get("/archive/static/shim.js")
    assert "SELF_ORIGIN" in response.text
    assert 'rest.indexOf("/archive/") !== 0' in response.text


# --- task-63 §3.2: asset resolves despite a cache-buster query mismatch ---


def _seed_css_asset(db, *, url_key: str, css: bytes = b".a{color:red}") -> None:
    settings = get_settings()
    blob = storage.store_blob(settings.artifacts_dir, css)
    repo = WebArchiveRepository(db)
    repo.register_blob(
        blob.sha256, size_bytes=blob.size_bytes, gzip_size_bytes=blob.gzip_size_bytes,
        content_type="text/css", path=blob.path,
    )
    asset = repo.get_or_create_asset(
        f"https://acme.com/{url_key.split('/', 1)[1]}", url_key, kind="css"
    )
    now = datetime(2026, 9, 4, 13, 30, tzinfo=UTC)
    repo.create_asset_version(
        asset_id=asset.id, blob_sha256=blob.sha256, content_type="text/css",
        http_status=200, first_seen_at=now, last_confirmed_at=now,
    )
    db.commit()


def test_replay_resolves_asset_when_archived_with_no_query_at_all(client, db):
    ts, _url = _seed_page(db)
    _seed_css_asset(db, url_key="acme.com/style.css")

    response = client.get(f"/archive/r/{ts}/https://acme.com/style.css?v=1788604050059")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")


def test_replay_resolves_asset_archived_under_a_different_query(client, db):
    ts, _url = _seed_page(db)
    _seed_css_asset(db, url_key="acme.com/style.css?v=1788000000000")

    response = client.get(f"/archive/r/{ts}/https://acme.com/style.css?v=1788604050059")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")


def test_replay_asset_fallback_never_crosses_to_a_different_file(client, db):
    ts, _url = _seed_page(db)
    _seed_css_asset(db, url_key="acme.com/other.css")

    response = client.get(f"/archive/r/{ts}/https://acme.com/style.css?v=123")
    assert response.status_code == 404


# --- task-63 §3.4: a busy write-lock on the miss log must never 500 ------


def test_replay_miss_survives_a_locked_database(client, db, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def _boom(self, url, url_key, *, now=None):
        raise OperationalError("INSERT", {}, Exception("database is locked"))

    monkeypatch.setattr(WebArchiveRepository, "record_replay_miss", _boom)

    response = client.get(
        "/archive/r/20260904133000/https://never-captured.example.com/",
        headers={"sec-fetch-dest": "iframe"},
    )

    assert response.status_code == 404
    assert response.headers["content-security-policy"]
