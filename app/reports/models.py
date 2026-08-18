"""Shared data shapes for the reports engine (task-42 §3.2).

`METRICS` is the single source of truth for order, key and label — the API
path, the PDF path, the table builder, the XLSX and the coverage/cross-check
reports all iterate this instead of each hard-coding their own copy, so they
can never drift on which 8 metrics exist or what order they render in.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PeriodMetrics:
    users: int | None = None
    organic_users: int | None = None
    direct_users: int | None = None
    conversions: int | None = None
    organic_conversions: int | None = None
    direct_conversions: int | None = None
    clicks: int | None = None
    impressions: int | None = None


METRICS: tuple[tuple[str, str], ...] = (
    ("users", "Users"),
    ("organic_users", "Organic Users"),
    ("direct_users", "Direct Users"),
    ("conversions", "Total Conversions"),
    ("organic_conversions", "Organic Conversions"),
    ("direct_conversions", "Direct Conversions"),
    ("clicks", "Clicks"),
    ("impressions", "Impressions"),
)

# Metrics sourced from GSC (task-42 §3.2) — a client without a `gsc`
# integration gets `None` here, never `0` (task-42 §3.4/PLAN §12).
GSC_METRIC_KEYS = frozenset({"clicks", "impressions"})
