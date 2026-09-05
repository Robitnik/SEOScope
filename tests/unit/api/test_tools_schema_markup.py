"""API-тести Schema Markup Checker (task-56): `run_check` мокнутий (мережа не
йде за межі процесу), персистенція + list/detail перевіряються через реальну
БД тестового фікстура (той самий `client`/`db`, що `test_monitor_robots_
sitemap_check_api.py`)."""

from __future__ import annotations

from app.api import tools_schema_markup as api_mod
from app.db.repositories.clients import ClientRepository
from app.tools.schema_markup.classify import ClassifyResult
from app.tools.schema_markup.extract import extract_all
from app.tools.schema_markup.generate import GeneratedEntity, GenerationResult
from app.tools.schema_markup.service import CheckResult
from app.tools.schema_markup.validate import Finding, Level, Severity


def _create_client(db, *, slug: str = "acme") -> str:
    client = ClientRepository(db).create(
        name="Acme", slug=slug, domain=f"{slug}.com", timezone="America/New_York"
    )
    db.commit()
    return client.id


def _clean_result(url: str) -> CheckResult:
    return CheckResult(
        url=url,
        final_url=url,
        http_status=200,
        verdict="missing",
        page_kind=ClassifyResult(kind="home", confidence="high", reason="empty path"),
        found_types=(),
        findings=[],
        generated=GenerationResult(
            entities=[
                GeneratedEntity(
                    type_name="Organization",
                    properties={"name": "Acme", "url": url},
                    sources={"name": "client:name", "url": "url_root"},
                    needs_input=["logo"],
                )
            ],
            skipped=[],
        ),
        vocab_version="latest",
        error=None,
    )


def _gaps_result(url: str) -> CheckResult:
    return CheckResult(
        url=url,
        final_url=url,
        http_status=200,
        verdict="has_gaps",
        page_kind=ClassifyResult(
            kind="blog_post", confidence="medium", reason="url segment 'blog'"
        ),
        found_types=("BlogPosting",),
        findings=[
            Finding(
                level=Level.l2_google,
                severity=Severity.error,
                code="missing_required_property",
                message='BlogPosting is missing "headline".',
                entity_types=("BlogPosting",),
                property_name="headline",
            )
        ],
        generated=GenerationResult(entities=[], skipped=[("BlogPosting", "already present")]),
        vocab_version="latest",
        error=None,
    )


def _failed_result(url: str) -> CheckResult:
    return CheckResult(
        url=url, final_url=None, http_status=404, verdict="fetch_failed",
        page_kind=None, found_types=(), findings=[], generated=None,
        vocab_version="latest", error="HTTP 404",
    )


_RAW_HTML = """
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Acme"}</script>
<script type="application/ld+json">{"@type": "Broken",}</script>
<div itemscope itemtype="https://schema.org/Product"><span itemprop="name">Mug</span></div>
"""


def _result_with_real_extraction(url: str) -> CheckResult:
    extraction = extract_all(_RAW_HTML)
    return CheckResult(
        url=url, final_url=url, http_status=200, verdict="has_gaps",
        page_kind=ClassifyResult(kind="home", confidence="high", reason="empty path"),
        found_types=("Organization", "Product"), findings=[], generated=None,
        vocab_version="latest", error=None, extraction=extraction,
    )


def test_create_check_persists_and_returns_generated_snippet(client, db, monkeypatch):
    client_id = _create_client(db)
    monkeypatch.setattr(api_mod, "run_check", lambda url, **kw: _clean_result(url))

    response = client.post(
        "/api/v1/tools/schema-markup/checks",
        json={"url": "https://acme.com/", "client_id": client_id},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "missing"
    assert body["client_name"] == "Acme"
    assert body["generated"]["entities"][0]["type_name"] == "Organization"
    assert body["generated"]["entities"][0]["sources"]["name"] == "client:name"
    assert body["generated"]["entities"][0]["needs_input"] == ["logo"]
    assert "id" in body


def test_create_check_without_client_has_no_client_name(client, monkeypatch):
    monkeypatch.setattr(api_mod, "run_check", lambda url, **kw: _clean_result(url))
    response = client.post(
        "/api/v1/tools/schema-markup/checks", json={"url": "https://example.com/"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["client_id"] is None
    assert body["client_name"] is None


def test_get_check_returns_findings_with_level_and_severity(client, monkeypatch):
    monkeypatch.setattr(
        api_mod, "run_check", lambda url, **kw: _gaps_result(url)
    )
    created = client.post(
        "/api/v1/tools/schema-markup/checks",
        json={"url": "https://example.com/blog/post"},
    ).json()

    response = client.get(f"/api/v1/tools/schema-markup/checks/{created['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "has_gaps"
    assert body["found_types"] == ["BlogPosting"]
    finding = body["findings"][0]
    assert finding["level"] == "L2"
    assert finding["severity"] == "error"
    assert finding["property_name"] == "headline"


def test_get_check_returns_raw_markup_blocks(client, monkeypatch):
    monkeypatch.setattr(api_mod, "run_check", lambda url, **kw: _result_with_real_extraction(url))
    created = client.post(
        "/api/v1/tools/schema-markup/checks", json={"url": "https://example.com/"}
    ).json()

    response = client.get(f"/api/v1/tools/schema-markup/checks/{created['id']}")
    body = response.json()
    raw = body["raw_markup"]
    assert len(raw) == 3  # 2 json-ld blocks (1 valid + 1 broken) + 1 microdata item

    valid_jsonld = next(b for b in raw if b["format"] == "json-ld" and b["error"] is None)
    assert valid_jsonld["raw"]["@type"] == "Organization"
    assert valid_jsonld["raw"]["name"] == "Acme"

    broken_jsonld = next(b for b in raw if b["format"] == "json-ld" and b["error"] is not None)
    assert broken_jsonld["raw"] is None
    assert broken_jsonld["error"]  # non-empty JSON parser message (trailing comma)

    microdata = next(b for b in raw if b["format"] == "microdata")
    assert microdata["raw"]["@type"] == "Product"
    assert microdata["raw"]["name"] == "Mug"


def test_fetch_failed_check_has_no_raw_markup(client, monkeypatch):
    monkeypatch.setattr(api_mod, "run_check", lambda url, **kw: _failed_result(url))
    created = client.post(
        "/api/v1/tools/schema-markup/checks", json={"url": "https://example.com/gone"}
    ).json()
    assert created["raw_markup"] == []


def test_fetch_failed_check_has_no_findings_or_generated(client, monkeypatch):
    monkeypatch.setattr(api_mod, "run_check", lambda url, **kw: _failed_result(url))
    response = client.post(
        "/api/v1/tools/schema-markup/checks", json={"url": "https://example.com/gone"}
    )
    body = response.json()
    assert body["verdict"] == "fetch_failed"
    assert body["findings"] == []
    assert body["generated"] is None
    assert body["error"] == "HTTP 404"


def test_get_unknown_check_is_404(client):
    response = client.get("/api/v1/tools/schema-markup/checks/does-not-exist")
    assert response.status_code == 404


def test_list_checks_returns_summaries_newest_first(client, monkeypatch):
    monkeypatch.setattr(api_mod, "run_check", lambda url, **kw: _clean_result(url))
    client.post("/api/v1/tools/schema-markup/checks", json={"url": "https://a.example.com/"})
    client.post("/api/v1/tools/schema-markup/checks", json={"url": "https://b.example.com/"})

    response = client.get("/api/v1/tools/schema-markup/checks")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["checks"][0]["url"] == "https://b.example.com/"
    assert body["checks"][1]["url"] == "https://a.example.com/"


def test_list_checks_filters_by_client_id(client, db, monkeypatch):
    client_id = _create_client(db)
    monkeypatch.setattr(api_mod, "run_check", lambda url, **kw: _clean_result(url))
    client.post(
        "/api/v1/tools/schema-markup/checks",
        json={"url": "https://acme.com/", "client_id": client_id},
    )
    client.post("/api/v1/tools/schema-markup/checks", json={"url": "https://other.example.com/"})

    response = client.get(f"/api/v1/tools/schema-markup/checks?client_id={client_id}")
    body = response.json()
    assert body["total"] == 1
    assert body["checks"][0]["client_id"] == client_id


def test_tools_catalog_lists_schema_markup(client):
    response = client.get("/api/v1/tools")
    assert response.status_code == 200
    keys = [t["key"] for t in response.json()["tools"]]
    assert "schema-markup" in keys
