"""`app.pageinventory.inventory.build_inventory` — canonical dedup and the
`None`/`""` translation (task-57 §1.5/§1.6)."""

from __future__ import annotations

from app.db.repositories.clients import ClientRepository
from app.db.repositories.monitor import MonitorRepository
from app.monitor.storage import store_blob
from app.pageinventory.inventory import build_inventory

_HTML_WITH_BREADCRUMBS = b"""
<script type="application/ld+json">
{"@type": "BreadcrumbList", "itemListElement": [
  {"@type": "ListItem", "position": 1, "name": "Home"},
  {"@type": "ListItem", "position": 2, "name": "Shop"}
]}
</script>
"""


def _client(session, *, slug: str = "acme") -> str:
    client = ClientRepository(session).create(name="Acme", slug=slug, domain=f"{slug}.com")
    session.commit()
    return client.id


def _checked_page(
    session, client_id, url, artifacts_dir, *, canonical=None, title=None, description=None,
    status=200, html=None, indexable=True, meta_robots=None,
):
    repo = MonitorRepository(session)
    page = repo.get_or_create_page(client_id, url)
    repo.upsert_page_state(
        page, status=status, final_url=url, hop_count=0, indexable=indexable,
        meta_robots=meta_robots, x_robots_tag=None, canonical=canonical,
        title=title, description=description, h1=None, word_count=None,
        analytics_ids=[], signals_sha256="s", text_sha256="t", raw_sha256="r",
        in_sitemap=True,
    )
    blob_sha = None
    if html is not None:
        stored = store_blob(artifacts_dir, html)
        repo.record_blob(
            stored.sha256, size_bytes=stored.size_bytes,
            gzip_size_bytes=stored.gzip_size_bytes, mime="text/html", path=stored.path,
        )
        blob_sha = stored.sha256
    repo.create_page_version(
        page, http_status=status, signals={}, signals_sha256="s", text_sha256="t",
        raw_sha256="r", body_blob_sha256=blob_sha, changed_fields=[], prev_version_id=None,
    )
    session.commit()
    return page


def test_dedup_by_canonical_keeps_one_row_and_lists_duplicates(session, tmp_path):
    client_id = _client(session)
    _checked_page(session, client_id, "https://acme.com/faq/", tmp_path, canonical="https://acme.com/faq/")
    _checked_page(
        session, client_id, "https://acme.com/pages/faq", tmp_path,
        canonical="https://acme.com/faq/",
    )

    result = build_inventory(session, client_id, artifacts_dir=tmp_path)

    assert result.pages_total == 2
    assert result.pages_unique == 1
    row = result.rows[0]
    assert row.url == "https://acme.com/faq/"
    assert row.duplicate_urls == ("https://acme.com/pages/faq",)


def test_missing_canonical_flags_row_and_uses_own_url_as_key(session, tmp_path):
    client_id = _client(session)
    _checked_page(session, client_id, "https://acme.com/orphan/", tmp_path, canonical=None)

    result = build_inventory(session, client_id, artifacts_dir=tmp_path)

    assert result.pages_unique == 1
    assert result.rows[0].canonical_missing is True
    assert result.rows[0].canonical_key == "https://acme.com/orphan"


def test_checked_page_with_absent_meta_description_is_empty_string_not_none(session, tmp_path):
    """task-57 §1.6: on a CHECKED live page, a tag the crawler didn't find
    is a real defect (`""`), never the "not applicable" `None` a hand-authored
    strategy document would use for the same absence."""
    client_id = _client(session)
    _checked_page(session, client_id, "https://acme.com/", tmp_path, description=None)

    result = build_inventory(session, client_id, artifacts_dir=tmp_path)

    row = result.rows[0]
    assert row.checked is True
    assert row.description == ""
    assert row.description is not None


def test_never_checked_page_stays_none_not_empty_string(session, tmp_path):
    client_id = _client(session)
    repo = MonitorRepository(session)
    repo.get_or_create_page(client_id, "https://acme.com/new/")
    session.commit()

    result = build_inventory(session, client_id, artifacts_dir=tmp_path)

    row = result.rows[0]
    assert row.checked is False
    assert row.status_label == "not_checked"
    assert row.title is None
    assert row.description is None


def test_breadcrumbs_extracted_from_stored_blob(session, tmp_path):
    client_id = _client(session)
    _checked_page(
        session, client_id, "https://acme.com/shop/", tmp_path, html=_HTML_WITH_BREADCRUMBS
    )

    result = build_inventory(session, client_id, artifacts_dir=tmp_path)

    row = result.rows[0]
    assert row.breadcrumb_source == "json_ld"
    assert row.page_label == "Shop"
    assert result.pages_with_breadcrumbs == 1


def test_no_stored_body_falls_back_to_url_derived_label_not_home(session, tmp_path):
    """task-57 live bug (2026-09-04, client GOREPORT): a page whose body was
    never stored (0 blobs for that client) rendered as "Home" on EVERY row,
    because an empty breadcrumb trail and a real ["Home"] trail were both
    treated the same way. `page_label` must never fall back to a literal
    "Home" unless the URL's own path is genuinely the root."""
    client_id = _client(session)
    _checked_page(
        session, client_id, "https://acme.com/10-key-risks-identified-in-roof-surveys/",
        tmp_path, html=None,  # no blob stored — matches the live gap exactly
    )

    result = build_inventory(session, client_id, artifacts_dir=tmp_path)

    row = result.rows[0]
    assert row.breadcrumb_source == "none"
    assert row.page_label == "10 Key Risks Identified In Roof Surveys"
    assert row.page_label != "Home"


def test_no_stored_body_on_the_actual_homepage_still_says_home(session, tmp_path):
    client_id = _client(session)
    _checked_page(session, client_id, "https://acme.com/", tmp_path, html=None)

    result = build_inventory(session, client_id, artifacts_dir=tmp_path)

    assert result.rows[0].page_label == "Home"


def test_stored_body_with_no_breadcrumb_markup_falls_back_to_url_label(session, tmp_path):
    """Extraction runs successfully (a body IS on disk) but finds nothing —
    same "empty trail" outcome as the no-blob case, so it must take the
    same URL-derived fallback, not `page_label()`'s bare "Home" default."""
    client_id = _client(session)
    _checked_page(
        session, client_id, "https://acme.com/about-us/", tmp_path,
        html=b"<html><body><p>No breadcrumbs on this page at all.</p></body></html>",
    )

    result = build_inventory(session, client_id, artifacts_dir=tmp_path)

    row = result.rows[0]
    assert row.breadcrumb_source == "none"
    assert row.page_label == "About Us"


def test_label_from_url_helper() -> None:
    from app.pageinventory.inventory import _label_from_url
    assert _label_from_url("https://acme.com/about-us/") == "About Us"
    assert _label_from_url("https://acme.com/") == "Home"
    assert _label_from_url("https://acme.com") == "Home"


def test_status_label_classification(session, tmp_path):
    client_id = _client(session)
    _checked_page(session, client_id, "https://acme.com/live/", tmp_path, status=200)
    _checked_page(session, client_id, "https://acme.com/gone/", tmp_path, status=404)
    _checked_page(session, client_id, "https://acme.com/moved/", tmp_path, status=301)
    _checked_page(
        session, client_id, "https://acme.com/hidden/", tmp_path, status=200, indexable=False
    )

    result = build_inventory(session, client_id, artifacts_dir=tmp_path)
    labels = {row.url: row.status_label for row in result.rows}

    assert labels["https://acme.com/live/"] == "live"
    assert labels["https://acme.com/gone/"] == "broken"
    assert labels["https://acme.com/moved/"] == "redirect"
    assert labels["https://acme.com/hidden/"] == "noindex"
