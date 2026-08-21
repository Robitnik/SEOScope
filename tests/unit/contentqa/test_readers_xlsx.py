from __future__ import annotations

import io

from openpyxl import Workbook

from app.contentqa.readers.xlsx import read_strategy_xlsx


def _build_workbook() -> bytes:
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Q3 Strategy"
    ws1.append(["Q3 2026 Strategy"])
    ws1.append([
        "Page Status", "Intent", "Page Name", "URL", "New URL",
        "Primary Key", "Secondary Key", "Longtail Key", "H1", "Title", "Description",
    ])
    ws1.append([
        "OPTIMIZE PAGE", "Navigational", "Homepage",
        "https://example.com/", "https://example.com/",
        "building surveying software", "survey reporting software",
        "RICS compliant survey software",
        "Building Surveying Software for Digital Capture, Reporting and Analysis",
        "Building Surveying Software UK | Digital Reports and RICS Templates | GoReport",
        "GoReport is UK building surveying software for mobile capture and reporting.",
    ])
    ws1.append([
        "OPTIMIZE PAGE", "Commercial", "Pricing",
        "https://example.com/pricing/", "https://example.com/pricing/",
        "survey software pricing", "", "",
        "Survey Software Pricing", "Survey Software Pricing | GoReport",
        "See plans and pricing for our survey software.",
    ])

    ws2 = wb.create_sheet("Structure v 2.0")
    ws2.append(["Pages", "URL", "Recommended URL (redirect to...)", "Breadcrumbs", "Comments"])
    ws2.append(["Home", "https://example.com/", "https://example.com/", "Home", "Q3 #1"])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_sheet_without_title_or_description_columns_is_skipped() -> None:
    doc = read_strategy_xlsx(_build_workbook(), source="test.xlsx")
    assert doc.skipped_sheets == ("Structure v 2.0",)


def test_recognized_sheet_yields_two_rows() -> None:
    doc = read_strategy_xlsx(_build_workbook(), source="test.xlsx")
    assert len(doc.items) == 2


def test_column_mapping_by_header_alias() -> None:
    doc = read_strategy_xlsx(_build_workbook(), source="test.xlsx")
    home = doc.items[0]
    assert home.page_name == "Homepage"
    assert home.url == "https://example.com/"
    assert home.intent == "Navigational"
    expected_title = (
        "Building Surveying Software UK | Digital Reports and RICS Templates | GoReport"
    )
    assert home.title == expected_title
    assert home.h1 == "Building Surveying Software for Digital Capture, Reporting and Analysis"
    assert home.keywords == (
        "building surveying software", "survey reporting software",
        "RICS compliant survey software",
    )


def test_ref_carries_sheet_name_and_row_number() -> None:
    doc = read_strategy_xlsx(_build_workbook(), source="test.xlsx")
    assert doc.items[0].ref == "Q3 Strategy!row 3"
    assert doc.items[1].ref == "Q3 Strategy!row 4"


def test_missing_secondary_longtail_keys_do_not_pad_with_empty_strings() -> None:
    doc = read_strategy_xlsx(_build_workbook(), source="test.xlsx")
    pricing = doc.items[1]
    assert pricing.keywords == ("survey software pricing",)
