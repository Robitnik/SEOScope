"""The client-facing document (task-47 §2.1/§3.2): one self-contained HTML
page rendered server-side from a run's own `result_json` (the exact same
data `GET /runs/{id}/table` already serves — no new collection, §1.5).

This is the single source of markup for BOTH surfaces the operator sees:
`GET /reports/runs/{id}/document` renders it for the `<iframe>` preview, and
`app.reports.client_pdf` drives Chromium against that exact URL. There is no
second template — a rendered screen and a rendered PDF can never drift
against each other because there is only one thing being rendered (task-47
§2.1 "Один рендерер — два виходи").

Hard constraints, not preferences (task-47 §1.2, a live-measured CSP
failure): no inline `<style>`, no inline/`<script>` tag anywhere in this
markup — `frontend/css/report-doc.css` carries every rule, and the 4 charts
are inline SVG (`app.reports.svg_charts`), not a canvas needing JS to paint.
`None` values render as `—`, never `0` (PLAN §12) — a tile with `—` proves
the metric was not collected, not that it dropped to zero.
"""

from __future__ import annotations

from datetime import date, datetime
from html import escape
from typing import Any

from app.reports.svg_charts import render_line_chart

# The 4 metrics the document highlights, in the order the hand-kept sheet
# used (task-47 §1.5) — a subset of `app.reports.models.METRICS`. All four
# are "more is better": no `better_direction=down` metric belongs in a
# client-facing report, so the up/down arrow can be driven by sign alone
# HERE ONLY (PLAN §12's ban on sign-only color is about metrics in general,
# not this fixed, all-up quartet).
_TILE_METRICS: tuple[tuple[str, str], ...] = (
    ("clicks", "Clicks"),
    ("impressions", "Impressions"),
    ("organic_users", "Organic Users"),
    ("organic_conversions", "Organic Conversions"),
)

# (tile period label, change label) pairs for the Monthly/Quarterly block —
# the SAME columns `app.reports.table._MONTHLY_CHANGES` already computes.
_MONTHLY_TILES: tuple[tuple[str, str], ...] = (("This Month", "% M/M"), ("This Quarter", "% Q/Q"))
_YOY_TILES: tuple[tuple[str, str], ...] = (("This Month", "% M YOY"), ("This Quarter", "% Q YOY"))

_CHART_METRICS: tuple[tuple[str, str, str], ...] = (
    ("clicks", "Clicks Dynamics", "chart-clicks"),
    ("impressions", "Impressions Dynamics", "chart-impressions"),
    ("organic_conversions", "Organic Conversions Dynamics", "chart-conversions"),
    ("ctr", "CTR Dynamics", "chart-ctr"),
)


def _fmt_int(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "+" if value > 0 else ""
    return f"{sign}{value * 100:.2f}%"


def _fmt_ctr(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def _fmt_ctr_axis(value: float) -> str:
    return f"{value * 100:.1f}%"


def _period_values(periods: list[dict[str, Any]], row_values: list[Any], label: str) -> Any:
    for period, value in zip(periods, row_values, strict=True):
        if period.get("label") == label:
            return value
    return None


def _change_value(change_labels: list[str], changes: list[Any], label: str) -> Any:
    for change_label, value in zip(change_labels, changes, strict=True):
        if change_label == label:
            return value
    return None


def _row_for(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    for row in rows:
        if row.get("key") == key:
            return row
    return None


def _delta_direction(value: float | None) -> str:
    if value is None:
        return "flat"
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "flat"


_ARROW_PATHS = {
    "up": "M6 2 L10 8 L7.2 8 L7.2 14 L4.8 14 L4.8 8 L2 8 Z",
    "down": "M6 14 L2 8 L4.8 8 L4.8 2 L7.2 2 L7.2 8 L10 8 Z",
    "flat": "M2 7 H10 V9 H2 Z",
}


def _delta_html(value: float | None) -> str:
    # `None` means "nothing to compare" (no data, or a zero/missing base —
    # PLAN §12), which is a different fact from "measured, and it didn't
    # move" (`flat`, an SVG bar + the word "No change"). Showing that icon
    # next to a plain `—` would claim a measurement that was never made.
    if value is None:
        return '<span class="report-doc__delta report-doc__delta--flat">—</span>'
    direction = _delta_direction(value)
    word = {"up": "Up", "down": "Down", "flat": "No change"}[direction]
    return (
        f'<span class="report-doc__delta report-doc__delta--{direction}">'
        f'<svg class="report-doc__arrow" width="12" height="16" viewBox="0 0 12 16" '
        f'aria-hidden="true"><path d="{_ARROW_PATHS[direction]}" fill="currentColor"/></svg>'
        f'<span class="visually-hidden">{word}</span>{escape(_fmt_pct(value))}</span>'
    )


def _tile_html(label: str, value: int | None, change: float | None) -> str:
    value_cls = "" if value is not None else " report-doc__tile-value--muted"
    return f"""<div class="report-doc__tile">
      <span class="report-doc__tile-label">{escape(label)}</span>
      <strong class="report-doc__tile-value{value_cls}">{escape(_fmt_int(value))}</strong>
      {_delta_html(change)}
    </div>"""


def _tile_group(
    rows: list[dict[str, Any]],
    periods: list[dict[str, Any]],
    change_labels: list[str],
    *,
    prefix: str,
    period_label: str,
    change_label: str,
) -> str:
    tiles = []
    for key, metric_label in _TILE_METRICS:
        row = _row_for(rows, key)
        value = _period_values(periods, row["values"], period_label) if row else None
        change = _change_value(change_labels, row["changes"], change_label) if row else None
        tiles.append(_tile_html(f"{prefix} {metric_label}", value, change))
    return f'<div class="report-doc__tile-group">{"".join(tiles)}</div>'


def _history_series(history: dict[str, Any] | None, key: str) -> list[tuple[str, float | None]]:
    months = (history or {}).get("months") or []
    points: list[tuple[str, float | None]] = []
    for month in months:
        label = str(month.get("label", ""))
        short = label.split(" ")[0][:3] if label else ""
        points.append((short, month.get(key)))
    return points


def _chart_card_html(history: dict[str, Any] | None, key: str, title: str, chart_id: str) -> str:
    points = _history_series(history, key)
    formatter = _fmt_ctr_axis if key == "ctr" else (lambda v: f"{v:,.0f}")
    svg = render_line_chart(points, chart_id=chart_id, value_format=formatter)
    return f"""<div class="report-doc__chart-card">
      <h2 class="report-doc__chart-title">{escape(title)}</h2>
      {svg}
    </div>"""


def _this_month_dates(periods: list[dict[str, Any]]) -> tuple[date, date] | None:
    this_month = next((p for p in periods if p.get("label") == "This Month"), None)
    if this_month is None or not this_month.get("date_from") or not this_month.get("date_to"):
        return None
    date_from = _as_date(this_month["date_from"])
    date_to = _as_date(this_month["date_to"])
    if date_from is None or date_to is None:
        return None
    return date_from, date_to


def _period_range_label(periods: list[dict[str, Any]]) -> str:
    bounds = _this_month_dates(periods)
    if bounds is None:
        return ""
    date_from, date_to = bounds
    return f"{date_from.strftime('%B %-d')} – {date_to.strftime('%B %-d, %Y')}"


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def render_document_html(
    *,
    client_name: str,
    table: dict[str, Any],
    generated_at: datetime,
) -> str:
    """`table` is the exact dict shape `GET /runs/{id}/table` serves
    (`ReportTableDTO`, i.e. `run.result_json`) — the document is a new RENDER
    of an already-finished result, never a new collection (task-47 §1.5).
    Any period label the table does not have (a non-`monthly` run) simply
    renders every tile `—`; the Historical Data block is period-independent
    and still renders normally.
    """
    periods = table.get("periods") or []
    change_labels = table.get("change_labels") or []
    rows = table.get("rows") or []
    history = table.get("history")

    monthly_period, monthly_change = _MONTHLY_TILES[0]
    quarterly_period, quarterly_change = _MONTHLY_TILES[1]
    yoy_monthly_period, yoy_monthly_change = _YOY_TILES[0]
    yoy_quarterly_period, yoy_quarterly_change = _YOY_TILES[1]

    monthly_tiles = _tile_group(
        rows, periods, change_labels, prefix="Monthly",
        period_label=monthly_period, change_label=monthly_change,
    )
    quarterly_tiles = _tile_group(
        rows, periods, change_labels, prefix="Quarterly",
        period_label=quarterly_period, change_label=quarterly_change,
    )
    yoy_monthly_tiles = _tile_group(
        rows, periods, change_labels, prefix="Monthly YOY",
        period_label=yoy_monthly_period, change_label=yoy_monthly_change,
    )
    yoy_quarterly_tiles = _tile_group(
        rows, periods, change_labels, prefix="Quarterly YOY",
        period_label=yoy_quarterly_period, change_label=yoy_quarterly_change,
    )

    charts = "".join(
        _chart_card_html(history, key, title, chart_id) for key, title, chart_id in _CHART_METRICS
    )

    period_range = _period_range_label(periods)
    subtitle = f"Monthly report · {period_range}" if period_range else "Monthly report"
    generated_line = generated_at.strftime("%B %-d, %Y")
    # task-49 §2.6: the client sees the same data-truth requirement `None !=
    # 0` already gives every metric — a report built on a reused PDF is not
    # the same thing as a fresh collection, and that difference belongs in
    # the document itself, not only in the operator's own UI.
    oldest_cached_at = table.get("oldest_cached_at")
    cache_line = None
    if oldest_cached_at:
        cache_date = date.fromisoformat(oldest_cached_at).strftime("%B %-d, %Y")
        cache_line = f"Source PDFs downloaded {cache_date}"

    bounds = _this_month_dates(periods)
    title_stamp = f", {bounds[0].strftime('%B %Y')}" if bounds else ""
    # Chromium's headless print-to-PDF reads `<title>`/`<meta name="author">`
    # straight into the PDF's own Title/Author metadata (task-47 §3.3) — no
    # PDF-editing library needed for two fields Chromium already sets.
    doc_title = f"{client_name} — Monthly report{title_stamp}"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="author" content="The Yovth">
<title>{escape(doc_title)}</title>
<link rel="stylesheet" href="/css/tokens.css">
<link rel="stylesheet" href="/css/report-doc.css">
</head>
<body class="report-doc-body">
<main class="report-doc">
  <div class="report-doc__page">
    <header class="report-doc__header">
      <div class="report-doc__identity">
        <h1 translate="no">{escape(client_name)}</h1>
        <p class="report-doc__subtitle">{escape(subtitle)}</p>
      </div>
      <img class="report-doc__logo" src="/assets/brand/logo.svg" alt="The Yovth"
           width="168" height="21">
    </header>

    <section class="report-doc__tiles" aria-label="Monthly and quarterly summary">
      {monthly_tiles}
      {quarterly_tiles}
    </section>

    <section class="report-doc__charts" aria-label="Monthly trends">
      {charts}
    </section>

    <section class="report-doc__tiles" aria-label="Year over year summary">
      {yoy_monthly_tiles}
      {yoy_quarterly_tiles}
    </section>

    <footer class="report-doc__footer">
      <p>Data source: Google Search Console + GA4 via Reporting Ninja</p>
      <p>Generated {escape(generated_line)} · Generated by The Yovth</p>
      {f'<p>{escape(cache_line)}</p>' if cache_line else ""}
    </footer>
  </div>
</main>
</body>
</html>"""
