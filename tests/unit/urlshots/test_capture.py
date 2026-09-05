"""`app.urlshots.capture.capture_screenshots` — per-preset isolation,
full-page height capping, and the SSRF fast-fail (task-64 §2.1-2.2).
Playwright itself is faked; nothing here launches a real browser.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.tools.http_safety import UnsafeUrlError
from app.urlshots import capture as capture_mod
from app.urlshots.capture import PRESET_NAMES, capture_screenshots


class _FakeResponse:
    def __init__(self, status):
        self.status = status


class _FakePage:
    def __init__(self, *, url, height, status=200, raise_on_goto=None, screenshot_bytes=b"img"):
        self.url = url
        self._height = height
        self._status = status
        self._raise_on_goto = raise_on_goto
        self._screenshot_bytes = screenshot_bytes
        self.screenshot_calls: list[dict] = []

    def goto(self, url, wait_until=None, timeout=None):
        if self._raise_on_goto is not None:
            raise self._raise_on_goto
        return _FakeResponse(self._status)

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script):
        return self._height

    def screenshot(self, **kwargs):
        self.screenshot_calls.append(kwargs)
        return self._screenshot_bytes


class _FakeContext:
    def __init__(self, page):
        self._page = page
        self.closed = False

    def route(self, pattern, handler):
        pass

    def new_page(self):
        return self._page

    def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, browser):
        self._browser = browser

    def launch(self, headless=True):
        return self._browser


class _FakeBrowser:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakePlaywrightCtx:
    def __init__(self, browser):
        self.chromium = _FakeChromium(browser)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _patched(pages_by_preset: dict[str, _FakePage]):
    browser = _FakeBrowser()

    def _fake_new_stealth_context(_browser, **kwargs):
        width = kwargs["viewport"]["width"]
        page = next(p for name, p in pages_by_preset.items() if _width_for(name) == width)
        return _FakeContext(page)

    def _width_for(name):
        return {"desktop": 1440, "tablet": 834, "mobile": 390}[name]

    return (
        patch.object(capture_mod, "sync_playwright", return_value=_FakePlaywrightCtx(browser)),
        patch.object(capture_mod, "new_stealth_context", side_effect=_fake_new_stealth_context),
        browser,
    )


class TestAllPresetsSucceed:
    def test_three_presets_captured_with_expected_metadata(self):
        pages = {
            "desktop": _FakePage(url="https://acme.com/", height=2000),
            "tablet": _FakePage(url="https://acme.com/", height=2200),
            "mobile": _FakePage(url="https://acme.com/", height=3000),
        }
        p1, p2, _browser = _patched(pages)
        with p1, p2:
            results = capture_screenshots("https://acme.com/")

        assert [r.preset for r in results] == list(PRESET_NAMES)
        assert all(r.state == "ok" for r in results)
        desktop = results[0]
        assert desktop.width == 1440
        assert desktop.fold_height == 900
        assert desktop.height == 2000
        assert desktop.truncated is False
        assert desktop.http_status == 200
        assert desktop.final_url == "https://acme.com/"
        assert desktop.image == b"img"

    def test_full_page_screenshot_used_when_under_the_height_cap(self):
        pages = {name: _FakePage(url="https://acme.com/", height=1000) for name in PRESET_NAMES}
        p1, p2, _browser = _patched(pages)
        with p1, p2:
            capture_screenshots("https://acme.com/", presets=("desktop",))
        call = pages["desktop"].screenshot_calls[0]
        assert call.get("full_page") is True
        assert "clip" not in call


class TestHeightCap:
    def test_height_past_cap_is_clipped_and_flagged_truncated(self):
        pages = {name: _FakePage(url="https://acme.com/", height=20_000) for name in PRESET_NAMES}
        p1, p2, _browser = _patched(pages)
        with p1, p2:
            results = capture_screenshots("https://acme.com/", presets=("desktop",))
        assert results[0].truncated is True
        assert results[0].height == 12_000
        call = pages["desktop"].screenshot_calls[0]
        assert "clip" in call
        assert call["clip"]["height"] == 12_000


class TestPerPresetIsolation:
    def test_one_preset_failing_does_not_affect_the_others(self):
        pages = {
            "desktop": _FakePage(url="https://acme.com/", height=1000),
            "tablet": _FakePage(
                url="https://acme.com/", height=1000, raise_on_goto=RuntimeError("boom")
            ),
            "mobile": _FakePage(url="https://acme.com/", height=1000),
        }
        p1, p2, _browser = _patched(pages)
        with p1, p2:
            results = capture_screenshots("https://acme.com/")

        by_preset = {r.preset: r for r in results}
        assert by_preset["desktop"].state == "ok"
        assert by_preset["mobile"].state == "ok"
        assert by_preset["tablet"].state == "failed"
        assert "boom" in (by_preset["tablet"].error or "")

    def test_timeout_error_is_reported_as_timeout(self):
        class TimeoutError(Exception):  # noqa: N818 - mimics playwright's own class name
            pass

        pages = {
            "desktop": _FakePage(url="https://acme.com/", height=1000),
            "tablet": _FakePage(url="https://acme.com/", height=1000),
            "mobile": _FakePage(
                url="https://acme.com/", height=1000, raise_on_goto=TimeoutError("slow")
            ),
        }
        p1, p2, _browser = _patched(pages)
        with p1, p2:
            results = capture_screenshots("https://acme.com/")
        mobile = next(r for r in results if r.preset == "mobile")
        assert mobile.state == "failed"
        assert mobile.error == "timeout"


class TestPresetSelection:
    def test_presets_param_captures_only_the_requested_subset(self):
        pages = {name: _FakePage(url="https://acme.com/", height=1000) for name in PRESET_NAMES}
        p1, p2, _browser = _patched(pages)
        with p1, p2:
            results = capture_screenshots("https://acme.com/", presets=("mobile",))
        assert [r.preset for r in results] == ["mobile"]


class TestUnsafeUrlFastFail:
    def test_rejected_before_any_browser_launch(self):
        with patch.object(
            capture_mod, "sync_playwright", side_effect=AssertionError("must not be called")
        ):
            with pytest.raises(UnsafeUrlError):
                capture_screenshots("http://169.254.169.254/")
