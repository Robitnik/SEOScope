from __future__ import annotations

from app.contentqa.model import ContentDoc, ContentItem
from app.contentqa.rules import (
    Profile,
    check_brand_voice_own,
    check_cross_page,
    check_data_integrity,
    check_links,
    check_structure,
    check_wikipedia_slop,
)


def test_h1_missing_only_fires_on_empty_not_none() -> None:
    absent = ContentItem(ref="r", h1=None)
    empty = ContentItem(ref="r", h1="")
    assert not any(f.check == "h1-missing" for f in check_structure(absent, Profile()))
    assert any(f.check == "h1-missing" for f in check_structure(empty, Profile()))


def test_h1_multiple_flagged() -> None:
    item = ContentItem(ref="r", headings=(("H1", "First"), ("H1", "Second")))
    findings = check_structure(item, Profile())
    assert any(f.check == "h1-multiple" for f in findings)


def test_orphan_h3_without_preceding_h2() -> None:
    item = ContentItem(ref="r", headings=(("H1", "Title"), ("H3", "Orphan")))
    findings = check_structure(item, Profile())
    assert any(f.check == "orphan-h3" for f in findings)


def test_h3_under_h2_is_not_orphan() -> None:
    item = ContentItem(ref="r", headings=(("H2", "Parent"), ("H3", "Child")))
    findings = check_structure(item, Profile())
    assert not any(f.check == "orphan-h3" for f in findings)


def test_duplicate_heading_detected_by_token_overlap() -> None:
    item = ContentItem(
        ref="r",
        headings=(
            ("H2", "What Is a Schedule of Condition"),
            ("H2", "What Is a Schedule of Condition Report"),
        ),
    )
    findings = check_structure(item, Profile())
    assert any(f.check == "duplicate-heading" for f in findings)


def test_faq_question_without_question_mark() -> None:
    item = ContentItem(ref="r", headings=(("H2", "FAQ"), ("H3", "What this includes")))
    findings = check_structure(item, Profile())
    assert any(f.check == "faq-not-question" for f in findings)


def test_faq_too_long_only_applies_to_blog_kind() -> None:
    # blog-rules.md's FAQ_MAX_WORDS=10 band; framework-rules.md (page) has no
    # such limit ("No minimum or maximum") — gating on kind is deliberate,
    # see rules.check_structure.
    long_q = (
        "What exactly does the GoReport schedule of condition software "
        "capture during a site visit today?"
    )
    item = ContentItem(
        ref="r", headings=(("H2", "Frequently Asked Questions"), ("H3", long_q)),
    )
    blog_findings = check_structure(item, Profile(kind="blog"))
    page_findings = check_structure(item, Profile(kind="page"))
    assert any(f.check == "faq-too-long" for f in blog_findings)
    assert not any(f.check == "faq-too-long" for f in page_findings)


def test_faq_count_band_only_applies_to_blog_kind() -> None:
    item = ContentItem(ref="r", headings=(("H2", "FAQ"), ("H3", "One question?")))
    blog_findings = check_structure(item, Profile(kind="blog"))
    page_findings = check_structure(item, Profile(kind="page"))
    assert any(f.check == "faq-count" for f in blog_findings)
    assert not any(f.check == "faq-count" for f in page_findings)


def test_links_below_min_for_commercial_intent() -> None:
    item = ContentItem(ref="r", intent="commercial", internal_links=("one link",))
    findings = check_links(item, Profile())
    assert any(f.check == "internal-links-below-min" for f in findings)


def test_links_within_band_is_clean() -> None:
    item = ContentItem(ref="r", intent="commercial", internal_links=("a", "b", "c"))
    findings = check_links(item, Profile())
    assert not any(f.check.startswith("internal-links") for f in findings)


def test_links_not_checked_when_doc_has_no_links_data_at_all() -> None:
    item = ContentItem(ref="r")
    findings = check_links(item, Profile())
    assert not any(f.check.startswith("internal-links") for f in findings)


def test_external_links_over_cap() -> None:
    item = ContentItem(
        ref="r", intent="commercial", internal_links=("a", "b"),
        external_links=("x", "y", "z"),
    )
    findings = check_links(item, Profile())
    assert any(f.check == "external-links-over-cap" for f in findings)


def test_word_swap_nudge() -> None:
    item = ContentItem(ref="r", description="We utilize best practices in order to help you.")
    findings = check_brand_voice_own(item, Profile())
    swaps = [f for f in findings if f.check == "voice-word-swap"]
    assert any("utilize" in f.detail for f in swaps)
    assert any("in order to" in f.detail for f in swaps)


def test_terminology_gmb_banned() -> None:
    item = ContentItem(ref="r", description="Update your GMB listing today.")
    findings = check_brand_voice_own(item, Profile())
    assert any(f.check == "voice-terminology" and f.severity == "error" for f in findings)


def test_locale_mismatch_en_us_profile_flags_british_spelling() -> None:
    item = ContentItem(ref="r", description="Standardise SOC reporting across teams.")
    findings = check_brand_voice_own(item, Profile(locale="en-US"))
    assert any(f.check == "voice-locale-mismatch" for f in findings)


def test_locale_mismatch_en_gb_profile_does_not_flag_british_spelling() -> None:
    item = ContentItem(ref="r", description="Standardise SOC reporting across teams.")
    findings = check_brand_voice_own(item, Profile(locale="en-GB"))
    assert not any(f.check == "voice-locale-mismatch" for f in findings)


def test_locale_mismatch_en_gb_profile_flags_american_spelling() -> None:
    item = ContentItem(ref="r", description="We standardize SOC reporting across teams.")
    findings = check_brand_voice_own(item, Profile(locale="en-GB"))
    assert any(f.check == "voice-locale-mismatch" for f in findings)


def test_money_leak_requires_both_trigger_word_and_currency() -> None:
    leaked = ContentItem(ref="r", description="Our monthly retainer is $2,500.")
    safe = ContentItem(ref="r", description="Our monthly retainer covers full reporting.")
    assert any(f.check == "voice-money-leak" for f in check_brand_voice_own(leaked, Profile()))
    assert not any(f.check == "voice-money-leak" for f in check_brand_voice_own(safe, Profile()))


def test_contraction_flagged_in_formal_deliverable() -> None:
    item = ContentItem(ref="r", description="We don't miss deadlines.")
    findings = check_brand_voice_own(item, Profile(kind="page"))
    assert any(f.check == "voice-contraction-in-formal" for f in findings)


def test_llm_artifact_is_error_severity() -> None:
    item = ContentItem(ref="r", description="See details oaicite:3 for the source.")
    findings = check_wikipedia_slop(item, Profile())
    hit = next(f for f in findings if f.check == "slop-llm-artifact")
    assert hit.severity == "error"


def test_placeholder_text_is_error_severity() -> None:
    item = ContentItem(ref="r", description="Pricing: TBD, confirm with client.")
    findings = check_wikipedia_slop(item, Profile())
    assert any(f.check == "slop-placeholder" and f.severity == "error" for f in findings)


def test_vague_attribution_is_warn_not_error() -> None:
    item = ContentItem(ref="r", description="Industry reports suggest this is effective.")
    findings = check_wikipedia_slop(item, Profile())
    hit = next(f for f in findings if f.check == "slop-vague-attribution")
    assert hit.severity == "warn"


def test_row_incomplete_when_named_but_no_copy_fields() -> None:
    item = ContentItem(ref="r", page_name="Pricing", title=None, description=None, h1=None)
    findings = check_data_integrity(item, Profile())
    assert any(f.check == "row-incomplete" for f in findings)


def test_row_incomplete_not_flagged_when_title_present() -> None:
    item = ContentItem(
        ref="r", page_name="Pricing", title="Pricing | Brand", description=None, h1=None,
    )
    findings = check_data_integrity(item, Profile())
    assert not any(f.check == "row-incomplete" for f in findings)


def test_cross_page_keyword_cannibalization() -> None:
    a = ContentItem(ref="Sheet!row3", keywords=("commercial property survey software",))
    b = ContentItem(ref="Sheet!row5", keywords=("commercial property survey software",))
    doc = ContentDoc(source="s.xlsx", doc_type="strategy_xlsx", items=(a, b))
    findings = check_cross_page(doc, Profile())
    assert any(f.check == "keyword-duplicate-across-pages" for f in findings)


def test_cross_page_url_duplicate() -> None:
    a = ContentItem(ref="Sheet!row3", url="https://goreport.com/pricing/")
    b = ContentItem(ref="Sheet!row5", url="https://goreport.com/pricing/")
    doc = ContentDoc(source="s.xlsx", doc_type="strategy_xlsx", items=(a, b))
    findings = check_cross_page(doc, Profile())
    assert any(f.check == "url-duplicate" for f in findings)


def test_no_cross_page_findings_for_distinct_pages() -> None:
    a = ContentItem(ref="Sheet!row3", url="https://goreport.com/pricing/", keywords=("pricing",))
    b = ContentItem(ref="Sheet!row5", url="https://goreport.com/features/", keywords=("features",))
    doc = ContentDoc(source="s.xlsx", doc_type="strategy_xlsx", items=(a, b))
    findings = check_cross_page(doc, Profile())
    assert findings == []


def test_h1_multiple_finding_carries_both_h1_texts() -> None:
    item = ContentItem(ref="r", headings=(("H1", "First Heading"), ("H1", "Second Heading")))
    findings = check_structure(item, Profile())
    hit = next(f for f in findings if f.check == "h1-multiple")
    assert hit.value == "First Heading; Second Heading"


def test_row_incomplete_finding_carries_the_page_name() -> None:
    item = ContentItem(ref="r", page_name="Pricing", title=None, description=None, h1=None)
    findings = check_data_integrity(item, Profile())
    hit = next(f for f in findings if f.check == "row-incomplete")
    assert hit.value == "Pricing"


def test_url_duplicate_finding_carries_the_url() -> None:
    a = ContentItem(ref="row3", url="https://example.com/pricing/")
    b = ContentItem(ref="row5", url="https://example.com/pricing/")
    doc = ContentDoc(source="s.xlsx", doc_type="strategy_xlsx", items=(a, b))
    findings = check_cross_page(doc, Profile())
    hit = next(f for f in findings if f.check == "url-duplicate")
    assert hit.value == "https://example.com/pricing/"
