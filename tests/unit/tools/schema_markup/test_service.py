"""Оркестрація повного прогону (task-56 §5): fetch (injected fake) → extract
→ classify → validate → generate → verdict.
"""

from __future__ import annotations

from app.tools.schema_markup.service import FetchOutcome, run_check

HOME_NO_SCHEMA_HTML = """
<html><head><title>Acme Co — Home</title>
<meta property="og:site_name" content="Acme Co">
<meta property="og:image" content="https://example.com/hero.jpg">
</head><body><h1>Welcome</h1></body></html>
"""

BLOG_WITH_GOOD_SCHEMA_HTML = """
<html><head><title>How we shipped it</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"BlogPosting","headline":"How we shipped it",
 "datePublished":"2026-08-01T10:00:00+00:00","image":"https://example.com/hero.jpg",
 "author":{"@type":"Person","name":"Jane Doe"}}
</script>
</head><body></body></html>
"""

BROKEN_SCHEMA_HTML = """
<html><head><script type="application/ld+json">{"@type": "Organization",}</script></head></html>
"""


def _fake_fetcher(html: str, *, url: str = "https://example.com/", status: int = 200):
    def fetch(_url: str) -> FetchOutcome:
        return FetchOutcome(ok=True, http_status=status, final_url=url, html=html, error=None)

    return fetch


def test_home_page_without_schema_is_missing_and_generates_org_website():
    result = run_check("https://example.com/", fetcher=_fake_fetcher(HOME_NO_SCHEMA_HTML))
    assert result.verdict == "missing"
    assert result.found_types == ()
    assert result.page_kind.kind == "home"
    assert result.generated is not None
    types = {e.type_name for e in result.generated.entities}
    assert types == {"Organization", "WebSite"}
    org = next(e for e in result.generated.entities if e.type_name == "Organization")
    assert org.properties["name"] == "Acme Co"
    assert org.sources["name"] == "page:og:site_name"


def test_client_name_overrides_og_site_name_as_source():
    result = run_check(
        "https://example.com/",
        organization_name="Acme Co (client record)",
        fetcher=_fake_fetcher(HOME_NO_SCHEMA_HTML),
    )
    org = next(e for e in result.generated.entities if e.type_name == "Organization")
    assert org.properties["name"] == "Acme Co (client record)"
    assert org.sources["name"] == "client:name"


def test_good_blog_schema_still_has_gaps_for_recommended_fields():
    result = run_check(
        "https://example.com/blog/how-we-shipped-it",
        fetcher=_fake_fetcher(BLOG_WITH_GOOD_SCHEMA_HTML, url="https://example.com/blog/how-we-shipped-it"),
    )
    assert result.found_types == ("BlogPosting",)
    # headline/image/datePublished усі присутні -> жодного L2 required-error;
    # dateModified — рекомендоване, відсутнє -> info-finding, verdict має це показати.
    from app.tools.schema_markup.validate import Severity

    assert not any(f.severity == Severity.error for f in result.findings)
    assert result.verdict == "has_gaps"
    # Генератор бачить, що BlogPosting уже є на сторінці -> не дублює.
    generated_types = {e.type_name for e in result.generated.entities}
    assert "BlogPosting" not in generated_types
    assert ("BlogPosting", "already present on the page") in result.generated.skipped


def test_broken_json_ld_is_has_gaps_not_missing():
    result = run_check("https://example.com/", fetcher=_fake_fetcher(BROKEN_SCHEMA_HTML))
    assert result.verdict == "has_gaps"
    assert any(f.code == "invalid_json_ld" for f in result.findings)


def test_fetch_failure_short_circuits_to_fetch_failed():
    def failing_fetch(_url: str) -> FetchOutcome:
        return FetchOutcome(ok=False, http_status=404, final_url=None, html=None, error="HTTP 404")

    result = run_check("https://example.com/gone", fetcher=failing_fetch)
    assert result.verdict == "fetch_failed"
    assert result.http_status == 404
    assert result.findings == []
    assert result.generated is None
