from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.reports import ninja_browser as nb
from app.reports.periods import ReportPeriod


class FakeLocator:
    def __init__(
        self,
        count: int = 0,
        texts: list[str] | None = None,
        on_dblclick: Callable[[str], None] | None = None,
    ) -> None:
        self._count = count
        self._texts = texts or []
        self.filled: str | None = None
        self._on_dblclick = on_dblclick

    def count(self) -> int:
        return self._count

    def all_text_contents(self) -> list[str]:
        return self._texts

    def all(self) -> list[FakeLocator]:
        return [FakeLocator(1, [text], self._on_dblclick) for text in self._texts]

    def text_content(self) -> str | None:
        return self._texts[0] if self._texts else None

    @property
    def first(self) -> FakeLocator:
        return self

    def click(self, *_args, **_kwargs) -> None:
        pass

    def wait_for(self, *_args, **_kwargs) -> None:
        pass

    def dblclick(self, *_args, **_kwargs) -> None:
        if self._on_dblclick is not None:
            self._on_dblclick(self._texts[0] if self._texts else "")

    def fill(self, value: str) -> None:
        self.filled = value

    def input_value(self) -> str:
        return self.filled or ""


class FakePage:
    def __init__(
        self, *, url: str = "https://app.reportingninja.com/#!reports",
        closed: bool = False, password_count: int = 0, report_names: list[str] | None = None,
        report_names_by_screen: list[list[str]] | None = None,
        filter_broken: bool = False,
    ) -> None:
        self.url = url
        self._closed = closed
        self._password_count = password_count
        # The live corruption two concurrent cabinet sessions cause
        # (`ninja_browser.cabinet_lock`): the search box accepts the query,
        # the filter RPC is a silent no-op, and the table keeps showing the
        # whole unfiltered first screen.
        self._filter_broken = filter_broken
        self.opened: list[str] = []
        self._report_names = report_names or []
        # Simulates a virtualized table's growing DOM as it scrolls
        # (task-48 §2.1): each `.locator(...).all_text_contents()` call
        # advances one screen further, stopping at the last one.
        self._report_names_by_screen = report_names_by_screen
        self._screen_index = 0
        self.search_input = FakeLocator(0)

    def is_closed(self) -> bool:
        return self._closed

    def _current_report_names(self) -> list[str]:
        if self._report_names_by_screen is None:
            names = self._report_names
        else:
            index = min(self._screen_index, len(self._report_names_by_screen) - 1)
            names = self._report_names_by_screen[index]
        # A real cabinet filters SERVER-side on whatever is in the search
        # box, so the fake does too — otherwise a test cannot tell a
        # genuinely filtered read from the unfiltered one that the old code
        # accepted as a search result.
        query = (self.search_input.filled or "").strip()
        if query and not self._filter_broken:
            return [name for name in names if query.casefold() in name.casefold()]
        return names

    def locator(self, selector: str) -> FakeLocator:
        if selector == "input[type='password']":
            return FakeLocator(self._password_count)
        if selector == "input.v-textfield":
            return self.search_input
        if selector == "div.v-label.cursorPointer":
            names = self._current_report_names()
            return FakeLocator(len(names), names, self.opened.append)
        return FakeLocator(0)

    def wait_for_selector(self, *_args, **_kwargs) -> None:
        pass

    def wait_for_timeout(self, *_args, **_kwargs) -> None:
        pass

    def goto(self, *_args, **_kwargs) -> None:
        pass

    def screenshot(self, *_args, **_kwargs) -> None:
        pass

    def evaluate(self, *_args, **_kwargs) -> None:
        # Simulates the scroll advancing the virtualized table one screen.
        if self._report_names_by_screen is not None:
            self._screen_index = min(
                self._screen_index + 1, len(self._report_names_by_screen) - 1
            )


class FakeContext:
    def __init__(self) -> None:
        self._closed = False

    def is_closed(self) -> bool:
        return self._closed

    def new_page(self) -> FakePage:
        return FakePage()

    def storage_state(self, path: str | None = None) -> None:
        pass

    def add_init_script(self, script: str) -> None:
        pass

    def close(self) -> None:
        self._closed = True


class FakeBrowser:
    def new_context(self, **_kwargs) -> FakeContext:
        return FakeContext()

    def close(self) -> None:
        pass


# --- classification helpers ---------------------------------------------------


def test_transient_net_error_is_recognized():
    assert nb._is_transient_net_error(RuntimeError("net::ERR_NETWORK_CHANGED at ..."))


def test_a_real_selector_timeout_is_not_transient():
    assert not nb._is_transient_net_error(TimeoutError("Timeout 30000ms exceeded waiting for X"))


def test_page_on_the_report_host_is_usable():
    assert nb._page_is_usable(FakePage(url="https://app.reportingninja.com/#!reports"))


def test_closed_page_is_not_usable():
    assert not nb._page_is_usable(FakePage(closed=True))


def test_dead_download_url_is_not_usable():
    assert not nb._page_is_usable(FakePage(url="https://app.reportingninja.com/downloadreport"))


def test_login_page_is_detected_by_password_field():
    assert nb._is_login_page(FakePage(password_count=1))
    assert not nb._is_login_page(FakePage(password_count=0))


# --- _fetch_period_with_retry --------------------------------------------------

_PERIOD = ReportPeriod("This Month", date(2026, 7, 1), date(2026, 7, 31))


def _retry_kwargs(**overrides):
    kwargs = dict(
        retries=3,
        sleep=lambda _seconds: None,
        is_cancelled=lambda: False,
        email="ops@example.com",
        password="secret",
        session_path=Path("/tmp/does-not-exist.json"),
        relogin_budget=[1],
        emit=lambda *_a, **_k: None,
        notify=lambda *_a, **_k: None,
    )
    kwargs.update(overrides)
    return kwargs


def test_transient_error_is_retried_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_download(page, report_name, period, pdf_path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("net::ERR_NETWORK_CHANGED")

    monkeypatch.setattr(nb, "_download_one_period", fake_download)
    monkeypatch.setattr(nb, "_is_login_page", lambda _page: False)

    browser, ctx, page = FakeBrowser(), FakeContext(), FakePage()
    nb._fetch_period_with_retry(
        browser, ctx, page, "EFFYS-Internal report", _PERIOD, Path("/tmp/x.pdf"),
        **_retry_kwargs(),
    )
    assert calls["n"] == 2


def test_a_playwright_download_timeout_is_retried_then_succeeds(monkeypatch):
    """task-47: live-found bug — `page.expect_download(timeout=240_000)`
    raises Playwright's OWN `TimeoutError` when the site is just slow to
    render a large period's PDF. That used to fail the period on attempt
    1/3 despite the config promising 3 retries; it must now be treated as
    recoverable, same as a transient network error. Classified by CLASS
    NAME (`stealth_browser.is_timeout_error`), not `isinstance` against one
    specific package — `patchright` (task-47 follow-up "stealth" fix) raises
    its OWN distinct `TimeoutError` class, not playwright's.
    """
    calls = {"n": 0}

    def fake_download(page, report_name, period, pdf_path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PlaywrightTimeoutError(
                'Timeout 240000ms exceeded while waiting for event "download"'
            )

    monkeypatch.setattr(nb, "_download_one_period", fake_download)
    monkeypatch.setattr(nb, "_is_login_page", lambda _page: False)

    browser, ctx, page = FakeBrowser(), FakeContext(), FakePage()
    _ctx, _page, attempt = nb._fetch_period_with_retry(
        browser, ctx, page, "EFFYS-Internal report", _PERIOD, Path("/tmp/x.pdf"),
        **_retry_kwargs(),
    )
    assert calls["n"] == 2
    assert attempt == 2


def test_a_missing_pdf_button_is_now_retried_not_treated_as_a_permanent_bug(monkeypatch):
    """task-48 follow-up: live-found bug — "Last Month" failed attempt 1/3
    with "PDF download button not found on the report page" and was never
    retried, burning the whole period on what turned out to be a one-off
    report-page render race (the toolbar icon hadn't painted yet when
    `_download_pdf`'s one-shot `evaluate` ran), not a genuine selector-drift
    bug. `_download_pdf` itself now waits for the button before clicking
    (`page.wait_for_function`); this test covers the belt-and-braces case —
    the same message reaching `_fetch_period_with_retry` directly must be
    retried, same as every other transient failure here.
    """
    calls = {"n": 0}

    def fake_download(page, report_name, period, pdf_path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("PDF download button not found on the report page")

    monkeypatch.setattr(nb, "_download_one_period", fake_download)
    monkeypatch.setattr(nb, "_is_login_page", lambda _page: False)
    monkeypatch.setattr(nb, "_page_is_usable", lambda _page: True)

    browser, ctx, page = FakeBrowser(), FakeContext(), FakePage()
    nb._fetch_period_with_retry(
        browser, ctx, page, "EFFYS-Internal report", _PERIOD, Path("/tmp/x.pdf"),
        **_retry_kwargs(),
    )
    assert calls["n"] == 2


# --- _download_pdf: waits for the button instead of a one-shot lookup ---------


class _DownloadPage:
    """Fake `Page` covering exactly the calls `_download_pdf` makes, in
    order — the assertion is on `calls`, not on Playwright's own behavior.
    """

    class _DownloadHandle:
        def __init__(self, path: Path) -> None:
            self._path = path

        def save_as(self, dest: str) -> None:
            Path(dest).write_bytes(b"0" * 100_000)  # > MIN_PDF_BYTES

    class _ExpectDownload:
        def __init__(self, page: _DownloadPage) -> None:
            self._page = page

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        @property
        def value(self):
            return _DownloadPage._DownloadHandle(self._page._out_path)

    def __init__(self, *, button_found: bool = True) -> None:
        self.calls: list[str] = []
        self._button_found = button_found
        self._out_path = Path()

    def wait_for_function(self, *_args, **_kwargs) -> None:
        self.calls.append("wait_for_function")

    def expect_download(self, *_args, **_kwargs):
        self.calls.append("expect_download")
        return self._ExpectDownload(self)

    def evaluate(self, *_args, **_kwargs) -> bool:
        self.calls.append("evaluate")
        return self._button_found


def test_download_pdf_waits_for_the_button_before_clicking(tmp_path):
    # task-48 follow-up: a live period ("Last Month") failed because the
    # toolbar's PDF icon hadn't rendered yet when a one-shot `evaluate` ran
    # right after opening the report — `wait_for_function` must run BEFORE
    # `expect_download`/`evaluate`, not after.
    page = _DownloadPage()
    page._out_path = tmp_path / "out.pdf"
    nb._download_pdf(page, page._out_path)
    assert page.calls == ["wait_for_function", "expect_download", "evaluate"]
    assert page._out_path.exists()


def test_download_pdf_raises_when_the_button_vanishes_after_the_wait(tmp_path):
    # The rare edge case `wait_for_function` itself doesn't cover: the
    # button existed when checked, then the click-time `evaluate` still came
    # up empty. Must raise (not silently produce an empty/missing file) —
    # and `_fetch_period_with_retry` already treats this message as
    # recoverable (see the button-retry test above).
    page = _DownloadPage(button_found=False)
    page._out_path = tmp_path / "out.pdf"
    with pytest.raises(RuntimeError, match="PDF download button not found"):
        nb._download_pdf(page, page._out_path)


def test_a_truncated_download_is_retried(monkeypatch):
    calls = {"n": 0}

    def fake_download(page, report_name, period, pdf_path):
        calls["n"] += 1
        if calls["n"] < 3:
            raise nb.PdfDownloadIncomplete("x.pdf: 100 B")

    monkeypatch.setattr(nb, "_download_one_period", fake_download)
    monkeypatch.setattr(nb, "_is_login_page", lambda _page: False)

    browser, ctx, page = FakeBrowser(), FakeContext(), FakePage()
    nb._fetch_period_with_retry(
        browser, ctx, page, "EFFYS-Internal report", _PERIOD, Path("/tmp/x.pdf"),
        **_retry_kwargs(retries=3),
    )
    assert calls["n"] == 3


def test_exhausting_all_retries_raises_with_the_period_label(monkeypatch):
    monkeypatch.setattr(
        nb, "_download_one_period",
        lambda *a: (_ for _ in ()).throw(nb.PdfDownloadIncomplete("always fails")),
    )
    monkeypatch.setattr(nb, "_is_login_page", lambda _page: False)

    browser, ctx, page = FakeBrowser(), FakeContext(), FakePage()
    with pytest.raises(RuntimeError, match="This Month"):
        nb._fetch_period_with_retry(
            browser, ctx, page, "EFFYS-Internal report", _PERIOD, Path("/tmp/x.pdf"),
            **_retry_kwargs(retries=2),
        )


def test_cancellation_is_checked_before_each_attempt(monkeypatch):
    monkeypatch.setattr(
        nb, "_download_one_period",
        lambda *a: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    browser, ctx, page = FakeBrowser(), FakeContext(), FakePage()
    with pytest.raises(RuntimeError, match="Cancelled"):
        nb._fetch_period_with_retry(
            browser, ctx, page, "EFFYS-Internal report", _PERIOD, Path("/tmp/x.pdf"),
            **_retry_kwargs(is_cancelled=lambda: True),
        )


# --- _ensure_session: exactly one automatic re-login --------------------------


def test_no_relogin_when_the_session_is_already_live(monkeypatch):
    monkeypatch.setattr(nb, "_is_login_page", lambda _page: False)
    monkeypatch.setattr(nb, "_login", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no")))

    browser, ctx, page = FakeBrowser(), FakeContext(), FakePage()
    budget = [1]
    nb._ensure_session(
        browser, ctx, page, email="a", password="b",
        session_path=Path("/tmp/x.json"), relogin_budget=budget,
    )
    assert budget == [1]  # untouched


def test_one_relogin_is_spent_and_then_the_budget_is_exhausted(monkeypatch):
    monkeypatch.setattr(nb, "_is_login_page", lambda _page: True)
    login_calls = {"n": 0}

    def fake_login(*_args, **_kwargs):
        login_calls["n"] += 1

    monkeypatch.setattr(nb, "_login", fake_login)

    browser, ctx, page = FakeBrowser(), FakeContext(), FakePage()
    budget = [1]
    with pytest.raises(nb.AuthenticationFailedError, match="still showing"):
        nb._ensure_session(
            browser, ctx, page, email="a", password="b",
            session_path=Path("/tmp/x.json"), relogin_budget=budget,
        )
    assert login_calls["n"] == 1
    assert budget == [0]


def test_a_second_relogin_in_the_same_run_is_refused(monkeypatch):
    monkeypatch.setattr(nb, "_is_login_page", lambda _page: True)
    browser, ctx, page = FakeBrowser(), FakeContext(), FakePage()
    budget = [0]  # already spent by an earlier call this run
    with pytest.raises(nb.AuthenticationFailedError, match="already used"):
        nb._ensure_session(
            browser, ctx, page, email="a", password="b",
            session_path=Path("/tmp/x.json"), relogin_budget=budget,
        )


# --- download_periods: one bad period never aborts the run --------------------


def test_one_failed_period_does_not_abort_the_rest(monkeypatch, tmp_path):
    monkeypatch.setattr(nb, "sync_playwright", _fake_sync_playwright)
    monkeypatch.setattr(nb, "_ensure_session", lambda browser, ctx, page, **k: (ctx, page))

    periods = [
        ReportPeriod("This Month", date(2026, 7, 1), date(2026, 7, 31)),
        ReportPeriod("Last Month", date(2026, 6, 1), date(2026, 6, 30)),
    ]

    def fake_fetch(browser, ctx, page, report_name, period, pdf_path, **kwargs):
        if period.label == "This Month":
            raise RuntimeError("selector drift")
        pdf_path.write_bytes(b"0" * 100)
        return ctx, page, 1

    monkeypatch.setattr(nb, "_fetch_period_with_retry", fake_fetch)

    events: list[tuple] = []
    result = nb.download_periods(
        "EFFYS-Internal report", periods, tmp_path,
        emit=lambda level, stage, message, details: events.append((level, stage, message)),
        is_cancelled=lambda: False,
        email="a", password="b",
        session_path=tmp_path / "session.json",
    )

    assert [d.period.label for d in result] == ["Last Month"]
    assert any(level == "error" and "This Month" in message for level, _stage, message in events)


# --- list_report_names: full scroll-until-stable discovery --------------------


def test_list_report_names_returns_every_name_once_the_scroll_stabilizes(monkeypatch, tmp_path):
    # A virtualized table that only ever painted 32 names past a certain
    # point in the account (task-48 §1.6 live measurement) — scrolling
    # reveals the rest, and the read stabilizes once nothing new appears.
    screens = [
        ["EFFYS-Internal report", "SAPHNY-Internal report"],
        ["EFFYS-Internal report", "SAPHNY-Internal report", "3XE-client"],
        ["EFFYS-Internal report", "SAPHNY-Internal report", "3XE-client"],  # stable
        ["EFFYS-Internal report", "SAPHNY-Internal report", "3XE-client"],  # stable
        ["EFFYS-Internal report", "SAPHNY-Internal report", "3XE-client"],  # stable
    ]
    monkeypatch.setattr(nb, "sync_playwright", _fake_sync_playwright)
    monkeypatch.setattr(
        FakeContext, "new_page", lambda self: FakePage(report_names_by_screen=screens)
    )
    monkeypatch.setattr(nb, "_ensure_session", lambda browser, ctx, page, **k: (ctx, page))

    names, truncated = nb.list_report_names(
        email="a", password="b", session_path=tmp_path / "session.json"
    )
    assert names == ["EFFYS-Internal report", "SAPHNY-Internal report", "3XE-client"]
    assert truncated is False


def test_list_report_names_reports_truncated_when_the_scroll_never_stabilizes(
    monkeypatch, tmp_path
):
    # Every read keeps finding one more name — the iteration budget runs out
    # first. `truncated=True` must never become grounds for
    # `report_name_unknown` (task-48 §1.7).
    screens = [[f"Client-{i}"] for i in range(1, 200)]
    monkeypatch.setattr(nb, "sync_playwright", _fake_sync_playwright)
    monkeypatch.setattr(
        FakeContext, "new_page", lambda self: FakePage(report_names_by_screen=screens)
    )
    monkeypatch.setattr(nb, "_ensure_session", lambda browser, ctx, page, **k: (ctx, page))

    names, truncated = nb.list_report_names(
        email="a", password="b", session_path=tmp_path / "session.json", max_iterations=10
    )
    assert truncated is True
    assert len(names) == 10


# --- search_report_names: finds a name past the first screen ------------------


def test_search_report_names_finds_a_client_the_first_screen_never_shows(monkeypatch, tmp_path):
    # task-48 §1.6: HWELL/LAXTON/.../SWM sort after the table's first
    # virtualized screen and never appear in a plain `all_text_contents()`
    # read — the search box, which the server itself filters, finds them
    # anyway (the same mechanism `_open_report` already relies on).
    monkeypatch.setattr(nb, "sync_playwright", _fake_sync_playwright)
    monkeypatch.setattr(
        FakeContext, "new_page", lambda self: FakePage(report_names=["SWM-client"])
    )
    monkeypatch.setattr(nb, "_ensure_session", lambda browser, ctx, page, **k: (ctx, page))

    result = nb.search_report_names(
        email="a", password="b", session_path=tmp_path / "session.json", query="SWM"
    )
    assert result == ["SWM-client"]


# --- discover_report_names: one session, both passes ---------------------------


def test_discover_report_names_combines_search_and_full_scroll_in_one_session(
    monkeypatch, tmp_path
):
    calls: list[str] = []
    original_launch = _FakePlaywrightContextManager.launch

    def counting_launch(self, **kwargs):
        calls.append("launch")
        return original_launch(self, **kwargs)

    monkeypatch.setattr(nb, "sync_playwright", _fake_sync_playwright)
    monkeypatch.setattr(_FakePlaywrightContextManager, "launch", counting_launch)
    monkeypatch.setattr(
        FakeContext,
        "new_page",
        lambda self: FakePage(report_names=["SWM-client", "EFFYS-Internal report"]),
    )
    monkeypatch.setattr(nb, "_ensure_session", lambda browser, ctx, page, **k: (ctx, page))

    result = nb.discover_report_names(
        email="a", password="b", session_path=tmp_path / "session.json", client_name="SWM",
    )
    # The search half returns ONLY this client's reports. This assertion used
    # to read `["SWM-client", "EFFYS-Internal report"]` — an unfiltered table
    # accepted as a search result, which is the live defect itself written
    # down as the expectation.
    assert result.matches == ["SWM-client"]
    # ...and the scroll half still sees the whole account, because
    # `_go_to_reports` now clears the search box. Before that fix this came
    # back as `["SWM-client"]` with `truncated=False`: one report presented
    # as the entire cabinet, measured live for real on 2026-09-07.
    assert result.all_names == ["SWM-client", "EFFYS-Internal report"]
    assert result.truncated is False
    assert calls == ["launch"]  # exactly one browser session for both passes


# --- the concurrency corruption, and the guards that make it loud ------------


def test_a_search_whose_filter_never_landed_raises_instead_of_answering(monkeypatch, tmp_path):
    """The measured failure: two cabinet sessions at once, the filter RPC is a
    no-op, and the read returns the account's unfiltered first screen. Taken
    as a search result it made `_cabinet_has_report_name` report every client
    sorting past "G" as absent — the live `report_name_unknown`.
    """
    monkeypatch.setattr(nb, "sync_playwright", _fake_sync_playwright)
    monkeypatch.setattr(
        FakeContext,
        "new_page",
        lambda self: FakePage(
            report_names=["19CLV-client", "3XE-client", "GRP-client"], filter_broken=True
        ),
    )
    monkeypatch.setattr(nb, "_ensure_session", lambda browser, ctx, page, **k: (ctx, page))

    with pytest.raises(nb.CabinetFilterError):
        nb.search_report_names(
            email="a", password="b", session_path=tmp_path / "session.json", query="SDS-client"
        )


def test_an_empty_search_result_is_a_legitimate_answer(monkeypatch, tmp_path):
    # "No such report" must stay expressible — the filter-landed check keys
    # off rows that contradict the query, not off the result being empty.
    monkeypatch.setattr(nb, "sync_playwright", _fake_sync_playwright)
    monkeypatch.setattr(
        FakeContext, "new_page", lambda self: FakePage(report_names=["19CLV-client"])
    )
    monkeypatch.setattr(nb, "_ensure_session", lambda browser, ctx, page, **k: (ctx, page))

    assert nb.search_report_names(
        email="a", password="b", session_path=tmp_path / "session.json", query="NOPE"
    ) == []


def test_open_report_refuses_to_open_a_different_report(monkeypatch):
    """The severe one. With the filter a no-op, `.first` was `19CLV-client`,
    so a PDF run for SDS downloaded another client's report and every metric
    parsed out of it was stored under SDS — a well-formed PDF with all 8/8
    metrics and a filename taken from the argument, so nothing downstream
    could ever have noticed.
    """
    # The live shape: the unfiltered screen is the account's first 15 reports,
    # cut off alphabetically at `GRP-client`, and `SDS-client` is past it.
    page = FakePage(
        report_names=["19CLV-client", "3XE-client", "GRP-client"], filter_broken=True
    )
    with pytest.raises(RuntimeError, match="refusing to open a different report"):
        nb._open_report(page, "SDS-client")
    assert page.opened == []  # `.first` would have opened 19CLV-client


def test_open_report_opens_the_row_whose_name_matches_exactly(monkeypatch):
    page = FakePage(report_names=["SDS-client", "SDS-Internal report"])
    nb._open_report(page, "SDS-Internal report")
    assert page.opened == ["SDS-Internal report"]


def test_landing_on_the_login_form_is_an_auth_failure_not_an_empty_account(monkeypatch):
    """A session that died after `_ensure_session` puts the cabinet back on
    the login form, whose email box is ALSO `input.v-textfield` — so the
    caller used to type its query into the login form, read zero rows, and
    hand that up as a truthful "the account has no such report".
    """
    page = FakePage(password_count=1, report_names=[])
    with pytest.raises(nb.AuthenticationFailedError):
        nb._go_to_reports(page)


class _FakePlaywrightContextManager:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    @property
    def chromium(self):
        return self

    def launch(self, **_kwargs) -> FakeBrowser:
        return FakeBrowser()


def _fake_sync_playwright() -> _FakePlaywrightContextManager:
    return _FakePlaywrightContextManager()


# --- cabinet_lock: the invariant the cabinet was missing ----------------------


def test_cabinet_lock_excludes_a_second_holder(monkeypatch, tmp_path):
    monkeypatch.setenv("SEOAUDIT_DASHBOARD_NINJA_SESSION_PATH", str(tmp_path / "s.json"))
    with nb.cabinet_lock(timeout_seconds=0.1):
        with pytest.raises(nb.CabinetBusyError):
            with nb.cabinet_lock(timeout_seconds=0.1):
                pass


def test_cabinet_lock_is_free_again_after_its_holder_leaves(monkeypatch, tmp_path):
    monkeypatch.setenv("SEOAUDIT_DASHBOARD_NINJA_SESSION_PATH", str(tmp_path / "s.json"))
    with nb.cabinet_lock(timeout_seconds=0.1):
        pass
    with nb.cabinet_lock(timeout_seconds=0.1):
        pass


def test_cabinet_lock_releases_even_when_the_body_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("SEOAUDIT_DASHBOARD_NINJA_SESSION_PATH", str(tmp_path / "s.json"))
    with pytest.raises(ValueError, match="boom"), nb.cabinet_lock(timeout_seconds=0.1):
        raise ValueError("boom")
    with nb.cabinet_lock(timeout_seconds=0.1):
        pass
