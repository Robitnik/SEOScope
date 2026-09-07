from __future__ import annotations

from datetime import UTC, datetime

from app.reports.document import render_document_html


def _period(label: str, date_from: str, date_to: str, *, is_partial: bool = False) -> dict:
    return {"label": label, "date_from": date_from, "date_to": date_to, "is_partial": is_partial}


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


def _table(*, periods: list[dict] | None = None, **row_overrides) -> dict:
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
        "periods": periods if periods is not None else _PERIODS,
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


def _render(*, source: str = "api", periods: list[dict] | None = None, **row_overrides) -> str:
    return render_document_html(
        client_name="SAPPHIRE NEW YORK",
        table=_table(periods=periods, **row_overrides),
        generated_at=datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
        source=source,
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


def test_zero_value_renders_as_a_real_zero_not_an_em_dash():
    # §0.4's other half: a genuine 0 is a real measurement and must not be
    # swallowed by the same "muted" styling/glyph a missing value gets.
    clicks = _row(
        "clicks", "Clicks",
        [0, 7898, 9020, 25113, 20714, 23731], [0.1058, 0.2127, -0.0328, -0.1966],
    )
    html = _render(clicks=clicks)
    tile_html = html.split("Monthly Clicks</span>")[1].split("</div>")[0]
    assert ">0<" in tile_html
    assert "report-doc__tile-value--muted" not in tile_html


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
    assert "<style" not in html
    assert "style=" not in html


def test_no_internal_identifiers_leak_into_a_client_facing_document():
    # "Reporting Ninja" IS mandated footer copy (task-47 §3.2 "Data source:
    # … via Reporting Ninja") — what must never appear is an internal id,
    # a run id, or a cabinet report NAME (as opposed to the provider name).
    html = _render()
    assert "run_id" not in html
    assert "ninja_report_name" not in html
    assert "Data source: Google Search Console + GA4 via Reporting Ninja" in html


def test_footer_says_direct_from_google_for_a_google_api_run():
    # task-85 §1.5/§2.7: after task-84, a run's data may never have touched
    # Reporting Ninja at all — the footer must not claim otherwise.
    html = _render(source="google_api")
    assert "Data source: Google Search Console + GA4 direct from Google" in html
    assert "via Reporting Ninja" not in html


def test_footer_falls_back_to_reporting_ninja_for_pdf_source():
    html = _render(source="pdf")
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
        source="api",
    )
    assert "Source PDFs downloaded August 18, 2026" in html


def test_footer_has_no_cache_line_when_nothing_was_reused():
    html = _render()
    assert "Source PDFs downloaded" not in html


def test_h1_renders_whatever_name_it_is_given():
    # The `display_name or name` fallback is resolved one layer up
    # (`app.api.reports._document_source`, task-85 §2.1) — this module only
    # ever sees the resolved string and must render it verbatim, whatever it is.
    html = _render()
    assert '<h1 translate="no">SAPPHIRE NEW YORK</h1>' in html


def test_both_tile_sections_get_a_real_visible_heading():
    # task-85 §1.2/§2.2: the two 8-tile blocks used to be told apart only by
    # a screen-reader-only `aria-label` — a sighted reader had no way to tell
    # the bottom block was Year-over-Year, not a duplicate of the top one.
    html = _render()
    assert '<h2 class="report-doc__section-title">Monthly &amp; Quarterly</h2>' in html
    assert '<h2 class="report-doc__section-title">Year over Year</h2>' in html
    assert 'aria-label="Monthly and quarterly summary"' in html
    assert 'aria-label="Year over year summary"' in html


def test_every_tile_group_shows_both_sides_of_its_comparison():
    # task-85 §2.6: a live incident traced a "data mismatch" report back to
    # comparing our August document against a July Reporting Ninja
    # screenshot — a value with no visible baseline period can't be checked
    # by anyone without opening the database.
    html = _render()
    assert "July 1 – July 31, 2026 vs June 1 – June 30, 2026" in html  # Monthly
    assert "May 1 – July 31, 2026 vs February 1 – April 30, 2026" in html  # Quarterly
    assert "July 1 – July 31, 2026 vs July 1 – July 31, 2025" in html  # Monthly YOY
    assert "May 1 – July 31, 2026 vs May 1 – July 31, 2025" in html  # Quarterly YOY


def test_a_partial_period_says_so_next_to_its_range():
    partial_periods = [
        _period("This Month", "2026-09-01", "2026-09-07", is_partial=True),
        _period("Last Month", "2026-08-01", "2026-08-31"),
        _period("Month YOY", "2025-09-01", "2025-09-30"),
        _period("This Quarter", "2026-07-01", "2026-09-07", is_partial=True),
        _period("Last Quarter", "2026-04-01", "2026-06-30"),
        _period("Quarterly YOY", "2025-07-01", "2025-09-30"),
    ]
    html = _render(periods=partial_periods)
    assert "September 1 – September 7, 2026 · partial vs August 1 – August 31, 2026" in html
    # The comparison side is a closed month and must NOT be marked partial.
    assert "August 1 – August 31, 2026 · partial" not in html


def test_no_comparison_label_when_the_table_has_no_matching_periods():
    # A non-monthly run (task-47 §1.5's own note): none of "This Month" /
    # "Last Month" / etc. exist, every tile already renders `—`, and a
    # fabricated date range would be worse than none at all.
    custom_periods = [_period("Selected Range", "2026-01-01", "2026-01-31")]
    no_changes = [None, None, None, None]
    html = _render(
        periods=custom_periods,
        clicks=_row("clicks", "Clicks", [8728], no_changes),
        impressions=_row("impressions", "Impressions", [189658], no_changes),
        organic_users=_row("organic_users", "Organic Users", [7462], no_changes),
        organic_conversions=_row("organic_conversions", "Organic Conversions", [1415], no_changes),
    )
    assert "report-doc__tile-block-range" not in html


def test_each_chart_card_shows_its_own_series_date_range():
    # task-85 §2.6: the exact ambiguity that let a stale screenshot pass for
    # current data — a chart ending in "… July 2026" looks identical whether
    # it is this month's chart or one running a month behind.
    html = _render()
    assert html.count("December 2025 – July 2026") == 4


def test_structural_snapshot_guards_every_section():
    # Not a byte-for-byte snapshot (too brittle against whitespace) — a
    # regression guard against a future edit silently dropping or reordering
    # a whole section (task-85 §4.8).
    html = _render()
    assert html.count('class="report-doc__tile-block"') == 4
    assert html.count('class="report-doc__chart-card"') == 4
    assert html.count("report-doc__section-title") == 2
    order = [
        html.index('<h1 translate="no">'),
        html.index("Monthly &amp; Quarterly"),
        html.index('aria-label="Monthly trends"'),
        html.index("Year over Year"),
        html.index('class="report-doc__footer"'),
    ]
    assert order == sorted(order)
