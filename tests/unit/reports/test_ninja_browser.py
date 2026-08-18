from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.reports import ninja_browser as nb
from app.reports.periods import ReportPeriod


class FakeLocator:
    def __init__(self, count: int = 0, texts: list[str] | None = None) -> None:
        self._count = count
        self._texts = texts or []
        self.filled: str | None = None

    def count(self) -> int:
        return self._count

    def all_text_contents(self) -> list[str]:
        return self._texts

    @property
    def first(self) -> FakeLocator:
        return self

    def click(self, *_args, **_kwargs) -> None:
        pass

    def fill(self, value: str) -> None:
        self.filled = value


class FakePage:
    def __init__(
        self, *, url: str = "https://app.reportingninja.com/#!reports",
        closed: bool = False, password_count: int = 0, report_names: list[str] | None = None,
        report_names_by_screen: list[list[str]] | None = None,
    ) -> None:
        self.url = url
        self._closed = closed
        self._password_count = password_count
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
            return self._report_names
        index = min(self._screen_index, len(self._report_names_by_screen) - 1)
        return self._report_names_by_screen[index]

    def locator(self, selector: str) -> FakeLocator:
        if selector == "input[type='password']":
            return FakeLocator(self._password_count)
        if selector == "input.v-textfield":
            return self.search_input
        if selector == "div.v-label.cursorPointer":
            names = self._current_report_names()
            return FakeLocator(len(names), names)
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
    raises Playwright's OWN `TimeoutError` (a different class from the
    builtin `TimeoutError` the test right below this one exercises) when the
    site is just slow to render a large period's PDF. That used to fail the
    period on attempt 1/3 despite the config promising 3 retries; it must
    now be treated as recoverable, same as a transient network error.
    """
    calls = {"n": 0}

    def fake_download(page, report_name, period, pdf_path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise nb.PlaywrightTimeoutError(
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


def test_a_live_selector_timeout_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def fake_download(page, report_name, period, pdf_path):
        calls["n"] += 1
        raise TimeoutError("Timeout 30000ms exceeded")

    monkeypatch.setattr(nb, "_download_one_period", fake_download)
    monkeypatch.setattr(nb, "_is_login_page", lambda _page: False)
    monkeypatch.setattr(nb, "_page_is_usable", lambda _page: True)

    browser, ctx, page = FakeBrowser(), FakeContext(), FakePage()
    with pytest.raises(RuntimeError, match="did not download"):
        nb._fetch_period_with_retry(
            browser, ctx, page, "EFFYS-Internal report", _PERIOD, Path("/tmp/x.pdf"),
            **_retry_kwargs(),
        )
    assert calls["n"] == 1  # never retried


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
    assert result.matches == ["SWM-client", "EFFYS-Internal report"]
    assert result.all_names == ["SWM-client", "EFFYS-Internal report"]
    assert result.truncated is False
    assert calls == ["launch"]  # exactly one browser session for both passes


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
