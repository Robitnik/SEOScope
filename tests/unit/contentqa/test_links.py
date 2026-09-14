"""contentqa/links.py — Framework -> Copy -> Site link chain (task-95).

`_extract_link_target` tests use the exact live-measured strings from
task-91 §1.4/§1.4a + task-95 §1.6 (SDS/SAPHNY parens, 3XE arrows, LAXTON's
misleading arrow, PJC/EFFYS's URL-less prose, SDS's dash form) — not
invented examples. `audit_links` tests reproduce the live EFFYS/LAXTON
scenarios from task-95 §1.1/§1.7 (false-green nav links, stale PDF drift,
zero-hyperlink copy tabs).
"""

from __future__ import annotations

from app.contentqa.links import (
    LinkRow,
    _extract_link_target,
    _match_key,
    audit_links,
)
from app.contentqa.model import ContentItem
from app.contentqa.readers.gdoc import CopyLink
from app.contentqa.site import LivePage, SiteLink
from app.monitor.page import PageSignals

# ── _extract_link_target (task-91 §1.4a + task-95 §1.6) ────────────────


def test_extracts_absolute_url_strategy_format() -> None:
    raw = "INT LINK 1 - https://www.shorelinedentalstudio.com/services/invisalign/"
    assert (
        _extract_link_target(raw, base_url=None)
        == "https://www.shorelinedentalstudio.com/services/invisalign/"
    )


def test_extracts_parenthesized_path_sds_saphny_format() -> None:
    raw = (
        'Dental Implants (/services/tooth-replacement/dental-implants/) '
        '- in "Discuss Implant Support"'
    )
    assert (
        _extract_link_target(raw, base_url="https://example.com/x")
        == "https://example.com/services/tooth-replacement/dental-implants/"
    )


def test_extracts_path_after_arrow_3xe_format() -> None:
    raw = "2026 EXXXOTICA locations → /locations/"
    assert (
        _extract_link_target(raw, base_url="https://exxxotica.com/home")
        == "https://exxxotica.com/locations/"
    )


def test_arrow_followed_by_non_path_is_not_extracted_laxton_format() -> None:
    # Live LAXTON cell: the arrow is there, but what follows is a
    # placement description, not a URL.
    raw = 'utrustning för spökjakt → placement: H2 "Utrustningen bakom undersökningarna"'
    assert _extract_link_target(raw, base_url="https://laxton.se/artikel") is None


def test_plain_prose_with_no_url_is_not_extracted_pjc_effys_format() -> None:
    assert _extract_link_target("make a reservation", base_url="https://pjclarkes.com/") is None
    assert _extract_link_target("Effy's UWS location", base_url="https://effys.com/") is None


def test_relative_path_without_base_url_is_returned_as_is() -> None:
    assert _extract_link_target("anchor → /menu/", base_url=None) == "/menu/"


def test_extracts_dash_path_sds_format() -> None:
    # task-95 §1.6: live SDS style, 5.3% of the whole Framework-link
    # corpus, silently dropped before this task.
    raw = "emergency dental care - /services/emergency-dental-care/"
    assert (
        _extract_link_target(raw, base_url="https://shorelinedentalstudio.com/x")
        == "https://shorelinedentalstudio.com/services/emergency-dental-care/"
    )


def test_dash_form_does_not_shadow_arrow_form() -> None:
    # An em-dash used purely as punctuation before an arrow-style link
    # must not swallow the arrow's own path.
    raw = "Paranormal REM Static – PRS → /shop/utrustning/prs/"
    assert (
        _extract_link_target(raw, base_url="https://laxton.se/x")
        == "https://laxton.se/shop/utrustning/prs/"
    )


# ── _match_key (task-95 §1.8) ────────────────────────────────────────


def test_match_key_casefolds_path_but_inspector_key_does_not() -> None:
    assert _match_key("https://example.com/Locations/") == _match_key(
        "https://example.com/locations/"
    )


def test_match_key_ignores_query() -> None:
    assert _match_key("https://example.com/shop/?add-to-cart=1") == _match_key(
        "https://example.com/shop/"
    )


# ── audit_links: chesni stany (task-95 §2.6) ────────────────────────────


def _framework_item(*links: str, url: str = "https://example.com/page/") -> ContentItem:
    return ContentItem(ref="fw", url=url, internal_links=links)


def _site(
    *, links: tuple[SiteLink, ...] = (), ok: bool = True, error: str | None = None
) -> LivePage:
    return LivePage(
        url="https://example.com/page/", status=200 if ok else None,
        error=None if ok else error, signals=PageSignals(title="T"),
        headings=(), sections=(), internal_links=links,
    )


def test_not_applicable_when_neither_side_claims_anything() -> None:
    result = audit_links(
        None, copy_links=(), has_copy_tab=False, copy_genre=None,
        site_page=None, has_url=False,
    )
    assert result.state == "not_applicable"
    assert result.in_copy.state == "not_comparable"
    assert result.on_site.state == "not_comparable"


def test_false_green_bug_is_fixed_framework_links_not_in_copy() -> None:
    """task-95 §1.1: the exact EFFYS columbus-ave shape — Framework
    declares 3 links, the copy contains NONE of them (they were only in
    the site's global nav) — the OLD `matrix.py` verdict said `match`
    because it checked against every `<a href>` on the page; this must
    now say `mismatch`."""
    fw = _framework_item(
        "full menu (/menu/)",
        "Effy's on 96th Street (/locations/upper-west-side/)",
        "our café story (/about-us/)",
    )
    result = audit_links(
        fw, copy_links=(), has_copy_tab=True, copy_genre="copy",
        site_page=_site(links=(SiteLink(url="https://example.com/menu/", text="Menu"),)),
        has_url=True,
    )
    assert result.in_copy.state == "mismatch"
    assert result.in_copy.detail == "0/3 framework link(s) in copy"


def test_framework_link_found_in_copy_and_on_site() -> None:
    fw = _framework_item("our menu (/menu/)")
    copy_links = (CopyLink(url="https://example.com/menu/", anchor="our menu", in_marker=False),)
    result = audit_links(
        fw, copy_links=copy_links, has_copy_tab=True, copy_genre="copy",
        site_page=_site(links=(SiteLink(url="https://example.com/menu/", text="Menu"),)),
        has_url=True,
    )
    assert result.in_copy.state == "match"
    assert result.on_site.state == "match"
    assert result.rows == (
        LinkRow(
            target=_match_key("https://example.com/menu/"),
            display="our menu (/menu/)", origin="framework",
            in_copy="yes", on_site="yes", note=None,
        ),
    )


def test_stale_pdf_drift_caught_via_copy_only_link() -> None:
    """task-95 §1.7: EFFYS_UWS — copy links to last year's certificate
    PDF, the live site already serves this year's. Framework never
    claimed this link at all — it must still surface as `on_site`
    mismatch (chain includes copy-only links, not just framework ones)."""
    copy_links = (
        CopyLink(
            url="https://example.com/cert-2026.pdf", anchor="current certificate",
            in_marker=False,
        ),
    )
    result = audit_links(
        None, copy_links=copy_links, has_copy_tab=True, copy_genre="copy",
        site_page=_site(links=(SiteLink(url="https://example.com/cert-2027.pdf", text="cert"),)),
        has_url=True,
    )
    assert result.on_site.state == "mismatch"
    assert result.rows[0].origin == "copy"
    assert result.rows[0].on_site == "no"


def test_zero_hyperlinks_in_copy_is_a_real_mismatch_not_not_comparable() -> None:
    """task-95 §1.5: 13/56 live copy tabs have zero hyperlinks despite the
    framework declaring several — that is a defect to report, not a
    silent `not_comparable`."""
    fw = _framework_item("link one (/a/)", "link two (/b/)")
    result = audit_links(
        fw, copy_links=(), has_copy_tab=True, copy_genre="copy",
        site_page=_site(links=(SiteLink(url="https://example.com/a/", text="A"),)),
        has_url=True,
    )
    assert result.in_copy.state == "mismatch"
    assert result.in_copy.detail == "0/2 framework link(s) in copy"


def test_no_copy_tab_gives_not_comparable_with_reason() -> None:
    fw = _framework_item("link (/a/)")
    result = audit_links(
        fw, copy_links=(), has_copy_tab=False, copy_genre=None,
        site_page=_site(), has_url=True,
    )
    assert result.in_copy.state == "not_comparable"
    assert result.in_copy.detail == "no copy tab in this document"


def test_marker_instruction_links_are_skipped_not_counted() -> None:
    """task-95 §1.4: EFFYS_LOCATIONS_HUB — `[MAP: ...]`/`[Order →]` rows
    are verstalnyk instructions, not copy the operator should expect on
    the page — they must not count toward either verdict."""
    fw = _framework_item("real link (/menu/)")
    copy_links = (
        CopyLink(url="https://example.com/menu/", anchor="menu", in_marker=False),
        CopyLink(
            url="https://maps.google.com/x", anchor="[MAP: location]", in_marker=True,
        ),
    )
    result = audit_links(
        fw, copy_links=copy_links, has_copy_tab=True, copy_genre="copy",
        site_page=_site(links=(SiteLink(url="https://example.com/menu/", text="Menu"),)),
        has_url=True,
    )
    assert result.in_copy.state == "match"
    assert result.on_site.state == "match"
    assert len(result.rows) == 1
    assert any("instruction, skipped" in s for s in result.skipped)


def test_no_target_framework_row_is_skipped_not_guessed() -> None:
    fw = _framework_item("order online", "our menu (/menu/)")
    result = audit_links(
        fw, copy_links=(), has_copy_tab=True, copy_genre="copy",
        site_page=_site(), has_url=True,
    )
    assert "order online" in result.skipped
    assert result.in_copy.detail == "0/1 framework link(s) in copy"


def test_site_unreachable_gives_not_comparable_on_site_with_reason() -> None:
    fw = _framework_item("our menu (/menu/)")
    copy_links = (CopyLink(url="https://example.com/menu/", anchor="menu", in_marker=False),)
    result = audit_links(
        fw, copy_links=copy_links, has_copy_tab=True, copy_genre="copy",
        site_page=_site(ok=False, error="blocked"), has_url=True,
    )
    assert result.on_site.state == "not_comparable"
    assert result.on_site.detail == "blocked"


def test_no_url_gives_not_comparable_on_site() -> None:
    fw = _framework_item("our menu (/menu/)")
    copy_links = (CopyLink(url="https://example.com/menu/", anchor="menu", in_marker=False),)
    result = audit_links(
        fw, copy_links=copy_links, has_copy_tab=True, copy_genre="copy",
        site_page=None, has_url=False,
    )
    assert result.on_site.state == "not_comparable"
    assert result.on_site.detail == "no URL"


def test_edit_brief_genre_annotates_rows_but_does_not_change_verdict() -> None:
    fw = _framework_item("our menu (/menu/)")
    copy_links = (CopyLink(url="https://example.com/menu/", anchor="menu", in_marker=False),)
    result = audit_links(
        fw, copy_links=copy_links, has_copy_tab=True, copy_genre="edit_brief",
        site_page=_site(links=(SiteLink(url="https://example.com/menu/", text="Menu"),)),
        has_url=True,
    )
    assert result.in_copy.state == "match"
    assert all(row.note for row in result.rows)
