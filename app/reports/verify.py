"""Cross-check: prove the PDF path and the API path agree (task-42 §3.6).

Two independent sources read the same date range; every one of the 8 x N
cells is compared. The verdict is DATA, not an exit code (unlike the ported
`pdf_verify.py`'s self-consistency check, which only proved a PDF and its own
XLSX agreed with each other): a run never fails because of a mismatch — the
discrepancy itself is the useful signal, so it is stored and shown, not
swallowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.reports.models import METRICS, PeriodMetrics
from app.reports.periods import ReportPeriod

# task-48 §2.3: `no_data`/`missing_api`/`missing_pdf` split what used to be
# one lying `match` (both `None` — the PDF said `0`-as-empty, task-48 §1.3)
# and one lying `mismatch` (one side `None`) into verdicts that say what
# actually happened, so the run's headline count means what a human reads
# into it (task-48 §3.1: "44 match · 4 no data · 0 differ", never "44
# values differ" for a period neither source had anything to report).
Verdict = str  # match | within_tolerance | mismatch | no_data | missing_api | missing_pdf

# The "differ" bucket for banners/summaries (task-48 §2.3/§3.1) — a genuine
# one-sided or numeric disagreement, as opposed to `no_data` (nothing to
# compare) or `match`/`within_tolerance` (agreement).
DIFFER_VERDICTS = frozenset({"mismatch", "missing_api", "missing_pdf"})


@dataclass(frozen=True)
class CellComparison:
    period_label: str
    metric_key: str
    metric_label: str
    pdf_value: int | None
    api_value: int | None
    diff_pct: float | None
    verdict: Verdict

    def describe(self) -> str:
        if self.verdict == "no_data":
            return f"{self.metric_label}, {self.period_label}: no data in either source"
        if self.verdict == "match":
            return f"{self.metric_label}, {self.period_label}: PDF and API agree ({self.pdf_value})"
        if self.diff_pct is None:
            return (
                f"{self.metric_label}, {self.period_label}: PDF {self.pdf_value!r} vs "
                f"API {self.api_value!r} — one side has no value"
            )
        sign = "+" if (self.pdf_value or 0) >= (self.api_value or 0) else "-"
        return (
            f"{self.metric_label}, {self.period_label}: PDF {self.pdf_value} vs "
            f"API {self.api_value} ({sign}{self.diff_pct * 100:.1f}%)"
        )


@dataclass(frozen=True)
class CrossCheckResult:
    status: Verdict
    cells: list[CellComparison]

    @property
    def mismatches(self) -> list[CellComparison]:
        return [c for c in self.cells if c.verdict == "mismatch"]

    @property
    def differs(self) -> list[CellComparison]:
        """Every genuine disagreement (task-48 §2.3/§3.1) — numeric
        `mismatch` plus the one-sided `missing_api`/`missing_pdf` cells,
        as opposed to `no_data` (nothing to compare in either source).
        """
        return [c for c in self.cells if c.verdict in DIFFER_VERDICTS]

    @property
    def no_data_cells(self) -> list[CellComparison]:
        return [c for c in self.cells if c.verdict == "no_data"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "cells": [
                {
                    "period_label": c.period_label,
                    "metric_key": c.metric_key,
                    "metric_label": c.metric_label,
                    "pdf_value": c.pdf_value,
                    "api_value": c.api_value,
                    "diff_pct": c.diff_pct,
                    "verdict": c.verdict,
                }
                for c in self.cells
            ],
        }


def _compare_cell(
    period_label: str,
    key: str,
    label: str,
    pdf_value: int | None,
    api_value: int | None,
    tolerance: float,
) -> CellComparison:
    # Both `None`: neither source had anything to say about this period
    # (task-48 §1.3/§2.3) — not a `match` (nothing was actually compared)
    # and not a `mismatch` (nothing actually disagrees either).
    if pdf_value is None and api_value is None:
        return CellComparison(period_label, key, label, pdf_value, api_value, None, "no_data")
    # Exactly one side is `None`: a real gap, named by WHICH side is
    # missing (task-48 §2.3) rather than one generic `mismatch`.
    if pdf_value is None:
        return CellComparison(period_label, key, label, pdf_value, api_value, None, "missing_pdf")
    if api_value is None:
        return CellComparison(period_label, key, label, pdf_value, api_value, None, "missing_api")
    if pdf_value == api_value:
        return CellComparison(period_label, key, label, pdf_value, api_value, 0.0, "match")

    base = api_value if api_value else pdf_value
    diff_pct = abs(pdf_value - api_value) / base if base else 1.0
    verdict: Verdict = "within_tolerance" if diff_pct * 100 <= tolerance else "mismatch"
    return CellComparison(period_label, key, label, pdf_value, api_value, diff_pct, verdict)


def cross_check(
    periods: list[ReportPeriod],
    pdf_metrics: dict[str, PeriodMetrics],
    api_metrics: dict[str, PeriodMetrics],
    *,
    tolerance_pct: Decimal | float = Decimal("0.5"),
) -> CrossCheckResult:
    """Compare every (period, metric) cell the PDF path and the API path both
    produced for the same date range. `tolerance_pct` is a percentage
    (`0.5` = 0.5%), the "freshness, not disagreement" allowance (task-42 §3.6).
    """
    tolerance = float(tolerance_pct)
    cells = [
        _compare_cell(
            period.label,
            key,
            label,
            getattr(pdf_metrics.get(period.label), key, None),
            getattr(api_metrics.get(period.label), key, None),
            tolerance,
        )
        for period in periods
        for key, label in METRICS
    ]

    # `no_data` cells never move the overall status — a period neither
    # source had anything for is not a disagreement (task-48 §1.3/§2.3).
    if any(c.verdict in DIFFER_VERDICTS for c in cells):
        status: Verdict = "mismatch"
    elif any(c.verdict == "within_tolerance" for c in cells):
        status = "within_tolerance"
    else:
        status = "match"
    return CrossCheckResult(status=status, cells=cells)
