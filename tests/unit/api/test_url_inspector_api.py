"""URL Inspector endpoints (`GET/POST /api/v1/url-inspector*`, task-61)."""

from __future__ import annotations

from unittest.mock import patch

from app.db.repositories.clients import ClientRepository


def _seed_client(db, *, domain: str = "acme.com", name: str = "Acme") -> None:
    ClientRepository(db).create(
        name=name, slug=name.lower(), domain=domain, timezone="America/New_York"
    )
    db.commit()


class TestGetUrlInspector:
    def test_resolves_known_client(self, client, db):
        _seed_client(db)

        response = client.get("/api/v1/url-inspector", params={"url": "https://www.acme.com/a"})

        assert response.status_code == 200
        body = response.json()
        assert body["client"]["name"] == "Acme"
        assert body["client_match"] == "exact"
        assert {c["source"] for c in body["coverage"]} == {
            "gsc", "reporting_ninja", "monitor", "archive", "page_inventory", "tech_audit",
        }

    def test_unknown_domain_is_a_200_not_an_error(self, client, db):
        _seed_client(db)

        response = client.get("/api/v1/url-inspector", params={"url": "https://unrelated.com/a"})

        assert response.status_code == 200
        body = response.json()
        assert body["client"] is None
        assert body["client_match"] == "unknown_domain"

    def test_invalid_url_is_400_with_error_code(self, client, db):
        response = client.get("/api/v1/url-inspector", params={"url": "javascript:alert(1)"})

        assert response.status_code == 400
        assert response.json()["detail"]["error_code"] == "unsupported_scheme"

    def test_empty_url_is_400(self, client, db):
        response = client.get("/api/v1/url-inspector", params={"url": " "})

        assert response.status_code == 400
        assert response.json()["detail"]["error_code"] == "empty"

    def test_granularity_defaults_to_week(self, client, db):
        _seed_client(db)

        response = client.get("/api/v1/url-inspector", params={"url": "https://www.acme.com/a"})

        assert response.status_code == 200
        assert response.json()["search_analytics"]["gsc_series"] == {}

    def test_one_broken_source_still_returns_200(self, client, db):
        _seed_client(db)

        with patch("app.urlinspector.service.collect_gsc", side_effect=RuntimeError("boom")):
            response = client.get(
                "/api/v1/url-inspector", params={"url": "https://www.acme.com/a"}
            )

        assert response.status_code == 200
        by_source = {c["source"]: c for c in response.json()["coverage"]}
        assert by_source["gsc"]["state"] == "failed"
        assert by_source["monitor"]["state"] != "failed"


class TestPostUrlInspectorLive:
    def test_invalid_url_is_400(self, client, db):
        response = client.post("/api/v1/url-inspector/live", json={"url": "https:///a"})

        assert response.status_code == 400
        assert response.json()["detail"]["error_code"] == "invalid_url"

    def test_blocked_trace_short_circuits(self, client, db):
        from app.tools.redirects.trace import Hop, TraceResult, Verdict

        blocked = TraceResult(
            input_url="https://www.acme.com/a", normalized_url="https://www.acme.com/a",
            verdict=Verdict.blocked,
            hops=(Hop(index=0, url="https://www.acme.com/a", status=429, status_text="Too Many"),),
            final_url="https://www.acme.com/a", final_status=429,
        )
        with (
            patch("app.urlinspector.live.trace_redirects", return_value=blocked),
            patch("app.urlinspector.live.fetch", side_effect=AssertionError("must not run")),
        ):
            response = client.post(
                "/api/v1/url-inspector/live", json={"url": "https://www.acme.com/a"}
            )

        assert response.status_code == 200
        assert response.json()["verdict"] == "blocked"


class TestGetUrlInspectorSuggest:
    def test_domain_stage(self, client, db):
        _seed_client(db, domain="acme.com", name="Acme")

        response = client.get("/api/v1/url-inspector/suggest", params={"q": "acm"})

        assert response.status_code == 200
        assert response.json()["suggestions"] == ["https://acme.com/"]

    def test_short_query_is_empty(self, client, db):
        response = client.get("/api/v1/url-inspector/suggest", params={"q": "a"})

        assert response.status_code == 200
        assert response.json()["suggestions"] == []
