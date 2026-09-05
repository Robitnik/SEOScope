"""`app.urlinspector.service.inspect_url` — an exception in ONE source must
become that source's own `failed` state, never a 500 hiding every other
panel (task-61 §2.6, the read-path analogue of PLAN §9.1 cell isolation)."""

from __future__ import annotations

from unittest.mock import patch

from app.db.models import Client
from app.schemas.url_inspector import UrlSourceStateDTO
from app.urlinspector.service import inspect_url


def _seed_client(session) -> Client:
    client = Client(name="Acme", slug="acme", domain="acme.com")
    session.add(client)
    session.commit()
    return client


class TestSourceIsolation:
    def test_one_broken_source_does_not_break_the_others(self, session):
        _seed_client(session)

        with patch(
            "app.urlinspector.service.collect_gsc", side_effect=RuntimeError("boom")
        ):
            dto = inspect_url(session, url="https://www.acme.com/a", granularity="week")

        by_source = {c.source: c for c in dto.coverage}
        assert by_source["gsc"].state == "failed"
        assert "boom" in (by_source["gsc"].detail or "")
        # Every other panel still resolved to a real (non-exceptional) state —
        # here "not_configured"/"no_data" since the fixture DB has no rows,
        # but never "failed".
        for source in (
            "reporting_ninja",
            "monitor",
            "archive",
            "page_inventory",
            "tech_audit",
            "keywords",
            "site_queries",
            "screenshots",
        ):
            assert by_source[source].state != "failed"

    def test_all_sources_broken_still_returns_a_dto(self, session):
        _seed_client(session)

        patches = [
            patch("app.urlinspector.service.collect_gsc", side_effect=RuntimeError("a")),
            patch("app.urlinspector.service.collect_ga4", side_effect=RuntimeError("b")),
            patch("app.urlinspector.service.collect_archive", side_effect=RuntimeError("c")),
            patch("app.urlinspector.service.collect_monitor", side_effect=RuntimeError("d")),
            patch("app.urlinspector.service.collect_meta", side_effect=RuntimeError("e")),
            patch("app.urlinspector.service.collect_tech_audit", side_effect=RuntimeError("f")),
            patch("app.urlinspector.service.collect_keywords", side_effect=RuntimeError("g")),
            patch("app.urlinspector.service.collect_screenshots", side_effect=RuntimeError("h")),
        ]
        for p in patches:
            p.start()
        try:
            dto = inspect_url(session, url="https://www.acme.com/a", granularity="week")
        finally:
            for p in patches:
                p.stop()

        assert all(c.state == "failed" for c in dto.coverage)
        assert dto.client is not None  # resolution itself never goes through a source try/except

    def test_unknown_domain_short_circuits_before_any_source_runs(self, session):
        with patch(
            "app.urlinspector.service.collect_gsc", side_effect=AssertionError("must not be called")
        ):
            dto = inspect_url(session, url="https://unrelated.com/a", granularity="week")

        assert dto.client is None
        assert dto.client_match == "unknown_domain"
        assert dto.coverage == [
            UrlSourceStateDTO(
                source="client", state="unknown_client", detail="No client matches this URL's host."
            )
        ]
