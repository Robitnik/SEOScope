"""Parse a downloaded Reporting Ninja PDF into the 8 metrics (task-42 §3.2).

Ported from `AISeoTools/.../clientreport/pdf_parser.py`, unchanged in
substance — `pdftotext -layout` preserves column positions, so a numbered
data row is split on 2+ spaces. Organic/Direct Users and Conversions come
from the SAME "First user default channel grouping" table row the PDF
itself prints, so the PDF path's attribution model is whatever the PDF says
it is — task-42 §1.3 only found a bug in the API path's own field/dimension
choice, not in how this parser reads the document.

Generalized from the original's fixed 6-slug lookup to an arbitrary period
via `app.reports.naming` (task-42 §2.4: 4 kinds, not just `monthly`).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from app.reports.models import METRICS, PeriodMetrics

# Marks the GSC "Overall performance" page inside the concatenated PDF text.
# A Reporting Ninja label rename becomes a one-line fix here instead of a
# silent "clicks/impressions came back empty".
_GSC_PAGE_MARKERS = ("Google SEO", "Google Console")


class PdfDownloadIncomplete(RuntimeError):
    """A saved PDF is empty or truncated — never fed to the parser as-is."""


MIN_PDF_BYTES = 50_000


def assert_pdf_complete(path: Path) -> None:
    size = path.stat().st_size if path.exists() else 0
    if size < MIN_PDF_BYTES:
        raise PdfDownloadIncomplete(
            f"{path.name}: {size} B — file is empty or truncated "
            f"(expected >= {MIN_PDF_BYTES // 1024} KB)"
        )


def _n(value: str) -> int:
    return int(value.replace(",", ""))


def _pdf_text(path: Path) -> str:
    return subprocess.check_output(
        ["pdftotext", "-layout", str(path), "-"], stderr=subprocess.DEVNULL
    ).decode("utf-8", errors="ignore")


def _overall_metric(text: str, metric: str) -> int | None:
    """First value on a metric row in the GA4 "Overall performance" table.

    Row: "Total users   98,063   48,120   103.79%   25,058   291.34%" — the
    first number is always the current-period value.
    """
    match = re.search(rf"^{re.escape(metric)}\s+([\d,]+)", text, re.MULTILINE)
    return _n(match.group(1)) if match else None


def _channel_row(text: str, channel: str) -> tuple[int | None, int | None]:
    """`(total_users, key_events)` from a numbered channel-grouping row.

    Scans the WHOLE document top-to-bottom and returns the first match: the
    "First user default channel grouping" table always precedes the
    "Session primary channel group" table (which reuses the same channel
    names) later in the document, so the first hit is always the right one.
    """
    for line in text.splitlines():
        if channel not in line:
            continue
        if not re.match(r"\s*\d+\s", line):
            continue
        parts = re.split(r"\s{2,}", line.strip())
        numbers = [p for p in parts[2:] if re.match(r"^\d[\d,]*$", p)]
        if len(numbers) >= 2:
            return _n(numbers[0]), _n(numbers[-1])
    return None, None


def _gsc_metric(pages: list[str], metric: str) -> int | None:
    gsc_page = next((p for p in pages if any(marker in p for marker in _GSC_PAGE_MARKERS)), "")
    match = re.search(rf"^{re.escape(metric)}\s+([\d,]+)", gsc_page, re.MULTILINE)
    return _n(match.group(1)) if match else None


def _period_has_no_data(
    *, users: int | None, conversions: int | None, organic_users: int | None,
    direct_users: int | None,
) -> bool:
    """A period the GA4 property had no data for (task-48 §1.3/§2.3), live
    example: EFFYS's `Month YOY`/`Quarterly YOY` columns predate the
    property's creation.

    Reporting Ninja's PDF encodes "no data" two ways in the SAME document at
    once: the "First user default channel grouping" table (which feeds
    `organic_users`/`direct_users`) prints "There are no results" -> no rows
    -> already `None` via `_channel_row`; the "Overall performance" table
    (which feeds `users`/`conversions`) instead prints a literal `0`. Only
    the combination of BOTH signals means "no data" — a channel table can
    legitimately be sparse (e.g. zero Direct traffic) while Overall
    performance still reports real, non-zero totals, and that combination
    must keep its `0` (task-48 §1.3: "справжній нуль лишається 0").
    """
    return organic_users is None and direct_users is None and users == 0 and conversions == 0


def parse_pdf(pdf_path: Path) -> PeriodMetrics:
    """Parse one Reporting Ninja PDF into the 8 metrics for its period."""
    text = _pdf_text(pdf_path)
    pages = text.split("\x0c")

    organic_users, organic_conversions = _channel_row(text, "Organic Search")
    direct_users, direct_conversions = _channel_row(text, "Direct")
    users = _overall_metric(text, "Total users")
    conversions = _overall_metric(text, "Key events")
    clicks = _gsc_metric(pages, "Clicks")
    impressions = _gsc_metric(pages, "Impressions")

    # The period-level flag, not a per-cell one (task-48 §2.3): once a period
    # is established as having no GA4 data at all, a GSC `0` printed for the
    # SAME date range is that same "no data" story, not an independent real
    # zero (task-48 §1.4) — a non-zero GSC value in a "no data" period is
    # left untouched rather than assumed to be part of the same story.
    if _period_has_no_data(
        users=users, conversions=conversions, organic_users=organic_users,
        direct_users=direct_users,
    ):
        users = None if users == 0 else users
        conversions = None if conversions == 0 else conversions
        clicks = None if clicks == 0 else clicks
        impressions = None if impressions == 0 else impressions

    return PeriodMetrics(
        users=users,
        organic_users=organic_users,
        direct_users=direct_users,
        conversions=conversions,
        organic_conversions=organic_conversions,
        direct_conversions=direct_conversions,
        clicks=clicks,
        impressions=impressions,
    )


# Present on every real Reporting Ninja report page regardless of whether
# any WIDGET on it actually had data — the GA4 section header always
# renders, and one of the GSC page markers always renders. Distinguishes a
# genuinely-empty period (task-48 §2.3: `parse_pdf` correctly nulled every
# metric) from a broken/mismatched PDF that never had real report pages to
# read in the first place — `metric_coverage` alone cannot tell these apart,
# since both produce "0 metrics found" (live regression, 2026-08-19: EFFYS's
# real Month YOY/Quarterly YOY periods — genuinely no GA4/GSC data before the
# property existed — were being discarded as parse FAILURES).
_REPORT_PAGE_MARKER = "Google Analytics"


def looks_like_a_report_pdf(pdf_path: Path) -> bool:
    """True if this PDF has the Reporting Ninja report's own page chrome —
    proof the download/render succeeded, independent of whether the metrics
    on it parsed to real numbers, `None` (task-45 §1.6 "no rows"), or `None`
    via the no-data normalization above (task-48 §2.3).
    """
    text = _pdf_text(pdf_path)
    return _REPORT_PAGE_MARKER in text and any(marker in text for marker in _GSC_PAGE_MARKERS)


def metric_coverage(metrics: PeriodMetrics) -> tuple[list[str], list[str]]:
    """`(found, missing)` human labels — the QA gate's evidence (task-42
    §3.7.8): a period with an empty `missing` parsed clean; a period whose
    `found` is empty parsed NOTHING (wrong PDF, or the layout drifted) and
    must be raised loudly, never shipped as a column of silent "—".
    """
    found: list[str] = []
    missing: list[str] = []
    for key, label in METRICS:
        (found if getattr(metrics, key) is not None else missing).append(label)
    return found, missing
