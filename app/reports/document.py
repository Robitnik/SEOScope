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

# Cache-buster for the ONE stylesheet this document owns. Live failure it
# exists to prevent (caught 2026-09-07, on the tunnel): the HTML comes from
# Python and is never cached, but Cloudflare serves `/css/*` with
# `max-age=14400` regardless of the app's own headers — so a CSS change
# lands up to 4 hours after the markup that needs it, and the document
# renders as new markup styled by old rules. Bump on every edit to
# `frontend/css/report-doc.css`.
_CSS_VERSION = "20260907-doc2"

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

_CHART_WIDTH = 380
_CHART_HEIGHT = 210

_CHART_METRICS: tuple[tuple[str, str, str], ...] = (
    ("clicks", "Clicks Dynamics", "chart-clicks"),
    ("impressions", "Impressions Dynamics", "chart-impressions"),
    ("organic_conversions", "Organic Conversions Dynamics", "chart-conversions"),
    ("ctr", "CTR Dynamics", "chart-ctr"),
)

# task-85 §1.5/§2.7: the footer line named Reporting Ninja unconditionally,
# which became a lie about the data's own origin the moment task-84 made a
# direct-Google run possible. Keyed by `ReportRun.source`; anything not in
# the map (today: `api`/`pdf`) keeps the original, already-shipped wording.
_SOURCE_FOOTER: dict[str, str] = {"google_api": "direct from Google"}
_SOURCE_FOOTER_DEFAULT = "via Reporting Ninja"

# task-85 §2.6: which period is the OTHER side of each tile-group's
# comparison. The main (displayed) period for each group already exists as
# `_MONTHLY_TILES`/`_YOY_TILES` above — a live-measured incident (an operator
# compared our August document against Reporting Ninja's July screenshot and
# filed it as "data mismatch") showed that a value with no visible baseline
# cannot be checked by anyone without direct DB access.
_COMPARE_PERIODS: dict[str, str] = {
    "Monthly": "Last Month",
    "Quarterly": "Last Quarter",
    "Monthly YOY": "Month YOY",
    "Quarterly YOY": "Quarterly YOY",
}


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


def _tile_html(
    metric_label: str, full_label: str, value: int | None, change: float | None
) -> str:
    """`metric_label` is what the eye reads ("Clicks"); `full_label` is what a
    screen reader reads ("Monthly YOY Clicks").

    They differ on purpose (task-85 §2.3): once every group carries a visible
    `<h3>` + date range, repeating the group name inside all four of its
    tiles is redundant — and it was what wrapped "Quarterly YOY Organic
    Conversions" onto three lines, leaving the values in one row sitting at
    different heights. The period never disappears for assistive tech, it
    just stops being printed four times per block.
    """
    value_cls = "" if value is not None else " report-doc__tile-value--muted"
    return f"""<div class="report-doc__tile">
      <span class="visually-hidden">{escape(full_label)}</span>
      <span class="report-doc__tile-label" aria-hidden="true">{escape(metric_label)}</span>
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
        tiles.append(_tile_html(metric_label, f"{prefix} {metric_label}", value, change))
    return f'<div class="report-doc__tile-group">{"".join(tiles)}</div>'


def _tile_block(
    rows: list[dict[str, Any]],
    periods: list[dict[str, Any]],
    change_labels: list[str],
    *,
    prefix: str,
    period_label: str,
    change_label: str,
    lead: bool = False,
) -> str:
    """One of the four visible groups (Monthly / Quarterly / Monthly YOY /
    Quarterly YOY) — task-85 §1.2/§2.6: a real `<h3>` heading so the eye can
    tell the two 8-tile blocks apart without reading `aria-label`, and the
    date range both sides of the comparison used to reach the numbers below.

    `lead=True` (Monthly only) is §2.3's "obvious in 3 seconds" criterion: a
    brighter surface and a full-strength heading make one cluster the entry
    point, and its first cell is Monthly Clicks. Position alone did not do
    it — all four blocks were the same weight, so the eye had nowhere to land.
    """
    group_html = _tile_group(
        rows, periods, change_labels, prefix=prefix,
        period_label=period_label, change_label=change_label,
    )
    compare_label = _COMPARE_PERIODS[prefix]
    range_label = _comparison_label(periods, period_label, compare_label)
    range_html = (
        f'<p class="report-doc__tile-block-range">{escape(range_label)}</p>' if range_label else ""
    )
    lead_cls = " report-doc__tile-block--lead" if lead else ""
    return f"""<div class="report-doc__tile-block{lead_cls}">
      <h3 class="report-doc__tile-block-title">{escape(prefix)}</h3>
      {range_html}
      {group_html}
    </div>"""


def _history_series(history: dict[str, Any] | None, key: str) -> list[tuple[str, float | None]]:
    months = (history or {}).get("months") or []
    points: list[tuple[str, float | None]] = []
    for month in months:
        label = str(month.get("label", ""))
        short = label.split(" ")[0][:3] if label else ""
        points.append((short, month.get(key)))
    return points


def _chart_card_html(
    history: dict[str, Any] | None, key: str, title: str, chart_id: str, range_label: str
) -> str:
    points = _history_series(history, key)
    formatter = _fmt_ctr_axis if key == "ctr" else (lambda v: f"{v:,.0f}")
    # The 4 charts sit two-up (task-85 §2.3/§2.4), so the viewBox is sized
    # for HALF the page rather than all of it. This matters beyond
    # proportions: the SVG scales to its container, so a 760-wide viewBox in
    # a ~350px column would shrink 10px axis labels to ~4.6px — vector, and
    # unreadable. Sizing the box near its rendered width keeps the type at
    # the size it was chosen to be.
    svg = render_line_chart(
        points, chart_id=chart_id, value_format=formatter, width=_CHART_WIDTH,
        height=_CHART_HEIGHT,
    )
    # task-85 §2.6: without its own range, a chart ending in `… July 2026` is
    # indistinguishable from one ending in the current month — the exact
    # ambiguity that made a stale Reporting Ninja screenshot pass for current
    # data until someone queried the database directly.
    range_html = (
        f'<p class="report-doc__chart-range">{escape(range_label)}</p>' if range_label else ""
    )
    return f"""<div class="report-doc__chart-card">
      <h3 class="report-doc__chart-title">{escape(title)}</h3>
      {range_html}
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


def _range_label(date_from: date, date_to: date) -> str:
    return f"{date_from.strftime('%B %-d')} – {date_to.strftime('%B %-d, %Y')}"


def _period_range_label(periods: list[dict[str, Any]]) -> str:
    bounds = _this_month_dates(periods)
    if bounds is None:
        return ""
    return _range_label(*bounds)


def _period_by_label(periods: list[dict[str, Any]], label: str) -> dict[str, Any] | None:
    return next((p for p in periods if p.get("label") == label), None)


def _period_span_label(period: dict[str, Any] | None) -> str:
    """A single period's own dates, `· partial` appended when it isn't a
    closed period yet (task-85 §2.6) — a partial month shown as if it were a
    complete one is a false comparison, only harder to notice than a missing
    baseline.
    """
    if period is None:
        return ""
    date_from = _as_date(period.get("date_from"))
    date_to = _as_date(period.get("date_to"))
    if date_from is None or date_to is None:
        return ""
    span = _range_label(date_from, date_to)
    return f"{span} · partial" if period.get("is_partial") else span


def _comparison_label(periods: list[dict[str, Any]], main_label: str, compare_label: str) -> str:
    """`Aug 1 – Aug 31, 2026 vs Jul 1 – Jul 31, 2026` — both sides of a
    tile-group's comparison, never just the number being compared (task-85
    §2.6). Empty when either period is absent (a non-monthly run): the tiles
    themselves already render `—` in that case, and an empty label beats a
    half-true one.
    """
    main_span = _period_span_label(_period_by_label(periods, main_label))
    compare_span = _period_span_label(_period_by_label(periods, compare_label))
    if not main_span or not compare_span:
        return ""
    return f"{main_span} vs {compare_span}"


def _history_range_label(history: dict[str, Any] | None) -> str:
    """The chart's own date range, from the first to the last month of its
    series (task-85 §2.6) — `label` is already `"September 2025"`, the same
    format the rest of the document uses, so this reuses it verbatim rather
    than reformatting from `month` ("YYYY-MM").
    """
    months = (history or {}).get("months") or []
    labels = [str(m["label"]) for m in months if m.get("label")]
    if not labels:
        return ""
    first, last = labels[0], labels[-1]
    return first if first == last else f"{first} – {last}"


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
    source: str,
) -> str:
    """`table` is the exact dict shape `GET /runs/{id}/table` serves
    (`ReportTableDTO`, i.e. `run.result_json`) — the document is a new RENDER
    of an already-finished result, never a new collection (task-47 §1.5).
    Any period label the table does not have (a non-`monthly` run) simply
    renders every tile `—`; the Historical Data block is period-independent
    and still renders normally. `source` is `ReportRun.source`
    (`api`/`pdf`/`google_api`, task-84) — it drives the footer's data-source
    line (task-85 §1.5/§2.7) and nothing else; this module never re-derives
    or second-guesses where the numbers came from.
    """
    periods = table.get("periods") or []
    change_labels = table.get("change_labels") or []
    rows = table.get("rows") or []
    history = table.get("history")

    monthly_period, monthly_change = _MONTHLY_TILES[0]
    quarterly_period, quarterly_change = _MONTHLY_TILES[1]
    yoy_monthly_period, yoy_monthly_change = _YOY_TILES[0]
    yoy_quarterly_period, yoy_quarterly_change = _YOY_TILES[1]

    monthly_tiles = _tile_block(
        rows, periods, change_labels, prefix="Monthly",
        period_label=monthly_period, change_label=monthly_change, lead=True,
    )
    quarterly_tiles = _tile_block(
        rows, periods, change_labels, prefix="Quarterly",
        period_label=quarterly_period, change_label=quarterly_change,
    )
    yoy_monthly_tiles = _tile_block(
        rows, periods, change_labels, prefix="Monthly YOY",
        period_label=yoy_monthly_period, change_label=yoy_monthly_change,
    )
    yoy_quarterly_tiles = _tile_block(
        rows, periods, change_labels, prefix="Quarterly YOY",
        period_label=yoy_quarterly_period, change_label=yoy_quarterly_change,
    )

    chart_range = _history_range_label(history)
    charts = "".join(
        _chart_card_html(history, key, title, chart_id, chart_range)
        for key, title, chart_id in _CHART_METRICS
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
    footer_source = _SOURCE_FOOTER.get(source, _SOURCE_FOOTER_DEFAULT)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="author" content="The Yovth">
<title>{escape(doc_title)}</title>
<link rel="stylesheet" href="/css/tokens.css">
<link rel="stylesheet" href="/css/report-doc.css?v={_CSS_VERSION}">
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

    <section class="report-doc__tiles report-doc__tiles--primary"
      aria-label="Monthly and quarterly summary">
      <h2 class="report-doc__section-title">Monthly &amp; Quarterly</h2>
      <div class="report-doc__tile-blocks">
        {monthly_tiles}
        {quarterly_tiles}
      </div>
    </section>

    <section class="report-doc__charts" aria-label="Monthly trends">
      {charts}
    </section>

    <section class="report-doc__tiles report-doc__tiles--secondary"
      aria-label="Year over year summary">
      <h2 class="report-doc__section-title">Year over Year</h2>
      <div class="report-doc__tile-blocks">
        {yoy_monthly_tiles}
        {yoy_quarterly_tiles}
      </div>
    </section>

    <footer class="report-doc__footer">
      <p>Data source: Google Search Console + GA4 {escape(footer_source)}</p>
      <p>Generated {escape(generated_line)} · Generated by The Yovth</p>
      {f'<p>{escape(cache_line)}</p>' if cache_line else ""}
    </footer>
  </div>
</main>
</body>
</html>"""
