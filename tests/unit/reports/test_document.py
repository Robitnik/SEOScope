from __future__ import annotations

from datetime import UTC, datetime

from app.reports.document import render_document_html


def _period(label: str, date_from: str, date_to: str) -> dict:
    return {"label": label, "date_from": date_from, "date_to": date_to, "is_partial": False}


_PERIODS = [
    _period("This Month", "2026-07-01", "2026-07-31"),
    _period("Last Month", "2026-06-01", "2026-06-30"),
    _period("Month YOY", "2025-07-01", "2025-07-31"),
    _period("This Quarter", "2026-05-01", "2026-07-31"),
    _period("Last Quarter", "2026-02-01", "2026-04-30"),
    _period("Quarterly YOY", "2025-05-01", "2025-07-31"),
]
_CHANGE_LABELS = ["% M/M", "% Q/Q", "% M YOY", "% Q YOY"]


def _row(key: str, label: str, values: list[int | None], changes: list[float | None]) -> dict:
    return {"key": key, "label": label, "values": values, "changes": changes}


def _month(month: str, label: str, clicks, impressions, ctr, organic_conversions) -> dict:
    return {
        "month": month, "label": label, "clicks": clicks, "impressions": impressions,
        "ctr": ctr, "organic_conversions": organic_conversions,
    }


def _table(**row_overrides) -> dict:
    defaults = {
        "clicks": _row(
            "clicks", "Clicks",
            [8728, 7898, 9020, 25113, 20714, 23731], [0.1058, 0.2127, -0.0328, -0.1966],
        ),
        "impressions": _row(
            "impressions", "Impressions",
            [189658, 196117, 236079, 583360, 486022, 631546], [-0.0328, 0.2005, -0.1966, -0.0762],
        ),
        "organic_users": _row(
            "organic_users", "Organic Users",
            [7462, 6409, 7961, 20031, 16850, 20114], [0.1643, 0.1888, -0.0693, -0.0041],
        ),
        "organic_conversions": _row(
            "organic_conversions", "Organic Conversions",
            [1415, 1324, 1994, 4074, 4519, 6220], [0.0687, -0.0987, -0.2904, -0.3451],
        ),
    }
    defaults.update(row_overrides)
    return {
        "periods": _PERIODS,
        "change_labels": _CHANGE_LABELS,
        "rows": list(defaults.values()),
        "history": {
            "months": [
                _month("2025-12", "December 2025", 7500, 190000, 0.0395, 2000),
                _month("2026-01", "January 2026", None, None, None, None),
                _month("2026-07", "July 2026", 8728, 189658, 0.046, 1415),
            ],
            "has_gsc": True,
            "has_ga4": True,
        },
    }


def _render(**row_overrides) -> str:
    return render_document_html(
        client_name="SAPPHIRE NEW YORK",
        table=_table(**row_overrides),
        generated_at=datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
    )


def test_renders_all_sixteen_tiles():
    html = _render()
    assert html.count('class="report-doc__tile"') == 16


def test_none_value_renders_as_an_em_dash_never_zero():
    clicks = _row(
        "clicks", "Clicks",
        [None, 7898, 9020, 25113, 20714, 23731], [None, 0.2127, -0.0328, -0.1966],
    )
    html = _render(clicks=clicks)
    assert ">0<" not in html
    assert "—" in html


def test_change_against_none_or_zero_renders_an_em_dash_not_infinity():
    clicks = _row(
        "clicks", "Clicks",
        [8728, 0, 9020, 25113, 20714, 23731], [None, 0.2127, -0.0328, -0.1966],
    )
    html = _render(clicks=clicks)
    assert "∞" not in html
    assert "Infinity" not in html


def test_no_inline_style_or_script_anywhere():
    html = _render()
    assert "<script" not in html
    assert "style=" not in html


def test_no_internal_identifiers_leak_into_a_client_facing_document():
    # "Reporting Ninja" IS mandated footer copy (task-47 §3.2 "Data source:
    # … via Reporting Ninja") — what must never appear is an internal id,
    # a run id, or a cabinet report NAME (as opposed to the provider name).
    html = _render()
    assert "run_id" not in html
    assert "ninja_report_name" not in html
    assert "Data source: Google Search Console + GA4 via Reporting Ninja" in html


def test_direction_word_is_present_for_screen_readers_not_color_only():
    html = _render()
    assert "visually-hidden" in html
    assert ("Up" in html) or ("Down" in html)


def test_chart_gap_becomes_a_line_break_not_a_zero():
    html = _render()
    # January 2026 is `None` in the fixture's 3-month history — the Clicks
    # chart must render TWO disjoint line segments around that gap, not one
    # continuous line through a fabricated zero.
    clicks_chart = html.split('data-chart-id="chart-clicks"')[1].split("</svg>")[0]
    line_segments = clicks_chart.count('fill="none" stroke="#ff4a17"')
    assert line_segments == 2


def test_footer_names_the_cache_date_when_a_period_was_reused():
    # task-49 §2.6: the client sees the same data-truth requirement every
    # other metric already gets — a report built on a reused PDF is not the
    # same thing as a fresh collection.
    html = render_document_html(
        client_name="SAPPHIRE NEW YORK",
        table={**_table(), "oldest_cached_at": "2026-08-18"},
        generated_at=datetime(2026, 8, 19, 12, 0, tzinfo=UTC),
    )
    assert "Source PDFs downloaded August 18, 2026" in html


def test_footer_has_no_cache_line_when_nothing_was_reused():
    html = _render()
    assert "Source PDFs downloaded" not in html
