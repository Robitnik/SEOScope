"""Адаптер плагіна Redirection (task-51 §3.2.2) — мережа інжектується

фейковим fetcher-ом, нуль сокетів. Кожен кейс — реальна форма відповіді з
вихідників плагіна 5.9.0 (конверт `{"data": "<json>", "total": N}`,
`Red_Item::to_json()`), не синтетика навмання.
"""

from __future__ import annotations

import json
from typing import Any

from app.monitor.redirects.credentials import WpCredentials
from app.monitor.redirects.wp_redirection import RestResponse, fetch_redirects

CREDS = WpCredentials(user="monitor-bot", app_password="secret-app-password")
BASE = "https://client.example"
EXPORT_URL = f"{BASE}/wp-json/redirection/v1/export/all/json"


class _FakeFetcher:
    """URL(без query) -> RestResponse; записує кожен запит і заголовки."""

    def __init__(self, routes: dict[str, RestResponse]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> RestResponse:
        self.calls.append((url, headers))
        base = url.split("?", 1)[0]
        if base in self.routes:
            return self.routes[base]
        return RestResponse(404, "text/html", b"<h1>Not Found</h1>")


def _item(
    item_id: int,
    *,
    url: str = "/old-page/",
    target: str | None = "/new-page/",
    action_code: int = 301,
    match_type: str = "url",
    regex: bool = False,
    enabled: bool = True,
    group_id: int = 1,
    title: str | None = None,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "url": url,
        "match_url": None,
        "match_data": None,
        "action_code": action_code,
        "action_type": "url",
        "action_data": {"url": target} if target is not None else None,
        "match_type": match_type,
        "title": title,
        "hits": 3,
        "regex": regex,
        "group_id": group_id,
        "position": 0,
        "last_access": "August 19, 2026",
        "enabled": enabled,
    }


def _export_response(
    redirects: list[dict[str, Any]], groups: list[dict[str, Any]] | None = None
) -> RestResponse:
    inner: dict[str, Any] = {"redirects": redirects}
    if groups is not None:
        inner["groups"] = groups
    envelope = {"data": json.dumps(inner), "total": len(redirects)}
    return RestResponse(200, "application/json", json.dumps(envelope).encode())


def _list_response(items: list[dict[str, Any]], total: int | None = None) -> RestResponse:
    payload = {"items": items, "total": total if total is not None else len(items)}
    return RestResponse(200, "application/json", json.dumps(payload).encode())


class TestHttpsOnlyGuard:
    def test_plain_http_is_refused_without_any_network_call(self) -> None:
        fetcher = _FakeFetcher({})
        result = fetch_redirects("http://client.example", CREDS, fetcher=fetcher)
        assert result.ok is False
        assert result.unavailable_reason == "unsafe_url"
        assert fetcher.calls == []


class TestExportEnvelope:
    def test_single_rule_via_export_envelope(self) -> None:
        fetcher = _FakeFetcher({EXPORT_URL: _export_response([_item(1487)])})
        result = fetch_redirects(BASE, CREDS, fetcher=fetcher)
        assert result.ok is True
        assert result.source == "redirection"
        assert len(result.rules) == 1
        rule = result.rules[0]
        assert rule.rule_key == "1487"
        assert rule.external_id == 1487
        assert rule.source_url == "/old-page/"
        assert rule.target_url == "/new-page/"
        assert rule.http_code == 301
        assert rule.status == "enabled"
        assert result.raw_snapshot is not None

    def test_group_id_is_resolved_to_group_name(self) -> None:
        groups = [{"id": 2, "name": "Modified Posts", "module_id": 1, "status": "enabled"}]
        fetcher = _FakeFetcher(
            {EXPORT_URL: _export_response([_item(1, group_id=2)], groups=groups)}
        )
        result = fetch_redirects(BASE, CREDS, fetcher=fetcher)
        assert result.rules[0].group_name == "Modified Posts"

    def test_disabled_rule_maps_to_status_disabled(self) -> None:
        fetcher = _FakeFetcher({EXPORT_URL: _export_response([_item(1, enabled=False)])})
        result = fetch_redirects(BASE, CREDS, fetcher=fetcher)
        assert result.rules[0].status == "disabled"

    def test_export_without_groups_key_leaves_group_name_none(self) -> None:
        fetcher = _FakeFetcher({EXPORT_URL: _export_response([_item(1)])})
        result = fetch_redirects(BASE, CREDS, fetcher=fetcher)
        assert result.rules[0].group_name is None

    def test_only_one_request_is_made_for_a_clean_export(self) -> None:
        fetcher = _FakeFetcher({EXPORT_URL: _export_response([_item(1)])})
        fetch_redirects(BASE, CREDS, fetcher=fetcher)
        assert len(fetcher.calls) == 1

    def test_authorization_header_is_basic_and_never_the_plaintext_password(self) -> None:
        fetcher = _FakeFetcher({EXPORT_URL: _export_response([_item(1)])})
        fetch_redirects(BASE, CREDS, fetcher=fetcher)
        _, headers = fetcher.calls[0]
        assert headers["Authorization"].startswith("Basic ")
        assert "secret-app-password" not in headers["Authorization"]

    def test_action_data_without_url_key_leaves_target_none(self) -> None:
        item = _item(1, target=None)
        fetcher = _FakeFetcher({EXPORT_URL: _export_response([item])})
        result = fetch_redirects(BASE, CREDS, fetcher=fetcher)
        assert result.rules[0].target_url is None

    def test_max_rules_truncates(self) -> None:
        items = [_item(i) for i in range(1, 6)]
        fetcher = _FakeFetcher({EXPORT_URL: _export_response(items)})
        result = fetch_redirects(BASE, CREDS, fetcher=fetcher, max_rules=3)
        assert len(result.rules) == 3


class TestHttpErrorClassification:
    def test_401_on_export_is_auth_failed_after_fallback_also_fails(self) -> None:
        fetcher = _FakeFetcher(
            {
                EXPORT_URL: RestResponse(401, "application/json", b'{"code":"rest_forbidden"}'),
                f"{BASE}/wp-json/redirection/v1/group": RestResponse(
                    401, "application/json", b"{}"
                ),
                f"{BASE}/wp-json/redirection/v1/redirect": RestResponse(
                    401, "application/json", b'{"code":"rest_forbidden"}'
                ),
            }
        )
        result = fetch_redirects(BASE, CREDS, fetcher=fetcher)
        assert result.ok is False
        assert result.unavailable_reason == "auth_failed"
        assert result.rules == ()

    def test_401_directly_on_export_short_circuits_without_fallback(self) -> None:
        # 401/403 на самому export — це вже остаточна відповідь (немає сенсу
        # пробувати посторінковий шлях тим самим креденшелам того самого сайту).
        fetcher = _FakeFetcher(
            {EXPORT_URL: RestResponse(401, "application/json", b'{"code":"rest_forbidden"}')}
        )
        result = fetch_redirects(BASE, CREDS, fetcher=fetcher)
        assert result.unavailable_reason == "auth_failed"
        assert len(fetcher.calls) == 1

    def test_404_on_export_is_plugin_missing(self) -> None:
        fetcher = _FakeFetcher({EXPORT_URL: RestResponse(404, "text/html", b"<h1>Not Found</h1>")})
        result = fetch_redirects(BASE, CREDS, fetcher=fetcher)
        assert result.ok is False
        assert result.unavailable_reason == "plugin_missing"

    def test_500_on_export_falls_back_and_then_reports_unavailable(self) -> None:
        fetcher = _FakeFetcher(
            {
                EXPORT_URL: RestResponse(500, "text/html", b"Internal Server Error"),
                f"{BASE}/wp-json/redirection/v1/group": RestResponse(
                    200, "application/json", b'{"items":[]}'
                ),
                f"{BASE}/wp-json/redirection/v1/redirect": RestResponse(
                    500, "text/html", b"Internal Server Error"
                ),
            }
        )
        result = fetch_redirects(BASE, CREDS, fetcher=fetcher)
        assert result.ok is False
        assert result.unavailable_reason == "unavailable"


class TestFallbackToPaginatedList:
    def test_malformed_export_envelope_falls_back_to_paginated_list(self) -> None:
        fetcher = _FakeFetcher(
            {
                EXPORT_URL: RestResponse(200, "application/json", b'{"unexpected": "shape"}'),
                f"{BASE}/wp-json/redirection/v1/group": _list_response(
                    [
                        {
                            "id": 1,
                            "name": "Redirections",
                            "redirects": 1,
                            "module_id": 1,
                            "moduleName": "WordPress",
                            "enabled": True,
                        }
                    ]
                ),
                f"{BASE}/wp-json/redirection/v1/redirect": _list_response([_item(1, group_id=1)]),
            }
        )
        result = fetch_redirects(BASE, CREDS, fetcher=fetcher)
        assert result.ok is True
        assert len(result.rules) == 1
        assert result.rules[0].group_name == "Redirections"

    def test_paginated_fallback_stops_at_a_short_page(self) -> None:
        page0 = _list_response([_item(i) for i in range(1, 201)])  # full page (200)
        page1 = _list_response([_item(201)])  # short page -> stop here

        class _PaginatedFetcher(_FakeFetcher):
            def __call__(self, url: str, headers: dict[str, str]) -> RestResponse:
                self.calls.append((url, headers))
                if url.startswith(EXPORT_URL):
                    return RestResponse(200, "text/html", b"<html>not json</html>")
                if "/group" in url:
                    return RestResponse(200, "application/json", b'{"items":[]}')
                if "page=0" in url:
                    return page0
                if "page=1" in url:
                    return page1
                return RestResponse(200, "application/json", b'{"items":[]}')

        fetcher = _PaginatedFetcher({})
        result = fetch_redirects(BASE, CREDS, fetcher=fetcher)
        assert result.ok is True
        assert len(result.rules) == 201

    def test_groups_endpoint_failure_during_fallback_does_not_crash(self) -> None:
        fetcher = _FakeFetcher(
            {
                EXPORT_URL: RestResponse(200, "text/html", b"<html>not json</html>"),
                f"{BASE}/wp-json/redirection/v1/group": RestResponse(500, "text/html", b"boom"),
                f"{BASE}/wp-json/redirection/v1/redirect": _list_response([_item(1, group_id=1)]),
            }
        )
        result = fetch_redirects(BASE, CREDS, fetcher=fetcher)
        assert result.ok is True
        assert result.rules[0].group_name is None

    def test_auth_failure_mid_pagination_reports_auth_failed_not_partial_rules(self) -> None:
        class _MidPageFailFetcher(_FakeFetcher):
            def __call__(self, url: str, headers: dict[str, str]) -> RestResponse:
                self.calls.append((url, headers))
                if url.startswith(EXPORT_URL):
                    return RestResponse(200, "text/html", b"<html>not json</html>")
                if "/group" in url:
                    return RestResponse(200, "application/json", b'{"items":[]}')
                if "page=0" in url:
                    return _list_response([_item(i) for i in range(1, 201)])
                return RestResponse(401, "application/json", b'{"code":"rest_forbidden"}')

        fetcher = _MidPageFailFetcher({})
        result = fetch_redirects(BASE, CREDS, fetcher=fetcher)
        assert result.ok is False
        assert result.unavailable_reason == "auth_failed"
        assert result.rules == ()
