"""Headless PDF download from Reporting Ninja (task-42 §3.7).

Ported from `AISeoTools/.../clientreport/pdf_exporter.py` — the selectors,
transient-network classification and download-integrity check are the
product of that module's own live measurement against the real site and are
kept verbatim. What changes here:

- `headless=True` and auto-login from `REPORTING_NINJA_EMAIL`/`_PASSWORD`
  (task-42 §1.1/§3.7.1) instead of a manual headed-browser login flow;
- events instead of `log.info` (`emit(level, stage, message, details)`);
- cooperative cancellation between periods and inside the retry loop
  (`is_cancelled`, §3.7.6);
- a screenshot artifact on every failed attempt, not just the final one
  (§3.7.4);
- an arbitrary list of `ReportPeriod`s instead of the fixed 6 (§2.4);
- period isolation (§3.7.5): one period's exhausted retries fail THAT
  period and move on — they never abort the whole browser session. Only an
  auth failure (§3.7.1: one re-login per run, a second failure is a real
  credential problem) aborts the remaining periods.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page

from app.reports.naming import pdf_filename
from app.reports.pdf_parse import PdfDownloadIncomplete, assert_pdf_complete
from app.reports.periods import ReportPeriod
from app.reports.stealth_browser import (
    is_timeout_error,
    new_stealth_context,
    sync_playwright,
)

BASE_URL = "https://app.reportingninja.com"

# Chrome net-stack errors that are TRANSIENT (a Wi-Fi/VPN switch, a
# suspend/resume, a server blip) — retrying these is safe. Anything else
# (a real selector drift, a logic bug) is re-raised at once.
_TRANSIENT_NET_MARKERS = (
    "ERR_NETWORK_CHANGED",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NETWORK_IO_SUSPENDED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_ABORTED",
    "ERR_CONNECTION_REFUSED",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_TIMED_OUT",
    "ERR_EMPTY_RESPONSE",
)

_DEAD_URL_MARKERS = ("chrome-error://", "/downloadreport")
_REPORT_HOST = "app.reportingninja.com"

_STEP_PAUSE_MS = 800
_LOCK_POLL_SECONDS = 0.25
# An interactive HTTP request waits this long for the cabinet, then says so.
_INTERACTIVE_LOCK_TIMEOUT = 90.0
_ROW_SELECTOR = "div.v-label.cursorPointer"
# How long a filter RPC gets to actually land before the read is called
# garbage (`_search_names`): one 1200ms settle plus this many re-reads.
_FILTER_SETTLE_MS = 1200
_FILTER_RETRY_MS = 700
_FILTER_MAX_REREADS = 6
Emit = Callable[[str, str, str, dict[str, Any]], None]
# `(label, state, *, attempt=1, max_attempts=1)` — loosely typed on purpose:
# the one real implementation (`runner.py::_make_period_marker`) forwards
# these as keyword args into `ReportRepository.update_period`, and a
# `Protocol` here would just restate that shape without checking anything
# `Callable[..., None]` doesn't already rule out (wrong arity/positional use).
PeriodStateCallback = Callable[..., None]


class AuthenticationFailedError(RuntimeError):
    """Session could not be (re-)established — a second login attempt also
    hit the login form (task-42 §3.7.1: a real credential/2FA problem, not
    a network blip).
    """


class CabinetBusyError(RuntimeError):
    """Another Reporting Ninja browser session holds the cabinet lock and did
    not release it within this caller's budget. A LOUD, retriable failure on
    purpose: the alternative (running anyway) is what silently corrupted the
    cabinet's own search — see `cabinet_lock`.
    """


class CabinetFilterError(RuntimeError):
    """The cabinet's search box was typed into, but the row set that came back
    is not the filtered one. See `_search_names` — reading it as a search
    result is what produced `report_name_unknown` for names that exist.
    """


def _lock_path() -> Path:
    return _session_path().with_suffix(".lock")


@contextlib.contextmanager
def cabinet_lock(*, timeout_seconds: float | None = None) -> Generator[None, None, None]:
    """Exclusive, CROSS-PROCESS access to the Reporting Ninja cabinet.

    Not an optimisation — a correctness requirement, and the project-wide
    invariant "browser profiles of one provider never run in parallel"
    (PLAN §9.1) applied to the one provider that was missing it. Measured
    live (2026-09-07) with two concurrent `search_report_names` calls: BOTH
    returned the identical UNFILTERED first virtualized screen — 15 reports
    alphabetically cut off at `GRP-client` — instead of their own filtered
    result. All three cabinet readers share one `storage_state`, hence one
    Reporting Ninja session cookie, hence one server-side Vaadin UI; a second
    context attaching to it makes the filter RPC a silent no-op, and the DOM
    read that follows looks like a perfectly ordinary answer.

    The two things that answer then get wrong are both silent and both
    already happened:

    - `_cabinet_has_report_name("SDS-client")` reads the unfiltered screen,
      does not find the name, and reports the report as ABSENT — the live
      `report_name_unknown` on a name the account really has;
    - `_open_report` takes `.first` of the row set, so a PDF run for SDS
      opens `19CLV-client` and stores another client's 8/8 metrics under
      SDS — the exact §1.2 poisoning, through a different door.

    The historical "virtualization cuts the list off after G" diagnosis
    (task-48 §1.6, task-49) describes this same 15-row screen. A single
    lock-holding `list_report_names` returns all 32 names in 6.6s with
    `truncated=False`.

    `fcntl.flock` (not a lock row, not a `threading.Lock`) for two reasons:
    the api and worker are separate PROCESSES sharing this cabinet, and the
    kernel releases an flock when its holder dies, so a crashed run can
    never wedge the cabinet permanently.

    `timeout_seconds=None` waits indefinitely — for `download_periods`,
    which is the work everything else should yield to. An interactive caller
    passes a budget and surfaces `CabinetBusyError` ("a report run is using
    the Reporting Ninja session"), because a request that hangs for the
    length of a 6-period PDF run is not an answer either.
    """
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(path, "a+b")  # noqa: SIM115 - held for the lock's duration, closed in `finally`
    try:
        if timeout_seconds is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout_seconds
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise CabinetBusyError(
                            "The Reporting Ninja browser session is busy with another "
                            f"cabinet operation (waited {timeout_seconds:g}s)"
                        ) from None
                    time.sleep(_LOCK_POLL_SECONDS)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


@dataclass(frozen=True)
class DownloadedPeriod:
    period: ReportPeriod
    pdf_path: Path


def _is_transient_net_error(exc: BaseException) -> bool:
    message = str(exc)
    return any(marker in message for marker in _TRANSIENT_NET_MARKERS)


def _session_path() -> Path:
    return Path(
        os.environ.get("SEOAUDIT_DASHBOARD_NINJA_SESSION_PATH", "/app/app_data/.ninja_session.json")
    )


def _safe_close(target: Page | BrowserContext) -> None:
    try:
        target.close()
    except Exception:  # noqa: BLE001 - closing an already-dead target is not a failure
        pass


def _page_is_usable(page: Page) -> bool:
    try:
        if page.is_closed():
            return False
        url = page.url or ""
    except Exception:  # noqa: BLE001 - a dead target raises on attribute access
        return False
    if any(marker in url for marker in _DEAD_URL_MARKERS):
        return False
    return _REPORT_HOST in url


def _is_login_page(page: Page) -> bool:
    try:
        return page.locator("input[type='password']").count() > 0
    except Exception:  # noqa: BLE001 - a dead target is not a login page either
        return False


def _login(page: Page, *, email: str, password: str) -> None:
    page.goto(f"{BASE_URL}/#!reports", wait_until="domcontentloaded")
    page.wait_for_selector("input.v-textfield", timeout=20_000)
    page.locator("input.v-textfield").first.fill(email)
    page.locator("input[type='password']").fill(password)
    page.locator("div.v-button.primary").click()
    page.wait_for_selector(".valo-menu-item", timeout=30_000)
    page.wait_for_timeout(_STEP_PAUSE_MS)


def _ensure_session(
    browser: Browser,
    ctx: BrowserContext,
    page: Page,
    *,
    email: str,
    password: str,
    session_path: Path,
    relogin_budget: list[int],
) -> tuple[BrowserContext, Page]:
    """Log in if there is no live session yet, or the tab has landed back on
    the login form. `relogin_budget` is a 1-item mutable list acting as a
    shared counter across the whole run — one automatic re-login total.
    """
    page.goto(f"{BASE_URL}/#!reports", wait_until="domcontentloaded")
    # The Vaadin SPA paints nothing meaningful at "domcontentloaded" — without
    # this wait, `_is_login_page` below race-checks a still-blank page and
    # always reads as "not a login page", even on a completely fresh,
    # unauthenticated context (confirmed live 2026-08-17: `list_report_names`
    # returned 0 rows because this function thought a brand-new session was
    # already logged in). `input.v-textfield` renders on both the login form
    # and the reports list, so it just proves the SPA has actually painted.
    page.wait_for_selector("input.v-textfield", timeout=20_000)
    if not _is_login_page(page):
        return ctx, page

    if relogin_budget[0] <= 0:
        raise AuthenticationFailedError(
            "Reporting Ninja session expired and the one automatic re-login "
            "for this run was already used"
        )
    relogin_budget[0] -= 1
    _login(page, email=email, password=password)
    ctx.storage_state(path=str(session_path))
    if _is_login_page(page):
        raise AuthenticationFailedError("Login submitted but the login form is still showing")
    return ctx, page


def _row_names(page: Page) -> list[str]:
    """The report names the cabinet's table currently shows, de-duplicated.

    Every report renders its label TWICE in this grid, so the raw read of
    32 reports is 64 strings; callers that count or compare want reports,
    not labels.
    """
    return list(
        dict.fromkeys(_clean_names(page.locator(_ROW_SELECTOR).all_text_contents()))
    )


def _clear_search(page: Page) -> None:
    """Empty the cabinet's search box and let the unfiltered list come back.

    `_go_to_reports`' `page.goto(".../#!reports")` cannot do this: the target
    differs from the current URL only in the FRAGMENT, so a Vaadin SPA
    already sitting on `#!reports` does not reload and the previous search
    stays applied. Measured live (2026-09-07): `discover_report_names` for
    "SDS" returned `all_names=['SDS-client', 'SDS-Internal report']` with
    `truncated=False` — the scroll pass had faithfully walked the still-
    FILTERED table and reported two reports as the whole account.
    """
    search_input = page.locator("input.v-textfield").first
    if not (search_input.input_value() or "").strip():
        return
    search_input.click(click_count=3)
    search_input.fill("")
    page.wait_for_timeout(_FILTER_SETTLE_MS)


def _go_to_reports(page: Page) -> None:
    page.goto(f"{BASE_URL}/#!reports", wait_until="domcontentloaded")
    page.wait_for_selector("input.v-textfield", timeout=20_000)
    page.wait_for_timeout(_STEP_PAUSE_MS)
    # A session that died between `_ensure_session` and here lands on the
    # login form, whose email box is ALSO `input.v-textfield` — so without
    # this check the caller types its query into the login form and reads
    # zero rows, which every reader downstream then treats as a truthful
    # "the account has no such report" (task-48 §1.7 names that inversion
    # exactly; nothing enforced it on this path).
    if _is_login_page(page):
        raise AuthenticationFailedError(
            "Reporting Ninja session is not live — the reports cabinet showed the login form"
        )
    _clear_search(page)


def _open_report(page: Page, report_name: str) -> None:
    """Open exactly `report_name`, or refuse to open anything.

    The row is picked by EXACT name, never as `.first` of whatever the table
    happens to show. `.first` was safe only for as long as the search filter
    was guaranteed to have landed, and it is not: with the filter a no-op
    (`cabinet_lock`), `.first` is `19CLV-client` — the account's
    alphabetically first report — so a run for SDS downloaded another
    client's PDF and every metric parsed out of it was stored under SDS.
    Nothing downstream could have caught that: the PDF is well-formed, it
    has all 8/8 metrics, and its filename comes from the argument, not from
    the report that was actually opened.
    """
    _go_to_reports(page)
    search_input = page.locator("input.v-textfield").first
    search_input.click(click_count=3)
    search_input.fill(report_name)
    page.wait_for_timeout(_FILTER_SETTLE_MS)
    page.locator(_ROW_SELECTOR).first.wait_for(state="visible", timeout=10_000)

    wanted = report_name.strip()
    row = None
    for candidate in page.locator(_ROW_SELECTOR).all():
        if (candidate.text_content() or "").strip() == wanted:
            row = candidate
            break
    if row is None:
        raise RuntimeError(
            f"Report {report_name!r} is not among the rows the Reporting Ninja search "
            f"returned for it ({_row_names(page)!r}) — refusing to open a different report"
        )
    row.dblclick()
    page.wait_for_timeout(_STEP_PAUSE_MS * 2)


def _set_popup_dates(page: Page, date_from: Any, date_to: Any) -> None:
    popup = page.locator("#ROOT-2521314-overlays .v-window.v-widget").last
    popup.wait_for(state="visible", timeout=10_000)

    popup.locator(".v-captiontext", has_text="Date range").click()
    page.wait_for_timeout(600)

    popup.locator("tr.v-formlayout-row").filter(has_text="Date range").locator(
        ".v-filterselect-button"
    ).click()
    page.wait_for_timeout(400)

    suggestions = page.locator(".v-filterselect-suggestpopup")
    suggestions.wait_for(state="visible", timeout=5_000)
    custom_option = suggestions.locator("td.gwt-MenuItem").filter(
        has_text=re.compile(r"[Cc]ustom")
    )
    if custom_option.count() == 0:
        raise RuntimeError("'Custom' date-range preset not found in Reporting Ninja's dropdown")
    custom_option.first.click()
    page.wait_for_timeout(400)

    for field_label, value in (("Start date", date_from), ("End date", date_to)):
        value_str = value.strftime("%m/%d/%Y")
        field_input = popup.locator("tr.v-formlayout-row").filter(has_text=field_label).locator(
            "input.v-textfield"
        )
        field_input.click()
        field_input.press("Control+a")
        field_input.press("Delete")
        field_input.type(value_str, delay=40)
        field_input.press("Tab")
        page.wait_for_timeout(300)

    popup.locator(".v-button-caption", has_text="Save").click()
    popup.wait_for(state="hidden", timeout=10_000)
    page.wait_for_timeout(_STEP_PAUSE_MS)


def _configure_dates(page: Page, date_from: Any, date_to: Any) -> None:
    page.locator("div.v-button:has-text('Edit template')").click()
    page.wait_for_timeout(_STEP_PAUSE_MS * 2)

    page.evaluate(
        """() => {
        const s = document.createElement('style');
        s.textContent =
            '.dashboard .headerContainer .v-component-group { display: inline-block !important; }' +
            '.dashboard .componentContent .v-component-group { display: inline-block !important; }';
        document.head.appendChild(s);
    }"""
    )
    page.wait_for_timeout(300)

    groups = page.locator(".v-slot-v-component-group:not(.v-slot-absoluteTopRight)")
    total = groups.count()
    for idx in range(min(2, total)):  # GA4=0, GSC=1
        group = groups.nth(idx)
        group.locator("div.v-button").first.click()
        page.wait_for_timeout(_STEP_PAUSE_MS)
        _set_popup_dates(page, date_from, date_to)

    page.go_back()
    page.wait_for_timeout(_STEP_PAUSE_MS * 2)


# Font Awesome "file-pdf-o" glyph (U+F1C1, private-use area) — the toolbar
# button carries no other stable attribute, only this icon's text content.
_PDF_GLYPH = ""

_FIND_PDF_BUTTON_JS = f"""() => {{
    const glyph = {_PDF_GLYPH!r};
    for (const span of document.querySelectorAll('span.v-icon.FontAwesome')) {{
        if (span.textContent === glyph) return span.closest('[role="button"]');
    }}
    return null;
}}"""


def _download_pdf(page: Page, out_path: Path) -> None:
    # Live measurement 2026-08-19: "Last Month" failed attempt 1/3 with
    # "PDF download button not found on the report page" and was never
    # retried (the error matched no recoverable classification below) —
    # the toolbar icon simply hadn't rendered yet when the one-shot
    # `evaluate` ran (a report-page render race, not a genuinely missing
    # button). Waiting for the button to actually exist removes the race
    # instead of just hoping the classification below catches it.
    page.wait_for_function(_FIND_PDF_BUTTON_JS, timeout=15_000)
    with page.expect_download(timeout=240_000) as download_info:
        clicked = page.evaluate(
            f"""() => {{
            const btn = ({_FIND_PDF_BUTTON_JS})();
            if (btn) {{ btn.click(); return true; }}
            return false;
        }}"""
        )
        if not clicked:
            raise RuntimeError("PDF download button not found on the report page")
    download_info.value.save_as(str(out_path))
    assert_pdf_complete(out_path)


def _download_one_period(
    page: Page, report_name: str, period: ReportPeriod, pdf_path: Path
) -> None:
    _open_report(page, report_name)
    _configure_dates(page, period.date_from, period.date_to)
    _open_report(page, report_name)  # editing does not auto-return to the report
    _download_pdf(page, pdf_path)


def _capture_failure_screenshot(page: Page, screenshots_dir: Path, name: str) -> Path | None:
    try:
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        path = screenshots_dir / f"{name}.png"
        page.screenshot(path=str(path))
        return path
    except Exception:  # noqa: BLE001 - a screenshot is best-effort diagnostics
        return None


def download_periods(  # noqa: PLR0913 - every argument is load-bearing, see docstring
    report_name: str,
    periods: list[ReportPeriod],
    out_dir: Path,
    *,
    emit: Emit,
    is_cancelled: Callable[[], bool],
    email: str,
    password: str,
    headless: bool = True,
    retries: int = 3,
    screenshots_dir: Path | None = None,
    session_path: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_period_state: PeriodStateCallback | None = None,
) -> list[DownloadedPeriod]:
    """Download one PDF per period. Returns only the periods that SUCCEEDED —
    a period absent from the result is a failure the caller already saw an
    `error` event for (task-42 §3.7.5: one period's failure never aborts the
    run). Raises `AuthenticationFailedError` if the session cannot be
    (re-)established at all, which DOES end the run early — every remaining
    period would fail at the login wall anyway.

    `on_period_state(label, state, attempt=, max_attempts=)` (additive,
    task-45 §2.1, attempt/max_attempts additive task-47) is called with
    `"running"` before EACH attempt (not just the first — a retry re-enters
    `"running"` with a bumped `attempt`, so the operator watches "retry 2/3"
    happen live instead of the row sitting on the first attempt's timestamp
    for the whole 3-attempt budget) and `"done"`/`"failed"` after the period
    settles. Best-effort narration: the caller's implementation swallows its
    own errors, and omitting the argument keeps the pre-task-47 behaviour.
    """
    notify: PeriodStateCallback = on_period_state or (lambda *_a, **_k: None)
    session_path = session_path or _session_path()
    out_dir.mkdir(parents=True, exist_ok=True)
    relogin_budget = [1]
    downloaded: list[DownloadedPeriod] = []

    # No timeout: a report run is the work every interactive cabinet lookup
    # should queue behind, not the other way round (`cabinet_lock`).
    with cabinet_lock(), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        ctx = new_stealth_context(
            browser, storage_state=str(session_path) if session_path.exists() else None,
        )
        page = ctx.new_page()
        ctx, page = _ensure_session(
            browser, ctx, page, email=email, password=password,
            session_path=session_path, relogin_budget=relogin_budget,
        )

        try:
            for period in periods:
                if is_cancelled():
                    emit("info", "cancelled", f"Cancelled before period {period.label!r}", {})
                    break

                pdf_path = out_dir / pdf_filename(
                    report_name, period.label, period.date_from, period.date_to
                )
                emit("info", "download", f"Downloading {period.label}", {"period": period.label})
                try:
                    ctx, page, final_attempt = _fetch_period_with_retry(
                        browser, ctx, page, report_name, period, pdf_path,
                        retries=retries, sleep=sleep, is_cancelled=is_cancelled,
                        email=email, password=password, session_path=session_path,
                        relogin_budget=relogin_budget, emit=emit, notify=notify,
                    )
                    downloaded.append(DownloadedPeriod(period, pdf_path))
                    notify(period.label, "done", attempt=final_attempt, max_attempts=retries)
                    emit("info", "download", f"{period.label} done", {"period": period.label})
                except AuthenticationFailedError:
                    raise
                except Exception as exc:  # noqa: BLE001 - classify, isolate, keep going
                    failed_attempt = getattr(exc, "attempt", retries)
                    notify(period.label, "failed", attempt=failed_attempt, max_attempts=retries)
                    screenshot = None
                    if screenshots_dir is not None and _page_is_usable(page):
                        screenshot = _capture_failure_screenshot(
                            page, screenshots_dir, f"{period.label}_failed"
                        )
                    emit(
                        "error", "download", f"{period.label} failed: {exc}",
                        {
                            "period": period.label,
                            "screenshot": str(screenshot) if screenshot else None,
                        },
                    )
        finally:
            _safe_close(page)
            _safe_close(ctx)
            browser.close()

    return downloaded


def _fetch_period_with_retry(  # noqa: PLR0913 - see download_periods
    browser: Browser,
    ctx: BrowserContext,
    page: Page,
    report_name: str,
    period: ReportPeriod,
    pdf_path: Path,
    *,
    retries: int,
    sleep: Callable[[float], None],
    is_cancelled: Callable[[], bool],
    email: str,
    password: str,
    session_path: Path,
    relogin_budget: list[int],
    emit: Emit,
    notify: PeriodStateCallback,
) -> tuple[BrowserContext, Page, int]:
    """Returns `(ctx, page, attempt)` — `attempt` is the 1-based attempt that
    finally succeeded, so the caller's `"done"` event/state can say which one.
    """
    for attempt in range(1, retries + 1):
        if is_cancelled():
            raise RuntimeError(f"Cancelled while downloading {period.label!r}")
        notify(period.label, "running", attempt=attempt, max_attempts=retries)
        try:
            _download_one_period(page, report_name, period, pdf_path)
            return ctx, page, attempt
        except Exception as exc:  # noqa: BLE001 - classify, then retry/re-login/raise
            if _is_login_page(page):
                ctx, page = _ensure_session(
                    browser, ctx, page, email=email, password=password,
                    session_path=session_path, relogin_budget=relogin_budget,
                )
                continue  # retry this same attempt slot, now authenticated

            # A `TimeoutError` here is Playwright's own — `_download_pdf`'s
            # `page.expect_download(timeout=240_000)` waited 4 minutes for a
            # click to trigger a download and nothing arrived. That is the
            # single most common transient failure against this site (the
            # report is just slow to render server-side for a wide date
            # range) — live-measured 2026-08-18: a quarter column failed on
            # attempt 1/3 and was never retried because this classification
            # excluded it, contradicting the "3 retries" the config promises.
            recoverable = (
                _is_transient_net_error(exc)
                or isinstance(exc, PdfDownloadIncomplete)
                or is_timeout_error(exc)
                # `_download_pdf`'s own button-not-found `RuntimeError` — the
                # `wait_for_function` before it already absorbs the common
                # render-race case (as a `TimeoutError`, already covered
                # above); this is the rarer case where the button vanished
                # again between that wait and the click itself.
                or "PDF download button not found" in str(exc)
                or ctx.is_closed()
                or not _page_is_usable(page)
            )
            if not recoverable or attempt == retries:
                failure = RuntimeError(
                    f"Period {period.label!r} did not download "
                    f"(attempt {attempt}/{retries}): {exc}"
                )
                failure.attempt = attempt  # type: ignore[attr-defined]
                raise failure from exc

            emit(
                "warning", "download",
                f"{period.label}: attempt {attempt}/{retries} failed ({exc}) — retrying",
                {"period": period.label, "attempt": attempt, "max_attempts": retries},
            )
            sleep(5.0 * attempt)
            if ctx.is_closed():
                ctx = new_stealth_context(browser, storage_state=str(session_path))
                page = ctx.new_page()
            elif not _page_is_usable(page):
                _safe_close(page)
                page = ctx.new_page()
    raise AssertionError("unreachable")  # pragma: no cover - loop always returns or raises


_MAX_SCROLL_ITERATIONS = 40
_SCROLL_STABLE_ROUNDS = 3


@dataclass(frozen=True)
class ReportDiscovery:
    """One combined cabinet lookup (task-48 §2.1) — `matches` from the
    cabinet's own search (never blind to virtualization: the server does the
    filtering), `all_names`/`truncated` from a full scroll-until-stable pass.
    """

    matches: list[str]
    all_names: list[str]
    truncated: bool


def _clean_names(raw: list[str]) -> list[str]:
    return [name.strip() for name in raw if name.strip()]


def _filter_landed(names: list[str], query: str) -> bool:
    """Is `names` a filtered answer to `query`, or the unfiltered table?

    An empty result is a legitimate filtered answer ("no such report"), so
    the signal is the opposite: a row that has nothing to do with the query
    proves the filter did NOT apply, because the server does the filtering.

    Substring containment is the test. Every query this module ever sends is
    either a client name or a whole report name, and this account names every
    report `<CLIENT>-client` / `<CLIENT>-Internal report`, so a legitimately
    matching row always contains the query. Should Reporting Ninja ever match
    on a field other than the name, this turns into a loud
    `CabinetFilterError` — deliberately preferred over the silent wrong answer
    it replaces (a false `report_name_unknown`, or `_open_report` opening
    another client's report).
    """
    needle = query.strip().casefold()
    return all(needle in name.casefold() for name in names)


def _search_names(page: Page, query: str) -> list[str]:
    """Whatever the cabinet's own search box returns for `query` — the same
    mechanism `_open_report` already uses to open a report by name (task-48
    §1.6/§2.1): the server filters, so a name past the table's first
    virtualized screen is found exactly as reliably as one on it.

    The row set is now VERIFIED to be the filtered one before it is returned
    (`_filter_landed`). The read used to be a single unconditional one 1200ms
    after typing, which is why a filter RPC that never landed came back as an
    ordinary-looking list of the account's first 15 reports — see
    `cabinet_lock` for the measurement and for both silent corruptions that
    list caused.
    """
    _go_to_reports(page)
    search_input = page.locator("input.v-textfield").first
    search_input.click(click_count=3)
    search_input.fill(query)
    page.wait_for_timeout(_FILTER_SETTLE_MS)

    names = _row_names(page)
    rereads = 0
    while not _filter_landed(names, query):
        if rereads >= _FILTER_MAX_REREADS:
            raise CabinetFilterError(
                f"The Reporting Ninja search for {query!r} did not filter the reports "
                f"table (still showing {len(names)} unrelated reports). The cabinet "
                "session is not usable for this lookup."
            )
        rereads += 1
        page.wait_for_timeout(_FILTER_RETRY_MS)
        names = _row_names(page)
    return names


def _scroll_reports_list(page: Page) -> None:
    """Scroll the virtualized reports table by one more screen so the next
    read sees rows that were not painted yet (task-48 §1.6: a Vaadin grid
    only ever renders what is currently in view).
    """
    page.evaluate(
        """() => {
        const rows = document.querySelectorAll('div.v-label.cursorPointer');
        const last = rows[rows.length - 1];
        if (last) last.scrollIntoView({block: 'end'});
    }"""
    )


def _scroll_all_names(
    page: Page,
    *,
    max_iterations: int = _MAX_SCROLL_ITERATIONS,
    stable_rounds: int = _SCROLL_STABLE_ROUNDS,
) -> tuple[list[str], bool]:
    """Every report name the cabinet has, scrolling until the visible set
    stops growing for `stable_rounds` consecutive reads (task-48 §2.1).
    Returns `(names, truncated)` — `truncated=True` means the iteration
    budget ran out before the set stabilized, which the caller MUST treat as
    "could not verify", never as proof a name is absent (task-48 §1.7: an
    unproven absence is not evidence for `report_name_unknown`).
    """
    _go_to_reports(page)
    seen: list[str] = []
    seen_set: set[str] = set()
    stable_count = 0
    truncated = True
    for _ in range(max_iterations):
        names = _row_names(page)
        grew = False
        for name in names:
            if name not in seen_set:
                seen_set.add(name)
                seen.append(name)
                grew = True
        if grew:
            stable_count = 0
        else:
            stable_count += 1
            if stable_count >= stable_rounds:
                truncated = False
                break
        _scroll_reports_list(page)
        page.wait_for_timeout(400)
    return seen, truncated


def search_report_names(
    *,
    email: str,
    password: str,
    headless: bool = True,
    session_path: Path | None = None,
    query: str,
    lock_timeout_seconds: float | None = _INTERACTIVE_LOCK_TIMEOUT,
) -> list[str]:
    """Own-session wrapper around `_search_names` (task-48 §2.1 point 1)."""
    session_path = session_path or _session_path()
    with cabinet_lock(timeout_seconds=lock_timeout_seconds), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        ctx = new_stealth_context(
            browser, storage_state=str(session_path) if session_path.exists() else None,
        )
        page = ctx.new_page()
        try:
            ctx, page = _ensure_session(
                browser, ctx, page, email=email, password=password,
                session_path=session_path, relogin_budget=[1],
            )
            return _search_names(page, query)
        finally:
            _safe_close(page)
            _safe_close(ctx)
            browser.close()


def list_report_names(
    *,
    email: str,
    password: str,
    headless: bool = True,
    session_path: Path | None = None,
    max_iterations: int = _MAX_SCROLL_ITERATIONS,
    stable_rounds: int = _SCROLL_STABLE_ROUNDS,
    lock_timeout_seconds: float | None = _INTERACTIVE_LOCK_TIMEOUT,
) -> tuple[list[str], bool]:
    """Own-session wrapper around `_scroll_all_names` (task-48 §2.1 point 2):
    every report name in the Reporting Ninja cabinet, or as many as a bounded
    scroll pass found. Requires only a login, no report selection.

    Replaces task-42's single `all_text_contents()` read, which only ever
    saw whatever the virtualized table happened to have painted — measured
    live at 32 of the account's real report names (task-48 §1.6), silently
    excluding every client whose name sorts after that point.
    """
    session_path = session_path or _session_path()
    with cabinet_lock(timeout_seconds=lock_timeout_seconds), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        ctx = new_stealth_context(
            browser, storage_state=str(session_path) if session_path.exists() else None,
        )
        page = ctx.new_page()
        try:
            ctx, page = _ensure_session(
                browser, ctx, page, email=email, password=password,
                session_path=session_path, relogin_budget=[1],
            )
            return _scroll_all_names(
                page, max_iterations=max_iterations, stable_rounds=stable_rounds
            )
        finally:
            _safe_close(page)
            _safe_close(ctx)
            browser.close()


def discover_report_names(
    *,
    email: str,
    password: str,
    headless: bool = True,
    session_path: Path | None = None,
    client_name: str,
    max_iterations: int = _MAX_SCROLL_ITERATIONS,
    stable_rounds: int = _SCROLL_STABLE_ROUNDS,
    lock_timeout_seconds: float | None = _INTERACTIVE_LOCK_TIMEOUT,
) -> ReportDiscovery:
    """One login, both discovery passes (task-48 §2.1) — used by the
    `GET /clients/{id}/ninja-report` endpoint so a targeted search and a full
    scroll never cost two separate browser sessions.
    """
    session_path = session_path or _session_path()
    with cabinet_lock(timeout_seconds=lock_timeout_seconds), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        ctx = new_stealth_context(
            browser, storage_state=str(session_path) if session_path.exists() else None,
        )
        page = ctx.new_page()
        try:
            ctx, page = _ensure_session(
                browser, ctx, page, email=email, password=password,
                session_path=session_path, relogin_budget=[1],
            )
            matches = _search_names(page, client_name)
            all_names, truncated = _scroll_all_names(
                page, max_iterations=max_iterations, stable_rounds=stable_rounds
            )
        finally:
            _safe_close(page)
            _safe_close(ctx)
            browser.close()
    return ReportDiscovery(matches=matches, all_names=all_names, truncated=truncated)
