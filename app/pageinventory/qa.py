"""`InventoryRow` -> per-field `Finding`s, via `app.contentqa`'s rule engine
(task-53, read-only dependency — task-57 §2.4/§1.4).

`app.contentqa.findings.Finding` has no `field` attribute, so which column
(title/description/keywords/h1/url/page) a finding belongs to is derived
HERE, deterministically — never guessed from `Finding.value` text:

- checks whose name names the field (`meta-title-*`, `meta-desc-*`, `h1-*`,
  `url-duplicate`, `keyword-*`) map via `FIELD_BY_CHECK`;
- slop/voice checks (`slop-*`, `voice-*`, `process-leak`, `tool-failure-*`,
  `ppl-ai-file-upload`) can fire on ANY text field, so those are found with
  an ISOLATED single-field pass: the engine runs three times per row, once
  each with only `title`, `description`, or `keywords` populated (the other
  two blanked to `None`, which `app.contentqa` rules skip) — a finding from
  pass N belongs to field N by construction, not by inference (task-57
  §2.4). Cross-page checks (`*-duplicate`, `sheet-skipped`) run once on the
  full document and also resolve via `FIELD_BY_CHECK`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from app.contentqa.engine import run_checks
from app.contentqa.findings import Finding
from app.contentqa.model import ContentDoc, ContentItem
from app.contentqa.rules import Profile
from app.pageinventory.inventory import InventoryRow

FIELD_BY_CHECK: dict[str, str] = {
    "meta-title-missing": "title",
    "meta-title-length": "title",
    "meta-title-brand-padding": "title",
    "meta-title-keyword-missing": "title",
    "meta-title-duplicate": "title",
    "meta-desc-missing": "description",
    "meta-desc-length": "description",
    "meta-desc-authored-long": "description",
    "meta-desc-banned-opener": "description",
    "meta-desc-duplicate": "description",
    "h1-missing": "h1",
    "h1-multiple": "h1",
    "h1-equals-title": "h1",
    "url-duplicate": "url",
    "keyword-duplicate-across-pages": "keywords",
    "policy-unavailable": "page",
    "sheet-skipped": "page",
    "row-incomplete": "page",
}

_ISOLATABLE_PREFIXES = ("slop-", "voice-")
_ISOLATABLE_EXTRA = frozenset({"process-leak", "tool-failure-narrative", "ppl-ai-file-upload"})

# These checks join every member's `item.ref` with ", " into ONE `Finding.ref`
# string instead of one finding per subject (`app/contentqa/policy_rules.py`
# `check_meta_duplicates`, `app/contentqa/rules.py` `check_cross_page`) — a
# finding here belongs to EVERY member URL, not to a single row (task-57
# §2.4). `url-duplicate` never fires in our pipeline in practice (rows are
# already deduped by canonical — task-57 §1.5) but is handled the same way
# for correctness if it ever does.
MULTI_REF_CHECKS = frozenset({
    "meta-title-duplicate", "meta-desc-duplicate", "url-duplicate",
    "keyword-duplicate-across-pages",
})


def _is_isolatable(check: str) -> bool:
    return check.startswith(_ISOLATABLE_PREFIXES) or check in _ISOLATABLE_EXTRA


def refs_of(finding: Finding) -> list[str]:
    if finding.check in MULTI_REF_CHECKS:
        return [part.strip() for part in finding.ref.split(",")]
    return [finding.ref]


@dataclass(frozen=True)
class FieldFinding:
    field: str  # "title" | "description" | "keywords" | "h1" | "url" | "page"
    finding: Finding


def _item_for_row(row: InventoryRow, *, only_field: str | None = None) -> ContentItem:
    """Builds a `ContentItem` for one row. `only_field` blanks every other
    text field to `None` — the isolated single-field pass (module docstring).
    `app.contentqa.model.ContentItem`'s own convention (`None` = field not
    in this source) makes that blanking a no-op for the rules that read it,
    not a fabricated absence: we are not claiming the page has no title,
    only that THIS pass isn't checking it.
    """
    title = row.title if only_field in (None, "title") else None
    description = row.description if only_field in (None, "description") else None
    keywords = row.keywords if only_field in (None, "keywords") else ()
    return ContentItem(
        ref=row.url,
        page_name=row.page_label or None,
        url=row.url,
        title=title,
        description=description,
        keywords=keywords,
    )


@dataclass(frozen=True)
class QaResult:
    per_row: dict[str, list[FieldFinding]]
    # Unfiltered findings straight from `run_checks`, kept alongside
    # `per_row` so `app.pageinventory.calibrate` can union-find the
    # pairwise duplicate findings into clusters without re-parsing refs
    # that `per_row` has already fanned out to every member row.
    raw: list[Finding]


def run_page_inventory_qa(rows: tuple[InventoryRow, ...], profile: Profile) -> QaResult:
    """Every checked row gets a `per_row` entry (possibly empty); unchecked
    rows (`checked=False`) are skipped, since "not checked" isn't a text-QA
    subject (PLAN §12)."""

    checked_rows = [r for r in rows if r.checked]
    per_row: dict[str, list[FieldFinding]] = {r.url: [] for r in checked_rows}
    raw: list[Finding] = []
    if not checked_rows:
        return QaResult(per_row=per_row, raw=raw)

    def _attach(finding: Finding, field: str) -> None:
        raw.append(finding)
        for ref in refs_of(finding):
            if ref in per_row:
                per_row[ref].append(FieldFinding(field=field, finding=finding))

    # Pass 1 — full items, catches every check whose field is named by
    # `FIELD_BY_CHECK` plus cross-page checks (duplicates, skipped sheets).
    full_doc = ContentDoc(
        source="page-inventory", doc_type="strategy_xlsx",
        items=tuple(_item_for_row(r) for r in checked_rows),
    )
    for finding in run_checks(full_doc, profile):
        if _is_isolatable(finding.check):
            continue  # resolved by the isolated passes below
        _attach(finding, FIELD_BY_CHECK.get(finding.check, "page"))

    # Passes 2-4 — one text field populated at a time, so a slop/voice hit
    # is attributed to the field that actually carried the offending text.
    for field in ("title", "description", "keywords"):
        doc = ContentDoc(
            source="page-inventory", doc_type="strategy_xlsx",
            items=tuple(_item_for_row(r, only_field=field) for r in checked_rows),
        )
        for finding in run_checks(doc, profile):
            if not _is_isolatable(finding.check):
                continue
            _attach(finding, field)

    return QaResult(per_row=per_row, raw=raw)


def group_by_field(findings: list[FieldFinding]) -> dict[str, list[Finding]]:
    grouped: dict[str, list[Finding]] = defaultdict(list)
    for item in findings:
        grouped[item.field].append(item.finding)
    return dict(grouped)
