"""Tests for the browser session bridge (task-20).

AISeoTools (`seo_audit.browser.profile`, `.warm`, `.stealth`) is monkeypatched throughout — these
tests cover only this module's own allowlisting, state mapping and CDP-attach plumbing, not
AISeoTools' own warm-Chrome/auth internals (covered by AISeoTools' own test suite).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

import pytest

from app.integrations import browser_session as bs
from app.schemas.enums import Provider


@dataclass
class _FakeWarmStatus:
    running: bool
    authenticated: bool | None = None
    busy: bool = False


class _FakeProfile:
    def __init__(self, provider: str, *, exists: bool = True):
        self.provider = provider
        self.exists = exists
        self.login_url = f"https://example.test/{provider}/login"
        self.busy_lock_calls = 0

    @contextmanager
    def busy_lock(self):
        self.busy_lock_calls += 1
        yield


def _patch_profile(monkeypatch, profile: _FakeProfile) -> None:
    monkeypatch.setattr(
        "seo_audit.browser.profile.BrowserProfile", lambda provider: profile
    )


@pytest.fixture(autouse=True)
def _clear_active_logins():
    bs._active_logins.clear()
    yield
    bs._active_logins.clear()


class TestAllowlist:
    def test_reporting_ninja_rejected(self):
        with pytest.raises(ValueError, match="no browser session"):
            bs.check_session(Provider.reporting_ninja)

    def test_reporting_ninja_rejected_for_login(self):
        with pytest.raises(ValueError, match="no browser session"):
            bs.start_login(Provider.reporting_ninja)

    def test_browser_providers_are_exactly_ahrefs_semrush_gsc(self):
        assert bs.BROWSER_PROVIDERS == {Provider.ahrefs, Provider.semrush, Provider.gsc}


class TestCheckSession:
    def test_no_profile(self, monkeypatch):
        """On the machine that OWNS the profile, a missing one really is first-time setup.

        The env var is cleared explicitly: this suite also runs INSIDE the dashboard container,
        where it is set, and `no_profile` is exactly the answer that must not come back there.
        """
        monkeypatch.delenv("SEOTOOLS_REMOTE_CDP_HOST", raising=False)
        _patch_profile(monkeypatch, _FakeProfile("ahrefs", exists=False))
        assert bs.check_session(Provider.ahrefs) is bs.SessionState.no_profile

    def test_a_consumer_never_reports_no_profile(self, monkeypatch):
        """The live 2026-07-22 bug. This container deliberately cannot see the profile directory,
        so `exists` is False even against a fully logged-in host. Reporting `no_profile` from that
        made the dashboard offer first-time login for every provider while the live warm Chrome it
        was already attached to sat right behind it."""
        monkeypatch.setenv("SEOTOOLS_REMOTE_CDP_HOST", "127.0.0.1")
        _patch_profile(monkeypatch, _FakeProfile("ahrefs", exists=False))
        monkeypatch.setattr(
            "seo_audit.browser.warm.status",
            lambda provider: _FakeWarmStatus(running=True, authenticated=True),
        )
        assert bs.check_session(Provider.ahrefs) is bs.SessionState.alive

    def test_expired_when_not_running(self, monkeypatch):
        _patch_profile(monkeypatch, _FakeProfile("ahrefs"))
        monkeypatch.setattr(
            "seo_audit.browser.warm.status",
            lambda provider: _FakeWarmStatus(running=False),
        )
        assert bs.check_session(Provider.ahrefs) is bs.SessionState.expired

    def test_expired_when_status_probe_raises(self, monkeypatch):
        _patch_profile(monkeypatch, _FakeProfile("ahrefs"))

        def _boom(provider):
            raise RuntimeError("cdp attach wedged")

        monkeypatch.setattr("seo_audit.browser.warm.status", _boom)
        assert bs.check_session(Provider.ahrefs) is bs.SessionState.expired

    def test_alive_when_busy(self, monkeypatch):
        """Another run driving the warm browser is a form of alive, not a false expiry."""
        _patch_profile(monkeypatch, _FakeProfile("semrush"))
        monkeypatch.setattr(
            "seo_audit.browser.warm.status",
            lambda provider: _FakeWarmStatus(running=True, authenticated=None, busy=True),
        )
        assert bs.check_session(Provider.semrush) is bs.SessionState.alive

    def test_alive_when_authenticated(self, monkeypatch):
        _patch_profile(monkeypatch, _FakeProfile("gsc"))
        monkeypatch.setattr(
            "seo_audit.browser.warm.status",
            lambda provider: _FakeWarmStatus(running=True, authenticated=True),
        )
        assert bs.check_session(Provider.gsc) is bs.SessionState.alive

    def test_expired_when_running_but_not_authenticated(self, monkeypatch):
        _patch_profile(monkeypatch, _FakeProfile("gsc"))
        monkeypatch.setattr(
            "seo_audit.browser.warm.status",
            lambda provider: _FakeWarmStatus(running=True, authenticated=False),
        )
        assert bs.check_session(Provider.gsc) is bs.SessionState.expired


class TestStartLogin:
    def test_raises_named_fallback_when_no_warm_chrome(self, monkeypatch):
        _patch_profile(monkeypatch, _FakeProfile("ahrefs"))
        monkeypatch.setattr("seo_audit.browser.warm.warm_cdp_url", lambda provider: None)

        with pytest.raises(bs.NoWarmChrome) as exc_info:
            bs.start_login(Provider.ahrefs)

        assert "browser-up --provider ahrefs" in exc_info.value.remedy
        assert exc_info.value.provider is Provider.ahrefs

    def test_happy_path_opens_login_page_and_returns_pollable_id(self, monkeypatch):
        profile = _FakeProfile("semrush")
        _patch_profile(monkeypatch, profile)
        monkeypatch.setattr(
            "seo_audit.browser.warm.warm_cdp_url", lambda provider: "http://127.0.0.1:9333"
        )

        goto_calls: list[tuple[str, int]] = []

        class _FakePage:
            def goto(self, url, wait_until=None, timeout=None):
                goto_calls.append((url, timeout))

        class _FakeContext:
            pages = []

            def new_page(self):
                return _FakePage()

        class _FakeBrowser:
            contexts = [_FakeContext()]

        class _FakeChromium:
            def connect_over_cdp(self, url, timeout=None):
                assert url == "http://127.0.0.1:9333"
                return _FakeBrowser()

        class _FakePlaywright:
            chromium = _FakeChromium()

        @contextmanager
        def _fake_sync_playwright():
            yield _FakePlaywright()

        monkeypatch.setattr(
            "seo_audit.browser.stealth.playwright_api", lambda: _fake_sync_playwright
        )
        monkeypatch.setattr(
            "seo_audit.browser.warm.status",
            lambda provider: _FakeWarmStatus(running=True, authenticated=True),
        )

        login_session_id = bs.start_login(Provider.semrush)

        assert profile.busy_lock_calls == 1
        assert goto_calls == [("https://example.test/semrush/login", 120_000)]
        assert login_session_id in bs._active_logins
        assert bs._active_logins[login_session_id].provider is Provider.semrush

        # Pollable afterwards via login_status, which just delegates to check_session.
        assert bs.login_status(login_session_id) is bs.SessionState.alive

    def test_login_status_unknown_id_raises(self):
        with pytest.raises(KeyError):
            bs.login_status("does-not-exist")
