"""One shared meta/copy QA pass for a single URL, given whatever
title/description/h1/status it currently has — used by both
`sources/inventory.py` (stored monitor state) and `live.py` (a fresh live
fetch), so the two panels can never disagree about what counts as a finding
for the same fields (task-61 §2.3/§2.4).

Thin wrapper around `app.pageinventory.qa.run_page_inventory_qa` — the exact
function task-57 runs for the whole portfolio, given a single-row input
instead of reimplementing any of its rules here.
"""

from __future__ import annotations

from app.contentqa.rules import Profile
from app.pageinventory.inventory import InventoryRow
from app.pageinventory.qa import run_page_inventory_qa
from app.schemas.url_inspector import UrlFindingDTO
from app.urlinspector.urlkey import inspector_key


def qa_findings(
    url: str,
    *,
    title: str | None,
    description: str | None,
    h1: str | None,
    status: int | None,
) -> list[UrlFindingDTO]:
    """`status=None` (never checked) skips QA entirely (PLAN §12 — "not
    checked" isn't a text-QA subject, same rule `run_page_inventory_qa`
    itself applies to `checked=False` rows).

    `title`/`description`/`h1` are coerced `None -> ""` here — `InventoryRow`'s
    own convention (its docstring) is that for a CHECKED page, `""` means
    "tag present-but-empty OR absent from the fetched HTML" (both real
    defects), and `None` is reserved for "never checked". Passing a raw
    `None` through unconverted silently skipped the missing-tag check
    entirely (caught live: a page with no `<meta description>` at all
    produced zero `meta-desc-missing` findings until this coercion)."""
    if status is None:
        return []
    row = InventoryRow(
        url=url,
        canonical_key=inspector_key(url),
        duplicate_urls=(),
        page_label="",
        breadcrumbs=(),
        breadcrumb_source="none",
        title=title or "",
        description=description or "",
        keywords=(),
        keywords_source="none",
        h1=h1 or "",
        status=status,
        status_label="live" if status == 200 else "broken",
        indexable=None,
        canonical_missing=False,
        checked=True,
    )
    profile = Profile(kind="page", intent="commercial", locale="en-US")
    result = run_page_inventory_qa((row,), profile)
    return [
        UrlFindingDTO(
            check=ff.finding.check,
            field=ff.field,
            severity=ff.finding.severity,
            message=ff.finding.detail,
        )
        for ff in result.per_row.get(url, [])
    ]
