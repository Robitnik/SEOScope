"""Browser-login fallback (task-51 follow-up 2026-08-19): every piece that

does not itself need a live browser page is unit-tested directly
(`parse_localized_script`, `cookie_header`, `login`/`_is_login_error` against
a fake `Page`) — the launch/close lifecycle around them is exercised live,
same precedent as `app.reports.ninja_browser.download_periods`.
"""

from __future__ import annotations

from app.monitor.redirects.wp_browser import cookie_header, login, parse_localized_script


class FakeLocator:
    def __init__(self, count: int = 0) -> None:
        self._count = count

    def count(self) -> int:
        return self._count


class FakePage:
    def __init__(
        self,
        *,
        login_error_count: int = 0,
        final_url: str = "https://client.example/wp-admin/",
    ) -> None:
        self.login_error_count = login_error_count
        self.url = "https://client.example/wp-login.php"
        self._final_url = final_url
        self.filled: dict[str, str] = {}
        self.clicked: list[str] = []

    def goto(self, *_args: object, **_kwargs: object) -> None:
        pass

    def fill(self, selector: str, value: str) -> None:
        self.filled[selector] = value

    def click(self, selector: str) -> None:
        self.clicked.append(selector)
        self.url = self._final_url

    def wait_for_load_state(self, *_args: object, **_kwargs: object) -> None:
        pass

    def locator(self, selector: str) -> FakeLocator:
        if selector == "#login_error":
            return FakeLocator(self.login_error_count)
        return FakeLocator(0)


class TestLogin:
    def test_successful_login_fills_and_clicks_and_lands_on_wp_admin(self) -> None:
        page = FakePage(login_error_count=0, final_url="https://client.example/wp-admin/")
        result = login(page, "https://client.example", wp_login="admin", password="secret")
        assert result is True
        assert page.filled["#user_login"] == "admin"
        assert page.filled["#user_pass"] == "secret"
        assert page.clicked == ["#wp-submit"]

    def test_login_error_element_present_is_a_rejected_login(self) -> None:
        page = FakePage(login_error_count=1, final_url="https://client.example/wp-admin/")
        assert login(page, "https://client.example", wp_login="admin", password="wrong") is False

    def test_still_on_wp_login_after_submit_is_a_rejected_login(self) -> None:
        # No #login_error element, but the click never actually navigated
        # away from wp-login.php (e.g. a 2FA/challenge interstitial).
        page = FakePage(login_error_count=0, final_url="https://client.example/wp-login.php?reauth=1")
        assert login(page, "https://client.example", wp_login="admin", password="wrong") is False



class TestParseLocalizedScript:
    def test_extracts_root_and_nonce_from_real_shaped_output(self) -> None:
        text = (
            'var Redirectioni10n = {"api":{"WP_API_root":'
            '"https://client.example/wp-json/","WP_API_nonce":"909680b56c",'
            '"site_health":"..."},"pluginBaseUrl":"..."};'
        )
        result = parse_localized_script(text)
        assert result == ("https://client.example/wp-json/", "909680b56c")

    def test_missing_script_text_returns_none(self) -> None:
        assert parse_localized_script("") is None

    def test_unrelated_script_text_returns_none(self) -> None:
        assert parse_localized_script("var SomethingElse = {};") is None

    def test_root_without_nonce_returns_none(self) -> None:
        text = 'var X = {"WP_API_root": "https://client.example/wp-json/"};'
        assert parse_localized_script(text) is None


class TestCookieHeader:
    def test_joins_name_value_pairs_for_the_matching_host(self) -> None:
        cookies = [
            {"name": "wordpress_logged_in", "value": "abc123", "domain": "client.example"},
            {"name": "wordpress_sec", "value": "def456", "domain": "client.example"},
        ]
        header = cookie_header(cookies, "client.example")
        assert header == "wordpress_logged_in=abc123; wordpress_sec=def456"

    def test_cookies_for_a_different_domain_are_excluded(self) -> None:
        # A login to one client's site must never leak into a request
        # aimed at a different one.
        cookies = [
            {"name": "session", "value": "for-client-a", "domain": "client-a.example"},
            {"name": "session", "value": "for-client-b", "domain": "client-b.example"},
        ]
        header = cookie_header(cookies, "client-a.example")
        assert header == "session=for-client-a"

    def test_no_matching_cookies_is_an_empty_string_not_an_error(self) -> None:
        assert cookie_header([], "client.example") == ""
