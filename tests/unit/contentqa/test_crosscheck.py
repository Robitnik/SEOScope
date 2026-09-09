"""contentqa/crosscheck.py — lanes H (Frame↔Strategy), I (Frame↔site),
J (Copy↔site) (task-90 §4.5)."""

from __future__ import annotations

from app.contentqa.crosscheck import (
    check_copy_site,
    check_frame_site,
    check_frame_strategy,
    find_strategy_rows_without_framework,
)
from app.contentqa.model import ContentDoc, ContentItem
from app.contentqa.sections import ContentSection
from app.contentqa.site import LivePage
from app.monitor.page import PageSignals


def _frame(**kw: object) -> ContentDoc:
    item = ContentItem(ref="Frame!ref", **kw)  # type: ignore[arg-type]
    return ContentDoc(source="doc", doc_type="framework_gdoc", items=(item,))


def _strategy(*items: ContentItem) -> ContentDoc:
    return ContentDoc(source="strategy", doc_type="strategy_gsheet", items=tuple(items))


def _live_page(**kw: object) -> LivePage:
    defaults: dict[str, object] = dict(
        url="https://example.com/", status=200, error=None,
        signals=PageSignals(), headings=(), sections=(),
    )
    defaults.update(kw)
    return LivePage(**defaults)  # type: ignore[arg-type]


# ── Lane H: Frame ↔ Strategy ─────────────────────────────────────────────────


def test_matched_row_with_identical_fields_gives_no_findings() -> None:
    frame = _frame(
        url="https://example.com/a", title="T", description="D", h1="H", keywords=("kw",)
    )
    strategy_row = ContentItem(
        ref="Strategy!row 2", url="https://example.com/a/",  # trailing slash — must still match
        title="T", description="D", h1="H", keywords=("kw",),
    )
    result = check_frame_strategy(frame, _strategy(strategy_row))
    assert result.findings == []
    assert result.info.state == "checked"
    assert result.matched_key is not None


def test_title_drift_detected() -> None:
    frame = _frame(url="https://example.com/a", title="New Title")
    strategy_row = ContentItem(ref="Strategy!row 2", url="https://example.com/a", title="Old Title")
    result = check_frame_strategy(frame, _strategy(strategy_row))
    checks = {f.check for f in result.findings}
    assert "sync-title-drift" in checks


def test_h1_and_description_drift_detected_independently() -> None:
    frame = _frame(url="https://example.com/a", description="New desc", h1="New H1")
    strategy_row = ContentItem(
        ref="Strategy!row 2", url="https://example.com/a", description="Old desc", h1="Old H1"
    )
    result = check_frame_strategy(frame, _strategy(strategy_row))
    checks = {f.check for f in result.findings}
    assert "sync-desc-drift" in checks
    assert "sync-h1-drift" in checks


def test_missing_field_on_either_side_is_not_a_drift() -> None:
    """PLAN §12: None on either side never fires a drift finding."""
    frame = _frame(url="https://example.com/a", title=None)
    strategy_row = ContentItem(
        ref="Strategy!row 2", url="https://example.com/a", title="Has a title"
    )
    result = check_frame_strategy(frame, _strategy(strategy_row))
    assert result.findings == []


def test_primary_keyword_drift_is_case_insensitive() -> None:
    frame = _frame(url="https://example.com/a", keywords=("Kosher Burger",))
    strategy_row = ContentItem(
        ref="Strategy!row 2", url="https://example.com/a", keywords=("kosher burger", "second")
    )
    result = check_frame_strategy(frame, _strategy(strategy_row))
    assert result.findings == []  # same primary, just different case


def test_keyword_drift_when_primaries_differ() -> None:
    frame = _frame(url="https://example.com/a", keywords=("kosher burger",))
    strategy_row = ContentItem(
        ref="Strategy!row 2", url="https://example.com/a", keywords=("smash burger",)
    )
    result = check_frame_strategy(frame, _strategy(strategy_row))
    assert any(f.check == "sync-keyword-drift" for f in result.findings)


def test_no_match_by_url_gives_not_in_strategy_finding() -> None:
    frame = _frame(url="https://example.com/nowhere", title="X")
    strategy_row = ContentItem(ref="Strategy!row 2", url="https://example.com/elsewhere", title="X")
    result = check_frame_strategy(frame, _strategy(strategy_row))
    assert len(result.findings) == 1
    assert result.findings[0].check == "sync-frame-not-in-strategy"
    assert result.info.state == "checked"  # attempted, just found no match
    assert result.matched_key is None


def test_no_url_or_page_name_gives_unresolved_and_not_checked() -> None:
    frame = _frame(title="X")  # neither url nor page_name
    result = check_frame_strategy(frame, _strategy())
    assert result.findings[0].check == "sync-url-unresolved"
    assert result.info.state == "not_checked"


def test_page_name_fallback_when_url_absent_on_both_sides() -> None:
    frame = _frame(page_name="Homepage", title="X")
    strategy_row = ContentItem(ref="Strategy!row 2", page_name="Homepage", title="Y")
    result = check_frame_strategy(frame, _strategy(strategy_row))
    assert result.matched_key is not None
    assert any(f.check == "sync-title-drift" for f in result.findings)


def test_known_row_bypasses_url_name_join() -> None:
    """Live-measured gap (SHB_SEO_STRATEGY_082626): a strategy sheet can lack
    a recognized URL column while Frame always has its own URL — the guessed
    join then never matches even though the chip link IS exact. Passing
    `known_row` (resolved by package.py from the chip's row number) must
    skip the guess entirely and still catch drift."""
    frame = _frame(url="https://example.com/a", title="Frame Title")
    unrelated_row = ContentItem(ref="Strategy!row 9", page_name="Something Else", title="Other")
    known_row = ContentItem(ref="Strategy!row 2", page_name="Home", title="Strategy Title")
    result = check_frame_strategy(frame, _strategy(unrelated_row), known_row=known_row)
    assert result.info.state == "checked"
    assert any(f.check == "sync-title-drift" for f in result.findings)
    assert not any(f.check == "sync-frame-not-in-strategy" for f in result.findings)


def test_known_row_matching_fields_gives_no_findings() -> None:
    frame = _frame(url="https://example.com/a", title="Same Title")
    known_row = ContentItem(ref="Strategy!row 2", title="Same Title")
    result = check_frame_strategy(frame, _strategy(), known_row=known_row)
    assert result.findings == []
    assert result.info.state == "checked"


def test_empty_frame_doc_is_not_applicable() -> None:
    empty_frame = ContentDoc(source="d", doc_type="framework_gdoc", items=())
    result = check_frame_strategy(empty_frame, _strategy())
    assert result.info.state == "not_applicable"


def test_find_strategy_rows_without_framework() -> None:
    matched = ContentItem(ref="r1", url="https://example.com/matched")
    unmatched = ContentItem(ref="r2", url="https://example.com/orphan")
    strategy = _strategy(matched, unmatched)
    matched_keys = {"url:example.com/matched"}
    findings = find_strategy_rows_without_framework(strategy, matched_keys)
    assert len(findings) == 1
    assert findings[0].check == "sync-strategy-row-no-frame"
    assert "orphan" in findings[0].detail


# ── Lane I: Frame ↔ live site ─────────────────────────────────────────────────


def test_frame_site_blocked_is_not_checked() -> None:
    frame = _frame(url="https://example.com/a", title="X")
    page = _live_page(error="blocked", status=403, signals=None)
    findings, info = check_frame_site(frame, page)
    assert info.state == "not_checked"
    assert findings[0].check == "site-blocked"


def test_frame_site_unreachable_is_not_checked() -> None:
    frame = _frame(url="https://example.com/a", title="X")
    page = _live_page(error="connection_error", status=None, signals=None)
    findings, info = check_frame_site(frame, page)
    assert info.state == "not_checked"
    assert findings[0].check == "site-unreachable"


def test_frame_site_title_h1_desc_drift() -> None:
    frame = _frame(
        url="https://example.com/a", title="Frame Title", h1="Frame H1", description="Frame D"
    )
    page = _live_page(
        signals=PageSignals(title="Live Title", h1="Live H1", description="Live D")
    )
    findings, info = check_frame_site(frame, page)
    checks = {f.check for f in findings}
    assert {"site-title-drift", "site-h1-drift", "site-desc-drift"} <= checks
    assert info.state == "checked"


def test_frame_site_matching_fields_give_no_drift() -> None:
    frame = _frame(url="https://example.com/a", title="Same", h1="Same H1")
    page = _live_page(signals=PageSignals(title="Same", h1="Same H1"))
    findings, _ = check_frame_site(frame, page)
    assert findings == []


def test_heading_missing_from_live_page() -> None:
    frame = _frame(url="https://example.com/a", headings=(("H2", "Missing Section"),))
    page = _live_page(signals=PageSignals(), headings=(("H2", "Other Section"),))
    findings, _ = check_frame_site(frame, page)
    assert any(f.check == "site-heading-missing" for f in findings)


def test_heading_extra_on_live_page_deduped_for_carousel_repeats() -> None:
    """The real SHB site repeats an H3 twice (carousel) — must produce ONE
    site-heading-extra finding, not two, for the same unexpected heading."""
    frame = _frame(url="https://example.com/a", headings=(("H2", "Known Section"),))
    page = _live_page(signals=PageSignals(), headings=(
        ("H2", "Known Section"), ("H3", "Repeated"), ("H3", "Repeated"),
    ))
    findings, _ = check_frame_site(frame, page)
    extra = [f for f in findings if f.check == "site-heading-extra"]
    assert len(extra) == 1


def test_heading_present_gives_no_missing_or_extra() -> None:
    frame = _frame(url="https://example.com/a", headings=(("H2", "Same Section"),))
    page = _live_page(signals=PageSignals(), headings=(("H2", "Same Section"),))
    findings, _ = check_frame_site(frame, page)
    assert findings == []


def test_heading_order_drift() -> None:
    frame = _frame(url="https://example.com/a", headings=(("H2", "A"), ("H2", "B")))
    page = _live_page(signals=PageSignals(), headings=(("H2", "B"), ("H2", "A")))
    findings, _ = check_frame_site(frame, page)
    assert any(f.check == "site-heading-order" for f in findings)


def test_canonical_mismatch() -> None:
    frame = _frame(url="https://example.com/a", title="ok match not tested here")
    page = _live_page(signals=PageSignals(canonical="https://example.com/different-page"))
    findings, _ = check_frame_site(frame, page)
    assert any(f.check == "site-canonical-mismatch" for f in findings)


def test_matching_canonical_gives_no_finding() -> None:
    frame = _frame(url="https://example.com/a")
    page = _live_page(signals=PageSignals(canonical="https://example.com/a/"))  # trailing slash ok
    findings, _ = check_frame_site(frame, page)
    assert findings == []


def test_noindex_page_flagged_as_error() -> None:
    frame = _frame(url="https://example.com/a")
    page = _live_page(signals=PageSignals(meta_robots="noindex, nofollow"), status=200)
    findings, _ = check_frame_site(frame, page)
    assert any(f.check == "site-noindex" for f in findings)


def test_empty_frame_doc_frame_site_not_applicable() -> None:
    findings, info = check_frame_site(
        ContentDoc(source="d", doc_type="framework_gdoc", items=()), _live_page()
    )
    assert findings == []
    assert info.state == "not_applicable"


# ── Lane J: Copy ↔ live site ──────────────────────────────────────────────────


def _section(level: str, heading: str, text: str) -> ContentSection:
    return ContentSection(level=level, heading=heading, text=text)


def test_copy_site_matching_prose_gives_no_finding() -> None:
    copy_sections = (_section("H1", "Title", "Identical prose about the topic."),)
    page = _live_page(sections=(_section("H1", "Title", "Identical prose about the topic."),))
    findings, info = check_copy_site(copy_sections, copy_ref="Copy!ref", site=page)
    assert findings == []
    assert info.state == "checked"


def test_copy_site_drift_below_threshold_is_flagged() -> None:
    copy_sections = (_section("H2", "About Us", "We are a great company with a long history."),)
    live_text = "Completely different unrelated text here."
    page = _live_page(sections=(_section("H2", "About Us", live_text),))
    findings, _ = check_copy_site(copy_sections, copy_ref="Copy!ref", site=page)
    assert any(f.check == "published-copy-drift" for f in findings)


def test_copy_site_section_missing_from_live_page() -> None:
    copy_sections = (_section("H2", "Never Published", "Some prose."),)
    page = _live_page(sections=(_section("H1", "Title", "Other prose."),))
    findings, _ = check_copy_site(copy_sections, copy_ref="Copy!ref", site=page)
    assert any(f.check == "published-section-missing" for f in findings)


def test_copy_site_extra_section_on_live_page() -> None:
    copy_sections = (_section("H1", "Title", "Same text."),)
    page = _live_page(sections=(
        _section("H1", "Title", "Same text."),
        _section("H2", "Bonus Section", "Not in Copy."),
    ))
    findings, _ = check_copy_site(copy_sections, copy_ref="Copy!ref", site=page)
    assert any(f.check == "published-copy-extra" for f in findings)


def test_copy_not_published_when_no_heading_matches() -> None:
    copy_sections = (_section("H1", "Draft Title", "Draft prose."),)
    page = _live_page(sections=(_section("H1", "Completely Different", "Other prose."),))
    findings, info = check_copy_site(copy_sections, copy_ref="Copy!ref", site=page)
    assert any(f.check == "published-not-published" for f in findings)
    assert info.state == "checked"


def test_copy_site_blocked_is_not_checked() -> None:
    copy_sections = (_section("H1", "Title", "Prose."),)
    page = _live_page(error="blocked", status=403)
    findings, info = check_copy_site(copy_sections, copy_ref="Copy!ref", site=page)
    assert info.state == "not_checked"
    assert findings[0].check == "site-blocked"


def test_copy_site_empty_sections_is_not_applicable() -> None:
    findings, info = check_copy_site((), copy_ref="Copy!ref", site=_live_page())
    assert findings == []
    assert info.state == "not_applicable"


def test_duplicate_site_headings_deduped_to_first_occurrence() -> None:
    """A carousel repeating the same section heading twice must not produce
    two published-copy-extra findings for the same logical section."""
    copy_sections = (_section("H1", "Title", "Text."),)
    page = _live_page(sections=(
        _section("H1", "Title", "Text."),
        _section("H3", "Dirty Fries", "desc 1"),
        _section("H3", "Dirty Fries", "desc 1"),
    ))
    findings, _ = check_copy_site(copy_sections, copy_ref="Copy!ref", site=page)
    extra = [f for f in findings if f.check == "published-copy-extra"]
    assert len(extra) == 1
