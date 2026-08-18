"""Build the 8-metric x N-column table (task-42 §3.2/§3.3), independent of
`kind` and `source` — the API path and the PDF path both hand this the same
shape (`{period_label: PeriodMetrics}`) and get the same table back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.reports.history import ReportHistory
from app.reports.models import METRICS, PeriodMetrics
from app.reports.periods import ReportPeriod

_MONTHLY_CHANGES = (
    ("% M/M", "This Month", "Last Month"),
    ("% Q/Q", "This Quarter", "Last Quarter"),
    ("% M YOY", "This Month", "Month YOY"),
    ("% Q YOY", "This Quarter", "Quarterly YOY"),
)


def safe_pct(current: int | None, previous: int | None) -> float | None:
    """`(current - previous) / previous`, or `None` when it isn't a number
    (PLAN §12: a `0`/`null` previous returns `null` with a reason, never
    infinity — the caller is that reason: there is nothing else to compute).
    """
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / previous


@dataclass(frozen=True)
class MetricRowResult:
    key: str
    label: str
    values: list[int | None]
    changes: list[float | None]


@dataclass(frozen=True)
class ReportTable:
    periods: list[ReportPeriod]
    change_labels: list[str]
    # `(newer_period_label, older_period_label)`, aligned with `change_labels`
    # — lets `xlsx.py` write a live `=IFERROR((newer-older)/older,"")` formula
    # by column letter without recomputing which periods a change compares.
    change_pairs: list[tuple[str, str]]
    rows: list[MetricRowResult]
    # PDF path only: {period_label: source PDF filename}. Empty for the API
    # path — task-42 §5 "походження кожної клітинки" is per-PERIOD (which
    # file a whole column was read from), not per metric cell.
    sources: dict[str, str] = field(default_factory=dict)
    # The monthly history block (task-45 §2.4), attached by the runner after
    # the table itself is built — it comes from its own two queries and never
    # depends on which periods this table has. `None` means "the history query
    # could not run", which the UI and the XLSX sheet both say out loud.
    history: ReportHistory | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe shape for `ReportRun.result_json` and `GET /runs/{id}/table`."""
        return {
            "periods": [
                {
                    "label": p.label,
                    "date_from": p.date_from.isoformat(),
                    "date_to": p.date_to.isoformat(),
                    "is_partial": p.is_partial,
                }
                for p in self.periods
            ],
            "change_labels": self.change_labels,
            "rows": [
                {"key": r.key, "label": r.label, "values": r.values, "changes": r.changes}
                for r in self.rows
            ],
            "sources": self.sources,
            "history": self.history.to_dict() if self.history is not None else None,
        }


def _changes_for(kind: str, periods: list[ReportPeriod]) -> list[tuple[str, str, str]]:
    """`[(change_label, newer_period_label, older_period_label), ...]`."""
    if kind == "monthly":
        present = {p.label for p in periods}
        return [c for c in _MONTHLY_CHANGES if c[1] in present and c[2] in present]
    return [
        (f"{periods[i].label} vs {periods[i + 1].label}", periods[i].label, periods[i + 1].label)
        for i in range(len(periods) - 1)
    ]


def build_table(
    kind: str,
    periods: list[ReportPeriod],
    metrics_by_label: dict[str, PeriodMetrics],
    *,
    sources: dict[str, str] | None = None,
) -> ReportTable:
    changes = _changes_for(kind, periods)
    rows = []
    for key, label in METRICS:
        values = [getattr(metrics_by_label.get(p.label), key, None) for p in periods]
        by_label = {p.label: v for p, v in zip(periods, values, strict=True)}
        row_changes = [safe_pct(by_label[newer], by_label[older]) for _, newer, older in changes]
        rows.append(MetricRowResult(key=key, label=label, values=values, changes=row_changes))

    return ReportTable(
        periods=periods,
        change_labels=[c[0] for c in changes],
        change_pairs=[(c[1], c[2]) for c in changes],
        rows=rows,
        sources=dict(sources or {}),
    )
