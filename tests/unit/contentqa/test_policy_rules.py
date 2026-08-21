"""Тести проти РЕАЛЬНОГО seo_audit.policy (bind-mount у контейнері) — це і є
перевірка, що policy_bridge не розійшовся зі своїм джерелом (задум task-53:
імпортувати, не копіювати)."""

from __future__ import annotations

from seo_audit import policy as live_policy

from app.contentqa.model import ContentDoc, ContentItem
from app.contentqa.policy_rules import (
    check_meta,
    check_meta_duplicates,
    check_slop_and_voice_policy,
)
from app.contentqa.rules import Profile


def test_title_over_house_ceiling_is_error() -> None:
    title = "x" * (live_policy.TITLE_MAX + 1)
    item = ContentItem(ref="r", title=title)
    findings = check_meta(item, Profile())
    hit = next(f for f in findings if f.check == "meta-title-length")
    assert hit.severity == "error"


def test_meta_title_length_finding_carries_the_actual_title_text() -> None:
    title = "Building Surveying Software UK | Digital Reports and RICS Templates | GoReport"
    item = ContentItem(ref="r", title=title)
    findings = check_meta(item, Profile())
    hit = next(f for f in findings if f.check == "meta-title-length")
    assert hit.value == title


def test_meta_desc_length_finding_carries_the_actual_description_text() -> None:
    description = "y" * (live_policy.DESC_MAX + 1)
    item = ContentItem(ref="r", description=description)
    findings = check_meta(item, Profile())
    hit = next(f for f in findings if f.check == "meta-desc-length")
    assert hit.value == description


def test_meta_title_missing_has_no_value_nothing_to_show() -> None:
    item = ContentItem(ref="r", title="")
    findings = check_meta(item, Profile())
    hit = next(f for f in findings if f.check == "meta-title-missing")
    assert hit.value is None


def test_long_title_value_is_excerpted_not_unbounded() -> None:
    title = "x" * 500
    item = ContentItem(ref="r", title=title)
    findings = check_meta(item, Profile())
    hit = next(f for f in findings if f.check == "meta-title-length")
    assert hit.value is not None
    assert len(hit.value) <= 301
    assert hit.value.endswith("…")


def test_title_under_floor_is_warn() -> None:
    item = ContentItem(ref="r", title="Short Title")
    findings = check_meta(item, Profile())
    hit = next(f for f in findings if f.check == "meta-title-length")
    assert hit.severity == "warn"


def test_description_over_house_ceiling_is_error() -> None:
    desc = "x" * (live_policy.DESC_MAX + 1)
    item = ContentItem(ref="r", description=desc)
    findings = check_meta(item, Profile())
    hit = next(f for f in findings if f.check == "meta-desc-length")
    assert hit.severity == "error"


def test_brand_padding_flagged_when_over_ceiling_and_ends_with_brand() -> None:
    base = "Schedule of Condition Software for Commercial Property Surveyors and Managing Agents"
    title = f"{base} | GoReport"
    assert len(title) > live_policy.TITLE_MAX
    item = ContentItem(ref="r", title=title)
    findings = check_meta(item, Profile(brand_names=("GoReport",)))
    assert any(f.check == "meta-title-brand-padding" for f in findings)


def test_banned_opener_in_description_is_error() -> None:
    item = ContentItem(
        ref="r",
        description="Discover the best schedule of condition software on the market today.",
    )
    findings = check_meta(item, Profile())
    assert any(f.check == "meta-desc-banned-opener" and f.severity == "error" for f in findings)


def test_slop_phrase_from_live_policy_wordlist() -> None:
    item = ContentItem(
        ref="r", description="Look no further, this is a game-changer for your team.",
    )
    findings = check_slop_and_voice_policy(item, Profile())
    assert any(f.check == "slop-phrase" for f in findings)


def test_em_dash_is_error_via_policy_bridge() -> None:
    item = ContentItem(ref="r", description="Fast — reliable — trusted.")
    findings = check_slop_and_voice_policy(item, Profile())
    assert any(f.check == "voice-em-dash" and f.severity == "error" for f in findings)


def test_process_leak_is_error_via_policy_bridge() -> None:
    item = ContentItem(ref="r", description="Data pulled from our Ahrefs session for this client.")
    findings = check_slop_and_voice_policy(item, Profile())
    assert any(f.check == "process-leak" for f in findings)


def test_meta_title_duplicate_across_pages() -> None:
    title = "Residential Surveying Software for RICS Home Surveys and Reports"
    a = ContentItem(ref="row3", title=title)
    b = ContentItem(ref="row9", title=title)
    doc = ContentDoc(source="s.xlsx", doc_type="strategy_xlsx", items=(a, b))
    findings = check_meta_duplicates(doc, Profile())
    assert any(f.check == "meta-title-duplicate" for f in findings)
