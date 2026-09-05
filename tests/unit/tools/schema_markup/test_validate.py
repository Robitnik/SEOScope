"""L0/L1/L2 валідація (task-56 §5) — кожен рівень перевіряється окремо, щоб
одна помилка не маскувала іншу.
"""

from __future__ import annotations

from app.tools.schema_markup.extract import extract_all
from app.tools.schema_markup.validate import Level, Severity, validate_all, validate_syntax
from app.tools.schema_markup.vocab import load_vocabulary

VOCAB = load_vocabulary()

CLEAN_ORG_HTML = """
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Organization","name":"Acme","url":"https://example.com/","logo":"https://example.com/logo.png"}
</script>
"""

UNKNOWN_TYPE_HTML = """
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"TotallyMadeUpThing","name":"Acme"}
</script>
"""

WRONG_DOMAIN_PROP_HTML = """
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Organization","name":"Acme","recipeIngredient":"flour"}
</script>
"""

MISSING_REQUIRED_HTML = """
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"BlogPosting","author":{"@type":"Person","name":"Jane"}}
</script>
"""

RETIRED_TYPE_HTML = """
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"HowTo","name":"Fix it"}
</script>
"""

FAQ_HTML = """
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[]}
</script>
"""

BROKEN_JSON_HTML = '<script type="application/ld+json">{"@type": "Organization",}</script>'


def _findings(html: str):
    result = extract_all(html)
    return validate_all(result, VOCAB)


def test_clean_organization_has_no_error_findings():
    findings = _findings(CLEAN_ORG_HTML)
    errors = [f for f in findings if f.severity == Severity.error]
    assert errors == []


def test_l0_syntax_error_reported_and_isolated_from_l1():
    result = extract_all(BROKEN_JSON_HTML)
    l0 = validate_syntax(result)
    assert len(l0) == 1
    assert l0[0].level == Level.l0_syntax
    assert l0[0].code == "invalid_json_ld"
    # L1/L2 не намагаються "здогадатися" про зламаний блок — сутностей нема.
    all_findings = validate_all(result, VOCAB)
    assert all(f.level == Level.l0_syntax for f in all_findings)


def test_l1_unknown_type_is_error():
    findings = _findings(UNKNOWN_TYPE_HTML)
    unknown = [f for f in findings if f.code == "unknown_type"]
    assert len(unknown) == 1
    assert unknown[0].level == Level.l1_vocabulary
    assert unknown[0].severity == Severity.error


def test_l1_property_outside_domain_is_warning_not_error():
    findings = _findings(WRONG_DOMAIN_PROP_HTML)
    mismatched = [f for f in findings if f.code == "property_not_in_domain"]
    assert len(mismatched) == 1
    assert mismatched[0].property_name == "recipeIngredient"
    assert mismatched[0].severity == Severity.warn


def test_l2_missing_required_property_is_error_with_docs_link():
    findings = _findings(MISSING_REQUIRED_HTML)
    missing = [f for f in findings if f.code == "missing_required_property"]
    missing_props = {f.property_name for f in missing}
    assert "headline" in missing_props
    assert "datePublished" in missing_props
    assert all(f.severity == Severity.error for f in missing)
    assert all("developers.google.com" in f.message for f in missing)


def test_l2_retired_type_warns_not_errors():
    findings = _findings(RETIRED_TYPE_HTML)
    retired = [f for f in findings if f.code == "retired_rich_result"]
    assert len(retired) == 1
    assert retired[0].severity == Severity.warn


def test_l2_faq_flagged_no_rich_result_but_still_valid():
    findings = _findings(FAQ_HTML)
    no_rr = [f for f in findings if f.code == "no_rich_result"]
    assert len(no_rr) == 1
    assert no_rr[0].severity == Severity.info
    # FAQPage сам по собі лишається валідним типом — жодної unknown_type помилки.
    assert not any(f.code == "unknown_type" for f in no_rr)
