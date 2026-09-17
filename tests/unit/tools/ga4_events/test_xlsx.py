"""Юніти Excel-експорту GA4 Events by Month (task-98 §2.6)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from openpyxl import load_workbook

from app.schemas.ga4_events import (
    Ga4EventMonthDTO,
    Ga4EventRowDTO,
    Ga4EventSegmentDTO,
    Ga4EventsReportDTO,
    Ga4EventTotalsDTO,
)
from app.tools.ga4_events.xlsx import build_workbook


def _segment(count: int, users: int, per_user: str, ev_pct: str, user_pct: str):
    return Ga4EventSegmentDTO(
        event_count=count,
        users=users,
        per_user=Decimal(per_user),
        event_share_pct=Decimal(ev_pct),
        user_share_pct=Decimal(user_pct),
    )


def _row(name: str, *, key: bool = False) -> Ga4EventRowDTO:
    return Ga4EventRowDTO(
        event_name=name,
        is_key_event=key,
        key_events=704 if key else None,
        segments={
            "all": _segment(704, 437, "1.61", "2.53", "11.05"),
            "direct": _segment(74, 57, "1.30", "0.61", "3.54"),
            "organic": _segment(70, 54, "1.30", "1.16", "7.71"),
        },
    )


def _month(month: str, label: str, *, rows: list[Ga4EventRowDTO]) -> Ga4EventMonthDTO:
    return Ga4EventMonthDTO(
        month=month,
        label=label,
        state="ok",
        is_partial=False,
        rows=rows,
        totals={
            "all": Ga4EventTotalsDTO(event_count=27772, users=3953, key_events=849),
            "direct": Ga4EventTotalsDTO(event_count=12088, users=1610, key_events=105),
            "organic": Ga4EventTotalsDTO(event_count=6042, users=700, key_events=111),
        },
    )


def _report(months: list[Ga4EventMonthDTO]) -> Ga4EventsReportDTO:
    return Ga4EventsReportDTO(
        id="r1",
        client_id="c1",
        client_name="SDS",
        property_id="properties/1",
        months=[month.month for month in months],
        state="ok",
        created_at=datetime(2026, 9, 17, 12, tzinfo=UTC),
        months_data=months,
    )


def _load(report: Ga4EventsReportDTO):
    return load_workbook(build_workbook(report))


def test_one_sheet_per_month_plus_summary() -> None:
    months = [
        _month("2026-08", "Aug 2026", rows=[_row("Book Now", key=True)]),
        _month("2026-07", "Jul 2026", rows=[_row("Book Now", key=True)]),
    ]
    assert _load(_report(months)).sheetnames == ["Summary", "2026-08", "2026-07"]


def test_each_event_takes_three_comparison_rows_named_once() -> None:
    sheet = _load(_report([_month("2026-07", "Jul 2026", rows=[_row("Book Now", key=True)])]))[
        "2026-07"
    ]
    values = [row for row in sheet.iter_rows(values_only=True)]

    assert values[0] == tuple(
        [
            "Event Name",
            "Comparison",
            "Event Count",
            "Event Count %",
            "Total Users",
            "Total Users %",
            "Event count per user",
        ]
    )
    assert values[1] == ("Book Now", "All Users", 704, 2.53, 437, 11.05, 1.61)
    # Назва івента стоїть один раз на групу — не тричі.
    assert values[2] == (None, "Direct traffic", 74, 0.61, 57, 3.54, 1.3)
    assert values[3] == (None, "Organic traffic", 70, 1.16, 54, 7.71, 1.3)


def test_total_rows_use_each_comparisons_property_totals() -> None:
    sheet = _load(_report([_month("2026-07", "Jul 2026", rows=[_row("Book Now")])]))["2026-07"]
    values = [row for row in sheet.iter_rows(values_only=True)]
    comparisons = {"All Users", "Direct traffic", "Organic traffic"}
    totals = [row for row in values if row[1] in comparisons][-3:]

    assert totals[0][2] == 27772 and totals[0][4] == 3953
    assert totals[1][2] == 12088 and totals[1][4] == 1610
    assert totals[2][2] == 6042 and totals[2][4] == 700
    # Сума колонки Users (437 + 57 + 54) нікуди не потрапляє.
    assert 548 not in [cell for row in values for cell in row]
    note = str(values[-1][0])
    assert "not the sum of the column" in note


def test_failed_month_still_gets_a_sheet_with_the_reason() -> None:
    month = Ga4EventMonthDTO(
        month="2026-08",
        label="Aug 2026",
        state="failed",
        is_partial=False,
        rows=[],
        totals={},
        error="ga4 runReport failed: http_500",
    )
    sheet = _load(_report([month]))["2026-08"]

    assert sheet.cell(row=2, column=1).value == "ga4 runReport failed: http_500"


def test_summary_is_all_users_only_with_key_events_first() -> None:
    months = [
        _month(
            "2026-07",
            "Jul 2026",
            rows=[
                Ga4EventRowDTO(
                    event_name="page_view",
                    segments={"all": _segment(9712, 6084, "1.60", "34.9", "50.0")},
                ),
                _row("Book Now", key=True),
            ],
        )
    ]
    sheet = _load(_report(months))["Summary"]
    values = [row for row in sheet.iter_rows(values_only=True)]

    assert values[0] == ("Event", "Key event", "Jul 2026")
    assert values[1][:2] == ("Book Now", "yes")
    assert values[2][0] == "page_view"
    total = next(row for row in values if row[0] == "TOTAL events (All Users)")
    assert total[2] == 27772
