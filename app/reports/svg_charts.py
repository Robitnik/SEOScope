"""Inline SVG line/area charts for the client document (task-47 §1.3/§2.1).

Chart.js was the first prototype and it failed outright under the app's CSP
(`default-src 'self'`, no `unsafe-inline`) — a `<canvas>` driven by an inline
`<script>` painted nothing (task-47 §1.2, a live-measured failure, not a
guess). Presentational SVG attributes (`fill="…"`, `stroke="…"`) are not
`style=` and are not scripts, so they render under the same policy without
any CSP change — and they stay vector at 400% zoom, which a canvas raster
never would (task-47 §7 gate).

`None` in the input is a genuine data gap (a month the provider has no row
for, PLAN §12) — the line breaks there instead of dropping to 0, which is
why points are grouped into contiguous runs rather than joined by one path.
"""

from __future__ import annotations

from collections.abc import Callable
from html import escape

_WIDTH = 760
_HEIGHT = 200
_PAD_LEFT = 52
_PAD_RIGHT = 12
_PAD_TOP = 16
_PAD_BOTTOM = 28
_GRIDLINES = 4


def _default_format(value: float) -> str:
    return f"{value:,.0f}"


def _empty_chart_svg(labels: list[str], *, chart_id: str, width: int, height: int) -> str:
    """No usable data at all — every point `None`, or every present value is
    0 (task-47 §7: "максимум 0 -> графік із порожнім станом, не ділення на
    нуль"). A flat axis with the real month labels, never a fabricated curve.

    Tick/empty-state text is `#aaa6a0` (`--ink-muted`), not `--ink-dim`
    (`#77736e`, task-47's original choice): measured at 3.54:1 on
    `--surface`, `--ink-dim` fails WCAG AA (4.5:1) at these font sizes
    (task-85 §2.5) — `--ink-muted` clears it at 6.88:1.
    """
    axis_y = height - _PAD_BOTTOM
    ticks = _x_ticks(labels, width)
    return f"""<svg class="report-doc-chart" viewBox="0 0 {width} {height}" role="img"
     aria-label="No data available for this range" data-chart-id="{escape(chart_id)}">
  <line x1="{_PAD_LEFT}" y1="{axis_y}" x2="{width - _PAD_RIGHT}" y2="{axis_y}"
        stroke="#383838" stroke-width="1"/>
  {ticks}
  <text x="{width / 2:.1f}" y="{(_PAD_TOP + axis_y) / 2:.1f}" text-anchor="middle"
        class="report-doc-chart__empty" fill="#aaa6a0" font-size="13">No data for this range</text>
</svg>"""


def _x_ticks(labels: list[str], width: int) -> str:
    n = len(labels)
    if n == 0:
        return ""
    step = (width - _PAD_LEFT - _PAD_RIGHT) / max(1, n - 1)
    y = _HEIGHT - _PAD_BOTTOM + 16
    parts = []
    for i, label in enumerate(labels):
        x = _PAD_LEFT + i * step
        parts.append(
            f'<text x="{x:.1f}" y="{y}" text-anchor="middle" '
            f'class="report-doc-chart__tick" fill="#aaa6a0" font-size="10">{escape(label)}</text>'
        )
    return "\n  ".join(parts)


def render_line_chart(
    points: list[tuple[str, float | None]],
    *,
    chart_id: str,
    value_format: Callable[[float], str] = _default_format,
    width: int = _WIDTH,
    height: int = _HEIGHT,
) -> str:
    """One area/line chart, oldest-first `points`.

    `chart_id` seeds the gradient's `id` — an SVG `<defs>` id is document-wide,
    so two charts on the same page with the same literal id would silently
    reuse (or overwrite) each other's fill.
    """
    labels = [label for label, _ in points]
    known = [(i, v) for i, (_, v) in enumerate(points) if v is not None]
    if not known or max(v for _, v in known) <= 0:
        return _empty_chart_svg(labels, chart_id=chart_id, width=width, height=height)

    n = len(points)
    plot_w = width - _PAD_LEFT - _PAD_RIGHT
    plot_h = height - _PAD_TOP - _PAD_BOTTOM
    x_step = plot_w / max(1, n - 1)

    values = [v for _, v in known]
    lo, hi = min(values), max(values)
    flat = hi == lo

    def x_at(i: int) -> float:
        return _PAD_LEFT + i * x_step

    def y_at(v: float) -> float:
        if flat:
            return _PAD_TOP + plot_h * 0.5
        return _PAD_TOP + plot_h * (1 - (v - lo) / (hi - lo))

    # Contiguous runs of known points — a gap (`None`) ends one run and
    # starts the next, so the rendered line breaks there rather than
    # connecting straight across a month with no data (task-47 §3.2/PLAN §12).
    runs: list[list[tuple[int, float]]] = []
    current: list[tuple[int, float]] = []
    for i, (_, value) in enumerate(points):
        if value is None:
            if current:
                runs.append(current)
                current = []
            continue
        current.append((i, value))
    if current:
        runs.append(current)

    gradient_id = f"chart-fill-{escape(chart_id)}"
    lines = []
    areas = []
    dots = []
    for run in runs:
        coords = [(x_at(i), y_at(v)) for i, v in run]
        path_d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in coords)
        lines.append(f'<path d="{path_d}" fill="none" stroke="#ff4a17" stroke-width="2.5" '
                      'stroke-linecap="round" stroke-linejoin="round"/>')
        if len(coords) >= 2:
            baseline = height - _PAD_BOTTOM
            area_d = (
                path_d
                + f" L {coords[-1][0]:.1f} {baseline} L {coords[0][0]:.1f} {baseline} Z"
            )
            areas.append(f'<path d="{area_d}" fill="url(#{gradient_id})" stroke="none"/>')
        for x, y in coords:
            dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="#ff4a17"/>')

    # Y gridlines + value labels — real ticks, not a decorative frame; a
    # reader must be able to read an approximate value without hovering
    # (there is no hover in a PDF).
    grid_lines = []
    grid_labels = []
    for step in range(_GRIDLINES + 1):
        frac = step / _GRIDLINES
        y = _PAD_TOP + plot_h * frac
        value = hi - (hi - lo) * frac if not flat else lo
        grid_lines.append(
            f'<line x1="{_PAD_LEFT}" y1="{y:.1f}" x2="{width - _PAD_RIGHT}" y2="{y:.1f}" '
            'stroke="#2a2a2a" stroke-width="1"/>'
        )
        grid_labels.append(
            f'<text x="{_PAD_LEFT - 8}" y="{y + 3:.1f}" text-anchor="end" '
            f'class="report-doc-chart__tick" fill="#aaa6a0" font-size="10">'
            f'{escape(value_format(value))}</text>'
        )

    return f"""<svg class="report-doc-chart" viewBox="0 0 {width} {height}" role="img"
     aria-label="{escape(_chart_summary(points, value_format))}" data-chart-id="{escape(chart_id)}">
  <defs>
    <linearGradient id="{gradient_id}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#ff4a17" stop-opacity="0.55"/>
      <stop offset="100%" stop-color="#ff4a17" stop-opacity="0.02"/>
    </linearGradient>
  </defs>
  {"".join(grid_lines)}
  {"".join(areas)}
  {"".join(lines)}
  {"".join(dots)}
  {"".join(grid_labels)}
  {_x_ticks(labels, width)}
</svg>"""


def _chart_summary(
    points: list[tuple[str, float | None]], value_format: Callable[[float], str]
) -> str:
    """A text summary for `aria-label`/screen readers (frontend/AGENTS.md §7:
    "Charts require accessible text summaries") — the SVG itself carries no
    other text alternative in a PDF, where there is no adjacent data table.
    """
    known = [(label, v) for label, v in points if v is not None]
    if not known:
        return "No data available for this range"
    first_label, first_value = known[0]
    last_label, last_value = known[-1]
    return (
        f"From {first_label} ({value_format(first_value)}) "
        f"to {last_label} ({value_format(last_value)})"
    )
