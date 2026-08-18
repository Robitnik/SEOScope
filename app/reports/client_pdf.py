"""Client PDF export (task-47 §1.1/§1.4/§2.1/§3.3).

Drives the already-baked Chromium (task-42, `PLAYWRIGHT_BROWSERS_PATH=
/opt/ms-playwright`) against the client document's OWN URL and calls
`page.pdf()` — the exact same markup `app.reports.document` renders for the
operator's `<iframe>` preview, so a screen and a file can never disagree
(task-47 §2.1 "Один рендерер — два виходи"). `api` and `worker` share the
host network (`network_mode: host`, compose.yaml), so the worker process
reaches the API's own server the same way any browser would.

The page height starts from a DOM measurement (`.report-doc__page`'s real
`getBoundingClientRect().height`), but that alone is not sufficient — live-
measured 2026-08-18: `page.pdf(height=<that exact value>)` still produced
`Pages: 2` every time, for a plain flex/grid layout with zero print media
queries. The browsing-context layout height and Chromium's own print-layout
height for the SAME markup at the SAME CSS width are simply not guaranteed
to agree pixel-for-pixel (grid/flex rounding compounds differently under the
print pipeline). Guessing a fixed fudge factor would be fragile against
different data (more/fewer history months, longer client names wrapping a
tile label). Instead this verifies its own output: `pdfinfo` (poppler-utils,
already an image dependency — `app/reports/pdf_parse.py` shells out to its
sibling `pdftotext` the same way) reports the real page count, and on
anything but 1 the page is regenerated taller until it converges.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from app.core.config import Settings

_PAGE_WIDTH_PX = 860
_PAGE_SELECTOR = ".report-doc__page"
_SLUG_RE = re.compile(r"[^\w-]+")
# Grows the candidate height until `pdfinfo` confirms one page. 1.15^5 ≈ 2x
# the original measurement — comfortably past every gap measured live
# (~15-20%) within 2 attempts, and bounded so a genuinely broken layout
# fails fast instead of looping.
_HEIGHT_GROWTH_FACTOR = 1.15
_MAX_HEIGHT_ATTEMPTS = 5


def _slug(value: str) -> str:
    return _SLUG_RE.sub("_", value.strip()).strip("_").upper() or "CLIENT"


def client_pdf_filename(client_name: str, month_stamp: str) -> str:
    """`TheYovth_<CLIENT>_Monthly_2026-07.pdf` (task-47 §3.3) — readable in
    the agency's own outbound email, unlike a bare `run_id.pdf`.
    """
    return f"TheYovth_{_slug(client_name)}_Monthly_{month_stamp}.pdf"


def _pdf_page_count(path: Path) -> int | None:
    """`None` when `pdfinfo` itself is unavailable/fails — the caller then
    trusts the current render rather than looping forever on a diagnostic
    tool problem that has nothing to do with the document's own layout.
    """
    try:
        result = subprocess.run(
            ["pdfinfo", str(path)], capture_output=True, text=True, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"^Pages:\s*(\d+)", result.stdout, re.MULTILINE)
    return int(match.group(1)) if match else None


def _render_single_page_pdf(page: Page, out_path: Path, measured_height: float) -> None:
    height = measured_height
    for _attempt in range(_MAX_HEIGHT_ATTEMPTS):
        page.pdf(
            path=str(out_path),
            width=f"{_PAGE_WIDTH_PX}px",
            height=f"{height}px",
            print_background=True,
            margin={"top": "0px", "right": "0px", "bottom": "0px", "left": "0px"},
        )
        page_count = _pdf_page_count(out_path)
        if page_count is None or page_count <= 1:
            return
        height *= _HEIGHT_GROWTH_FACTOR


def render_client_pdf(
    *,
    settings: Settings,
    run_id: str,
    out_path: Path,
    headless: bool = True,
) -> None:
    """Render `GET /reports/runs/{run_id}/document` to `out_path`.

    Raises on any failure — the caller (`runner.py`'s tail, or the API's
    `POST .../client-pdf` handler) treats a client PDF as best-effort output,
    exactly like `write_run_xlsx` (task-45): a run that already has a table is
    never failed by a PDF-rendering problem.
    """
    url = f"{settings.reports_internal_base_url}/api/v1/reports/runs/{run_id}/document"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        try:
            page = browser.new_page(viewport={"width": _PAGE_WIDTH_PX, "height": 2400})
            page.goto(url, wait_until="networkidle", timeout=30_000)
            measured_height = page.eval_on_selector(
                _PAGE_SELECTOR, "el => el.getBoundingClientRect().height"
            )
            _render_single_page_pdf(page, out_path, measured_height)
        finally:
            browser.close()
