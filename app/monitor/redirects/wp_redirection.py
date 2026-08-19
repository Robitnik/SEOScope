"""Джерело редіректів: плагін Redirection через `redirection/v1` REST

(task-51 §3.2.2) — основний режим: 10 із 14 клієнтів портфелю мають цей
namespace у `/wp-json/` (виміряно 2026-08-19).

**Основний виклик** — один запит `/export/all/json`: віддає повний список
правил і груп одним HTTP-запитом. Сама відповідь — конверт
`{"data": "<JSON-рядок>", "total": N}` (`Redirection_Api_Export::route_export`,
вихідники плагіна 5.9.0), де `data` — це ще один шар JSON:
`{"redirects": [Red_Item::to_json(), ...], "groups": [Red_Group::to_export(), ...]}`.
Подвійне кодування навмисне з боку плагіна (той самий формат іде у файл
завантаження) — тому парсер розкриває конверт у два проходи, а не один.

**Фолбек**, якщо конверт не той, що очікували (нестандартна версія плагіна,
проксі спотворив тіло) або статус інший за 200: посторінковий
`GET /redirect?per_page=200&orderby=id&direction=desc` + окремий `GET /group`
для назв груп — обидва віддають той самий по суті `Red_Item::to_json()`, тож
парситься одним і тим самим `rule_from_item`.

`rule_from_item`/`parse_export_envelope` навмисно публічні (не `_`-префікс):
`wp_browser.py` (браузерний фолбек, task-51 follow-up) отримує той самий
конверт REST-експорту через cookie+nonce замість Basic Auth і переюзає цей
самий парсер — подвійне кодування конверта однакове незалежно від способу
автентифікації.

**Чому дата створення сюди не входить.** `Red_Item::to_json()['last_access']`
— вже відформатований людський рядок (`date_i18n`, напр. `"August 19, 2026"`
або `"-"` за відсутності хітів), не машинний час, і це останнє СПРАЦЮВАННЯ,
не створення. Плагін узагалі не зберігає дату створення правила (task-51
§1.3) — увесь сенс цього модуля в тому, щоб дату дала наша власна щогодинна
перевірка (diff.py + runner.py), а не сам плагін.

Мережа — stdlib `urllib.request` під тим самим SSRF-гардом/UA/тротлінгом, що
й решта інструментів (`app.tools.http_safety`), без нових залежностей.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.monitor.redirects.contract import RedirectRule, RedirectSourceResult
from app.monitor.redirects.credentials import WpCredentials, basic_auth_header
from app.tools.http_safety import (
    NO_CACHE_HEADERS,
    USER_AGENT,
    HostThrottle,
    UnsafeUrlError,
    assert_public_http_url,
    safe_host,
)

__all__ = [
    "RestResponse",
    "Fetcher",
    "SOURCE",
    "fetch_redirects",
    "parse_export_envelope",
    "rule_from_item",
]

REQUEST_TIMEOUT_SECONDS = 15.0
MAX_BODY_BYTES = 4 * 1024 * 1024
PAGE_SIZE = 200
# Аварійний клапан на бік пагінації, не продуктова межа — узгоджено з
# `monitor_redirects_max_rules` (план §3.7, дефолт 20 000 / PAGE_SIZE = 100.
MAX_PAGES = 100

SOURCE = "redirection"
_THROTTLE = HostThrottle()


@dataclass(frozen=True)
class RestResponse:
    http_status: int
    content_type: str
    body: bytes


Fetcher = Callable[[str, dict[str, str]], RestResponse]


def _fetch_once(url: str, headers: dict[str, str]) -> RestResponse:
    request = urllib.request.Request(  # noqa: S310 - схема перевірена гардом вище
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            **NO_CACHE_HEADERS,
            **headers,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
            raw = response.read(MAX_BODY_BYTES + 1)[:MAX_BODY_BYTES]
            return RestResponse(response.status, response.headers.get_content_type() or "", raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_BODY_BYTES + 1)[:MAX_BODY_BYTES]
        content_type = exc.headers.get_content_type() if exc.headers else ""
        return RestResponse(exc.code, content_type or "", raw)


def _default_fetcher(url: str, headers: dict[str, str]) -> RestResponse:
    assert_public_http_url(url)
    host = safe_host(url)
    with _THROTTLE.slot(host):
        return _fetch_once(url, headers)


def _auth_headers(creds: WpCredentials) -> dict[str, str]:
    return {"Authorization": basic_auth_header(creds)}


def rule_from_item(item: dict[str, Any], groups: dict[int, str]) -> RedirectRule | None:
    external_id = item.get("id")
    source_url = item.get("url")
    if not isinstance(external_id, int) or not isinstance(source_url, str) or not source_url:
        return None
    action_data = item.get("action_data")
    target_url = None
    if isinstance(action_data, dict):
        candidate = action_data.get("url")
        if isinstance(candidate, str) and candidate:
            target_url = candidate
    action_code = item.get("action_code")
    match_type = item.get("match_type")
    group_id = item.get("group_id")
    title = item.get("title")
    return RedirectRule(
        rule_key=str(external_id),
        source_url=source_url,
        target_url=target_url,
        http_code=action_code if isinstance(action_code, int) else None,
        match_type=match_type if isinstance(match_type, str) and match_type else "url",
        regex=bool(item.get("regex")),
        status="enabled" if item.get("enabled") else "disabled",
        group_name=groups.get(group_id) if isinstance(group_id, int) else None,
        title=title if isinstance(title, str) and title else None,
        external_id=external_id,
    )


def parse_export_envelope(body: bytes) -> tuple[list[RedirectRule], bytes] | None:
    """`{"data": "<json>", "total": N}` → правила + сирий `data` (для блоба

    в `MonitorResource`, task-51 §3.1). `None` означає «конверт не той, що
    очікували» — виклик має перейти на посторінковий фолбек, а не падати.
    """
    try:
        envelope = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), str):
        return None
    try:
        payload = json.loads(envelope["data"])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("redirects"), list):
        return None

    groups: dict[int, str] = {}
    for group in payload.get("groups") or []:
        if (
            isinstance(group, dict)
            and isinstance(group.get("id"), int)
            and isinstance(group.get("name"), str)
        ):
            groups[group["id"]] = group["name"]

    rules: list[RedirectRule] = []
    for item in payload["redirects"]:
        if not isinstance(item, dict):
            continue
        rule = rule_from_item(item, groups)
        if rule is not None:
            rules.append(rule)
    return rules, envelope["data"].encode("utf-8")


def _fetch_groups(base_url: str, creds: WpCredentials, fetch: Fetcher) -> dict[int, str]:
    url = f"{base_url}/wp-json/redirection/v1/group?per_page=200&orderby=id&direction=asc"
    response = fetch(url, _auth_headers(creds))
    if response.http_status != 200:
        return {}
    try:
        payload = json.loads(response.body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    items = payload.get("items") if isinstance(payload, dict) else None
    groups: dict[int, str] = {}
    if isinstance(items, list):
        for group in items:
            if (
                isinstance(group, dict)
                and isinstance(group.get("id"), int)
                and isinstance(group.get("name"), str)
            ):
                groups[group["id"]] = group["name"]
    return groups


def _fetch_paginated(
    base_url: str, creds: WpCredentials, fetch: Fetcher, *, max_rules: int
) -> list[RedirectRule] | str:
    """Фолбек: перегін сторінками `/redirect`. Повертає список правил або

    рядок-код помилки (`"auth_failed"` тощо) при першому ж збої сторінки —
    часткового списку в разі збою ми свідомо не повертаємо: краще чесний
    `redirects_unavailable`, ніж мовчазний недорахунок, який діф прочитав би
    як масове видалення правил.
    """
    groups = _fetch_groups(base_url, creds, fetch)
    rules: list[RedirectRule] = []
    for page in range(MAX_PAGES):
        url = (
            f"{base_url}/wp-json/redirection/v1/redirect"
            f"?per_page={PAGE_SIZE}&page={page}&orderby=id&direction=desc"
        )
        response = fetch(url, _auth_headers(creds))
        if response.http_status in (401, 403):
            return "auth_failed"
        if response.http_status == 404:
            return "plugin_missing"
        if response.http_status != 200:
            return "unavailable"
        try:
            payload = json.loads(response.body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return "unavailable"
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            break
        for item in items:
            if isinstance(item, dict):
                rule = rule_from_item(item, groups)
                if rule is not None:
                    rules.append(rule)
        if len(items) < PAGE_SIZE or len(rules) >= max_rules:
            break
    return rules[:max_rules]


def fetch_redirects(
    base_url: str,
    credentials: WpCredentials,
    *,
    fetcher: Fetcher | None = None,
    max_rules: int = 20_000,
) -> RedirectSourceResult:
    """Один знімок правил редіректу сайту через плагін Redirection.

    `base_url` — без кінцевого слеша (`https://client.com`). Лише HTTPS:
    Basic auth по звичайному HTTP надіслав би Application Password у
    відкритому вигляді — відмова тут не робить жодного мережевого запиту.
    """
    if not base_url.lower().startswith("https://"):
        return RedirectSourceResult(
            ok=False,
            source=SOURCE,
            unavailable_reason="unsafe_url",
            detail="Only HTTPS sites can be checked (Basic auth requires an encrypted connection).",
        )

    fetch = fetcher or _default_fetcher
    export_url = f"{base_url}/wp-json/redirection/v1/export/all/json"

    try:
        response = fetch(export_url, _auth_headers(credentials))
    except UnsafeUrlError as exc:
        return RedirectSourceResult(
            ok=False, source=SOURCE, unavailable_reason="unsafe_url", detail=str(exc)
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return RedirectSourceResult(
            ok=False,
            source=SOURCE,
            unavailable_reason="unavailable",
            detail=f"Could not reach the site: {exc}.",
        )

    if response.http_status in (401, 403):
        return RedirectSourceResult(
            ok=False,
            source=SOURCE,
            unavailable_reason="auth_failed",
            detail=(
                f"WordPress REST API returned HTTP {response.http_status} — "
                "check the Application Password."
            ),
        )
    if response.http_status == 404:
        return RedirectSourceResult(
            ok=False,
            source=SOURCE,
            unavailable_reason="plugin_missing",
            detail="The Redirection plugin's REST endpoint was not found.",
        )

    parsed = parse_export_envelope(response.body) if response.http_status == 200 else None
    if parsed is not None:
        rules, raw_snapshot = parsed
        return RedirectSourceResult(
            ok=True, source=SOURCE, rules=tuple(rules[:max_rules]), raw_snapshot=raw_snapshot
        )

    try:
        fallback = _fetch_paginated(base_url, credentials, fetch, max_rules=max_rules)
    except UnsafeUrlError as exc:
        return RedirectSourceResult(
            ok=False, source=SOURCE, unavailable_reason="unsafe_url", detail=str(exc)
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return RedirectSourceResult(
            ok=False,
            source=SOURCE,
            unavailable_reason="unavailable",
            detail=f"Could not reach the site: {exc}.",
        )
    if isinstance(fallback, str):
        return RedirectSourceResult(ok=False, source=SOURCE, unavailable_reason=fallback)
    return RedirectSourceResult(ok=True, source=SOURCE, rules=tuple(fallback))
