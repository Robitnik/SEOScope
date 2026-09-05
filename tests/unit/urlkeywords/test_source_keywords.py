"""`app.urlinspector.sources.keywords.collect_keywords` — the two-block
keywords panel: on-page terms from the last crawled body, and site-level
GSC queries matched by full text coverage (task-64 §3)."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import Client, DimensionKind, MonitorPage, MonitorPageVersion, SyncJob
from app.db.repositories.dimensions import DimensionRepository, DimensionRowInput
from app.db.repositories.monitor import MonitorRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.monitor.storage import store_blob
from app.schemas.enums import ComparisonMode, PeriodKind, Provider, Quality, SourceMode
from app.urlinspector.sources.keywords import collect_keywords

URL = "https://acme.com/hearing-aids"

HTML = (
    b"<html><head><title>Hearing Aids | Acme</title></head>"
    b"<body><h1>Hearing aid fitting</h1>"
    b"<p>Hearing aid cleaning and hearing aid repair services in NYC. "
    b"Hearing aid fitting done by our audiologists.</p></body></html>"
)


def _seed_client(session) -> Client:
    client = Client(name="Acme", slug="acme", domain="acme.com")
    session.add(client)
    session.commit()
    return client


def _seed_page_with_body(session, client_id, tmp_path, *, html: bytes = HTML) -> MonitorPage:
    page = MonitorPage(
        client_id=client_id,
        url=URL,
        title="Hearing Aids | Acme",
        h1="Hearing aid fitting",
        status=200,
    )
    session.add(page)
    session.flush()

    stored = store_blob(tmp_path, html)
    MonitorRepository(session).record_blob(
        stored.sha256,
        size_bytes=stored.size_bytes,
        gzip_size_bytes=stored.gzip_size_bytes,
        mime="text/html",
        path=stored.path,
    )
    version = MonitorPageVersion(
        page_id=page.id, client_id=client_id, http_status=200, body_blob_sha256=stored.sha256
    )
    session.add(version)
    session.commit()
    return page


def _seed_query_rows(session, client_id, rows: list[tuple[str, int, int]]) -> None:
    job = SyncJob(
        client_id=client_id,
        period_kind=PeriodKind.month,
        comparison_mode=ComparisonMode.closed,
        anchor_date=date(2026, 8, 31),
    )
    session.add(job)
    session.commit()
    snapshot = SnapshotRepository(session).create_immutable(
        job_id=job.id,
        client_id=client_id,
        provider=Provider.gsc,
        source_mode=SourceMode.browser,
        observed_at=utcnow(),
        quality=Quality.complete,
    )
    DimensionRepository(session).bulk_insert(
        snapshot_id=snapshot.id,
        client_id=client_id,
        dimension=DimensionKind.query,
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        rows=[
            DimensionRowInput(
                dimension_value=value,
                clicks=clicks,
                impressions=impressions,
                ctr=None,
                position=None,
                quality=Quality.complete,
                rank=1,
            )
            for value, clicks, impressions in rows
        ],
    )
    session.commit()


def _patched_settings(tmp_path):
    settings = Settings(
        _env_file=None, database_url="sqlite:///:memory:", artifacts_dir=tmp_path
    )
    return patch("app.urlinspector.sources.keywords.get_settings", return_value=settings)


class TestNoMonitorPage:
    def test_untracked_url_is_no_data_on_both_blocks(self, session):
        client = _seed_client(session)
        dto = collect_keywords(session, client_id=client.id, url=URL, meta_keywords=[])
        assert dto.state.state == "no_data"
        assert dto.queries_state.state == "no_data"
        assert dto.on_page == []
        assert dto.site_queries == []


class TestOnPageExtraction:
    def test_page_with_no_stored_body_is_no_data(self, session):
        client = _seed_client(session)
        session.add(MonitorPage(client_id=client.id, url=URL, status=200))
        session.commit()
        dto = collect_keywords(session, client_id=client.id, url=URL, meta_keywords=[])
        assert dto.state.state == "no_data"

    def test_stored_body_yields_ranked_terms(self, session, tmp_path):
        client = _seed_client(session)
        _seed_page_with_body(session, client.id, tmp_path)
        with _patched_settings(tmp_path):
            dto = collect_keywords(session, client_id=client.id, url=URL, meta_keywords=["hearing"])
        assert dto.state.state == "has_data"
        assert dto.meta_keywords == ["hearing"]
        top = dto.on_page[0]
        assert top.term == "hearing"
        assert top.in_title is True
        assert top.in_h1 is True


class TestSiteQueryMatching:
    def test_no_query_rows_is_no_data(self, session, tmp_path):
        client = _seed_client(session)
        _seed_page_with_body(session, client.id, tmp_path)
        with _patched_settings(tmp_path):
            dto = collect_keywords(session, client_id=client.id, url=URL, meta_keywords=[])
        assert dto.queries_state.state == "no_data"

    def test_matching_query_surfaces_with_clicks_and_impressions(self, session, tmp_path):
        client = _seed_client(session)
        _seed_page_with_body(session, client.id, tmp_path)
        _seed_query_rows(
            session,
            client.id,
            [
                ("hearing aid cleaning", 12, 340),
                ("hearing aid price nyc", 3, 90),  # "price" not on the page -> excluded
            ],
        )
        with _patched_settings(tmp_path):
            dto = collect_keywords(session, client_id=client.id, url=URL, meta_keywords=[])
        assert dto.queries_state.state == "has_data"
        assert [q.query for q in dto.site_queries] == ["hearing aid cleaning"]
        assert dto.site_queries[0].clicks == 12
        assert dto.site_queries[0].impressions == 340

    def test_other_sentinel_row_is_never_matched(self, session, tmp_path):
        from app.db.models import DIMENSION_OTHER_VALUE

        client = _seed_client(session)
        _seed_page_with_body(session, client.id, tmp_path)
        _seed_query_rows(session, client.id, [(DIMENSION_OTHER_VALUE, 99, 999)])
        with _patched_settings(tmp_path):
            dto = collect_keywords(session, client_id=client.id, url=URL, meta_keywords=[])
        assert dto.site_queries == []
        assert dto.queries_state.state == "no_data"
