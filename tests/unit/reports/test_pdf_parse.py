from __future__ import annotations

from pathlib import Path

import pytest

from app.reports import pdf_parse
from app.reports.pdf_parse import (
    MIN_PDF_BYTES,
    PdfDownloadIncomplete,
    assert_pdf_complete,
    metric_coverage,
    parse_pdf,
)

_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "reports"
_FIXTURE = _FIXTURES_DIR / "EFFYS-Internal_report__this_month__2026-07-01_2026-07-31.pdf"
# Real PDF, live-downloaded 2026-08-17 (run `2af31094`, task-48 §1.3): EFFYS's
# GA4 property did not exist in July 2025, so BOTH the channel table ("First
# user default channel grouping": "There are no results") and Overall
# performance (a literal `0` for Total users/Key events) say "no data" for
# this exact period — the real-world case the `no_data` verdict exists for.
_NO_DATA_FIXTURE = _FIXTURES_DIR / "EFFYS-Internal_report__month_yoy__2025-07-01_2025-07-31.pdf"


def test_fixture_exists():
    assert _FIXTURE.exists(), "real Reporting Ninja PDF fixture (task-42 §8) is missing"


def test_parse_pdf_matches_the_live_measurement_from_the_task_doc():
    # task-42 §1.3 — EFFYS, July 2026, verified live against three independent sources.
    metrics = parse_pdf(_FIXTURE)
    assert metrics.users == 6251
    assert metrics.organic_users == 3061
    assert metrics.direct_users == 2858
    assert metrics.conversions == 1099
    assert metrics.organic_conversions == 654
    assert metrics.direct_conversions == 391
    assert metrics.clicks == 2528
    assert metrics.impressions == 31564


def test_organic_search_is_not_polluted_by_organic_social():
    # The fixture's channel table also has an "Organic Social" row (137 users, 26
    # conversions) — organic_users/organic_conversions must stay at the
    # "Organic Search" row's own values, not include it.
    metrics = parse_pdf(_FIXTURE)
    assert metrics.organic_users == 3061
    assert metrics.organic_users != 3061 + 137


def test_metric_coverage_is_8_of_8_on_a_clean_pdf():
    metrics = parse_pdf(_FIXTURE)
    found, missing = metric_coverage(metrics)
    assert len(found) == 8
    assert missing == []


def test_assert_pdf_complete_accepts_the_real_fixture():
    assert_pdf_complete(_FIXTURE)  # >800 KB, does not raise


def test_assert_pdf_complete_rejects_a_truncated_file(tmp_path):
    truncated = tmp_path / "truncated.pdf"
    truncated.write_bytes(b"%PDF-1.4 not a real file")
    with pytest.raises(PdfDownloadIncomplete):
        assert_pdf_complete(truncated)


def test_min_pdf_bytes_is_well_below_every_real_period_size():
    # Live measurement (task-42 §1.2): smallest real PDF was 242 KB.
    assert MIN_PDF_BYTES < 200_000


# --- task-48 §1.3/§2.3: "no data" (0-as-empty) normalization -----------------


def test_no_data_fixture_exists():
    assert _NO_DATA_FIXTURE.exists(), "real 'no GA4 data' Reporting Ninja PDF fixture is missing"


def test_a_period_with_no_channel_rows_and_zero_overall_parses_as_all_none():
    # task-48 §1.3 live measurement: EFFYS Month YOY 2025-07 — empty channel
    # table + literal `0` in Overall performance on EVERY metric that table
    # feeds. Before this fix, `parse_pdf` returned `0` here; the API path
    # (task-45) already returns `None` for the same period, which used to
    # show up as 4 false `mismatch` cells per PDF run (task-48 §1.4).
    metrics = parse_pdf(_NO_DATA_FIXTURE)
    assert metrics.users is None
    assert metrics.conversions is None
    assert metrics.organic_users is None
    assert metrics.direct_users is None
    assert metrics.organic_conversions is None
    assert metrics.direct_conversions is None
    assert metrics.clicks is None
    assert metrics.impressions is None


# --- looks_like_a_report_pdf: telling "no data" apart from "broken PDF" -----


def test_a_genuinely_empty_period_still_looks_like_a_real_report():
    # Live regression (2026-08-19): EFFYS's real Month YOY/Quarterly YOY
    # periods parse to all-`None` (the no-data fixture above) — but the PDF
    # itself is a perfectly good, complete Reporting Ninja report, not a
    # broken download. `runner.parse_and_check` needs this signal to avoid
    # discarding the period as a parse failure.
    from app.reports.pdf_parse import looks_like_a_report_pdf

    assert looks_like_a_report_pdf(_NO_DATA_FIXTURE) is True


def test_a_real_period_with_data_also_looks_like_a_real_report():
    from app.reports.pdf_parse import looks_like_a_report_pdf

    assert looks_like_a_report_pdf(_FIXTURE) is True


def test_a_pdf_missing_the_report_chrome_does_not_look_like_a_real_report(monkeypatch):
    # The genuine failure case `parse_and_check` must still catch loudly:
    # a PDF that never had the report's own page structure at all (wrong
    # file, totally different layout) — as opposed to a real report page
    # that simply had nothing to show.
    from app.reports import pdf_parse

    monkeypatch.setattr(pdf_parse, "_pdf_text", lambda _path: "This is not a report at all.")
    assert pdf_parse.looks_like_a_report_pdf(Path("irrelevant.pdf")) is False


def test_a_real_nonzero_period_is_never_touched_by_the_no_data_rule():
    # The existing "This Month" fixture has real channel rows and real
    # non-zero Overall performance — `_period_has_no_data` must not fire,
    # so every value stays exactly what task-42 §1.3 already verified.
    metrics = parse_pdf(_FIXTURE)
    assert metrics.users == 6251
    assert metrics.clicks == 2528


def test_a_genuine_zero_metric_with_channel_rows_present_stays_zero(monkeypatch):
    # task-48 §1.3: "справжній нуль (є рядки каналів, є ненульові сусідні
    # метрики) лишається 0" — no real fixture has a genuine zero metric
    # alongside populated channel rows, so this synthesizes the PDF TEXT
    # (not the file) to exercise the discriminator directly: a populated
    # channel table means `_period_has_no_data` must never fire, regardless
    # of what Overall performance prints.
    fake_text = (
        "Total users                    0          0     0.00%\n"
        "Key events                     0          0     0.00%\n"
        "1  Organic Search              500        50    9.09%   3061   654\n"
        "2  Direct                      120        12    9.09%   2858   391\n"
        "Google SEO - Google Console\n"
        "Clicks                         0          0     0.00%\n"
        "Impressions                    1000       900   10.00%\n"
    )
    monkeypatch.setattr(pdf_parse, "_pdf_text", lambda _path: fake_text)
    metrics = parse_pdf(Path("irrelevant.pdf"))
    assert metrics.organic_users == 500
    assert metrics.direct_users == 120
    assert metrics.users == 0  # a genuine zero: channel rows exist, so it is real
    assert metrics.conversions == 0
    assert metrics.clicks == 0
    assert metrics.impressions == 1000
