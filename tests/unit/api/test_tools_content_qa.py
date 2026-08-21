"""API-тести Strategy Check + Framework Check (task-53): стан не персистується,
тож кожен тест — один POST і перевірка тіла відповіді."""

from __future__ import annotations

import io
import zipfile
from typing import Any
from xml.sax.saxutils import escape

from openpyxl import Workbook

_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _xlsx(rows: list[list[object]], sheet_name: str = "Q3 Strategy") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _p(text: str) -> str:
    return f'<w:p xmlns:w="{_NS}"><w:r><w:t>{escape(text)}</w:t></w:r></w:p>'


def _docx(*paragraphs: str) -> bytes:
    body = "".join(_p(p) for p in paragraphs)
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_NS}"><w:body>{body}</w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def _post(client: Any, route: str, content: bytes, filename: str, **params: str) -> Any:
    return client.post(
        f"/api/v1/tools/{route}",
        params=params,
        content=content,
        headers={"X-Filename": filename, "Content-Type": "application/octet-stream"},
    )


STRATEGY_ROWS = [
    ["Page Name", "URL", "Primary Key", "H1", "Title", "Description"],
    [
        "Homepage", "https://example.com/", "building surveying software",
        "Building Surveying Software for Digital Capture, Reporting and Analysis",
        "Building Surveying Software UK | Digital Reports and RICS Templates and More | GoReport",
        "GoReport is UK building surveying software for mobile capture and reporting teams.",
    ],
]

FRAMEWORK_PARAGRAPHS = (
    "Page:",
    "URL",
    "https://example.com/schedule-of-condition-software/",
    "Target Keywords",
    "(first keyword is the primary)",
    "schedule of condition software",
    "Meta Page Title:",
    "Schedule of Condition (SOC) Software | GoReport",
    "Meta Description:",
    "Digital Schedule of Condition surveys with photo capture and branded outputs. Book a demo.",
    "H1",
    "Schedule of Condition Software for Commercial Property Surveyors",
    "Structure / Content:",
    "H1: Schedule of Condition Software for Commercial Property Surveyors",
    "H2: What Is a Schedule of Condition?",
    "External link + Anchor text",
    "No",
    "Internal Link + Anchor Text",
    "party wall survey (/party-wall-survey/)",
    "SEO & Content Guidelines",
)


def test_strategy_check_returns_findings_and_persists_the_report(client: Any) -> None:
    resp = _post(client, "strategy-check/check", _xlsx(STRATEGY_ROWS), "strategy.xlsx")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["doc_type"] == "strategy_xlsx"
    assert body["source_filename"] == "strategy.xlsx"
    assert body["item_count"] == 1
    assert body["skipped_sheets"] == []
    checks = {f["check"] for f in body["findings"]}
    assert "meta-title-length" in checks
    assert body["severity_counts"].get("error", 0) >= 1
    assert body["id"]
    assert body["created_at"]

    title_finding = next(f for f in body["findings"] if f["check"] == "meta-title-length")
    assert title_finding["value"] == STRATEGY_ROWS[1][4]

    # The same report is fetchable by id — this is the "shareable link" contract.
    fetched = client.get(f"/api/v1/tools/strategy-check/reports/{body['id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json() == body

    listed = client.get("/api/v1/tools/strategy-check/reports")
    assert listed.status_code == 200, listed.text
    listed_body = listed.json()
    assert listed_body["total"] == 1
    assert listed_body["reports"][0]["id"] == body["id"]
    assert listed_body["reports"][0]["source_filename"] == "strategy.xlsx"


def test_strategy_report_not_mixed_up_with_framework_reports(client: Any) -> None:
    strategy_resp = _post(client, "strategy-check/check", _xlsx(STRATEGY_ROWS), "s.xlsx")
    strategy_id = strategy_resp.json()["id"]

    framework_resp = _post(
        client, "framework-check/check", _docx(*FRAMEWORK_PARAGRAPHS), "f.docx",
    )
    framework_id = framework_resp.json()["id"]

    # Fetching a framework report through the strategy-check URL is 404, not
    # a cross-tool leak — the two tools share one table but never one URL space.
    assert client.get(f"/api/v1/tools/strategy-check/reports/{framework_id}").status_code == 404
    assert client.get(f"/api/v1/tools/framework-check/reports/{strategy_id}").status_code == 404

    strategy_list = client.get("/api/v1/tools/strategy-check/reports").json()
    assert [r["id"] for r in strategy_list["reports"]] == [strategy_id]
    framework_list = client.get("/api/v1/tools/framework-check/reports").json()
    assert [r["id"] for r in framework_list["reports"]] == [framework_id]


def test_get_report_404_for_unknown_id(client: Any) -> None:
    resp = client.get("/api/v1/tools/strategy-check/reports/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "report_not_found"


def test_strategy_check_reports_skipped_sheet_not_silence(client: Any) -> None:
    wb_rows = [["Pages", "URL", "Breadcrumbs"], ["Home", "https://example.com/", "Home"]]
    resp = _post(client, "strategy-check/check", _xlsx(wb_rows, "Structure v 2.0"), "s.xlsx")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped_sheets"] == ["Structure v 2.0"]
    assert body["item_count"] == 0


def test_strategy_check_rejects_empty_upload(client: Any) -> None:
    resp = _post(client, "strategy-check/check", b"", "empty.xlsx")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "empty_upload"


def test_strategy_check_rejects_unreadable_file(client: Any) -> None:
    resp = _post(client, "strategy-check/check", b"not an xlsx file", "broken.xlsx")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "unreadable_xlsx"


def test_framework_check_returns_findings(client: Any) -> None:
    resp = _post(
        client, "framework-check/check", _docx(*FRAMEWORK_PARAGRAPHS), "framework.docx",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["doc_type"] == "framework_docx"
    assert body["item_count"] == 1
    checks = {f["check"] for f in body["findings"]}
    assert "meta-title-length" in checks  # 47 chars < TITLE_MIN=50 -> warn


def test_framework_check_faq_band_only_applies_when_kind_is_blog(client: Any) -> None:
    # One FAQ question: below blog-rules.md's FAQ_MIN=4, but framework-rules.md
    # (page) has no minimum at all — the `kind` query param must reach the
    # engine's Profile, not just default silently to "page" server-side.
    # Inserted inside the "Structure / Content:" block (before the next label)
    # so the docx reader's heading parser actually picks it up.
    split = FRAMEWORK_PARAGRAPHS.index("External link + Anchor text")
    paragraphs = (
        *FRAMEWORK_PARAGRAPHS[:split],
        "H2: Frequently Asked Questions",
        "H3: What does this cover?",
        *FRAMEWORK_PARAGRAPHS[split:],
    )
    page_resp = _post(
        client, "framework-check/check", _docx(*paragraphs), "f.docx", kind="page",
    )
    blog_resp = _post(
        client, "framework-check/check", _docx(*paragraphs), "f.docx", kind="blog",
    )
    page_checks = {f["check"] for f in page_resp.json()["findings"]}
    blog_checks = {f["check"] for f in blog_resp.json()["findings"]}
    assert "faq-count" not in page_checks
    assert "faq-count" in blog_checks


def test_framework_check_rejects_unreadable_file(client: Any) -> None:
    resp = _post(client, "framework-check/check", b"not a docx file", "broken.docx")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == "unreadable_docx"


def test_brand_padding_uses_brand_query_param(client: Any) -> None:
    rows = [
        ["Page Name", "URL", "H1", "Title", "Description"],
        [
            "Home", "https://example.com/", "H1 text",
            "x" * 55 + " | GoReport",
            "y" * 150,
        ],
    ]
    without_brand = _post(client, "strategy-check/check", _xlsx(rows), "s.xlsx")
    with_brand = _post(client, "strategy-check/check", _xlsx(rows), "s.xlsx", brand="GoReport")
    without_checks = {f["check"] for f in without_brand.json()["findings"]}
    with_checks = {f["check"] for f in with_brand.json()["findings"]}
    assert "meta-title-brand-padding" not in without_checks
    assert "meta-title-brand-padding" in with_checks
