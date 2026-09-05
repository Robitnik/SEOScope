"""`app.pageinventory.qa.run_page_inventory_qa` — deterministic per-field
finding attribution (task-57 §2.4), including the multi-ref fan-out for
pairwise duplicate checks (`meta-title-duplicate`/`meta-desc-duplicate`)."""

from __future__ import annotations

from app.contentqa.rules import Profile
from app.pageinventory.inventory import InventoryRow
from app.pageinventory.qa import run_page_inventory_qa


def _row(url, *, title=None, description=None, keywords=(), checked=True) -> InventoryRow:
    return InventoryRow(
        url=url, canonical_key=url, duplicate_urls=(), page_label="Page",
        breadcrumbs=(), breadcrumb_source="none", title=title, description=description,
        keywords=keywords, keywords_source="none" if not keywords else "meta", h1=None,
        status=200, status_label="live", indexable=True, canonical_missing=False, checked=checked,
    )


def test_slop_in_description_is_not_attributed_to_title() -> None:
    row = _row(
        "https://acme.com/",
        title="Acme Widgets",
        description="In today's fast-paced world, look no further for widgets.",
    )
    result = run_page_inventory_qa((row,), Profile())
    fields = {ff.field for ff in result.per_row[row.url] if ff.finding.check == "slop-phrase"}
    assert fields == {"description"}


def test_slop_in_title_is_not_attributed_to_description() -> None:
    row = _row(
        "https://acme.com/",
        title="Discover the best-in-class widgets today",
        description="Widgets for professionals and hobbyists.",
    )
    result = run_page_inventory_qa((row,), Profile())
    checks_on_title = {ff.finding.check for ff in result.per_row[row.url] if ff.field == "title"}
    checks_on_description = {
        ff.finding.check for ff in result.per_row[row.url] if ff.field == "description"
    }
    assert "slop-filler-term" in checks_on_title or "slop-phrase" in checks_on_title
    assert "slop-filler-term" not in checks_on_description


def test_meta_title_length_attributed_to_title_field() -> None:
    row = _row("https://acme.com/", title="x" * 90, description="A perfectly fine description.")
    result = run_page_inventory_qa((row,), Profile())
    findings = result.per_row[row.url]
    assert any(ff.finding.check == "meta-title-length" and ff.field == "title" for ff in findings)


def test_pairwise_duplicate_finding_is_attributed_to_both_member_rows() -> None:
    shared = "Buy the best widgets online today for less"
    row_a = _row("https://acme.com/a/", title=shared, description="Description A is unique enough.")
    row_b = _row("https://acme.com/b/", title=shared, description="Description B is unique too.")
    result = run_page_inventory_qa((row_a, row_b), Profile())

    a_checks = {ff.finding.check for ff in result.per_row[row_a.url]}
    b_checks = {ff.finding.check for ff in result.per_row[row_b.url]}
    assert "meta-title-duplicate" in a_checks
    assert "meta-title-duplicate" in b_checks


def test_unchecked_rows_are_skipped_entirely() -> None:
    row = _row("https://acme.com/new/", checked=False)
    result = run_page_inventory_qa((row,), Profile())
    assert row.url not in result.per_row


def test_raw_findings_preserve_multi_ref_for_clustering() -> None:
    shared = "Shop our huge selection of premium widgets now"
    row_a = _row("https://acme.com/a/", title=shared)
    row_b = _row("https://acme.com/b/", title=shared)
    result = run_page_inventory_qa((row_a, row_b), Profile())
    dup = [f for f in result.raw if f.check == "meta-title-duplicate"]
    assert dup
    assert "," in dup[0].ref
