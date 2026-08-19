"""Blackbox-проба редіректів (task-51 §3.2.3) — фолбек для сайтів без

`redirection/v1` (Shopify, WP без плагіна: 4 із 14 клієнтів портфелю,
виміряно 2026-08-19): трасуємо відомі URL і читаємо лише перший хоп через
`app.tools.redirects.trace.fetch_no_redirect` — той самий SSRF-гард/UA/
тротлінг, що й у Redirect Checker (task-29). `runner.py` (пізніше, після
task-50) складає список кандидатів: наявні правила клієнта + ключові
сторінки + вибірка з sitemap (§3.2.3) — тут лише сам прохід.

Бачимо тільки редіректи на URL, якими самі ходимо — це не повний список
правил сайту, а найкраще наближення без доступу до адмінки. Чесно позначено
в `RedirectSourceResult.detail`, щоб UI показував «probe mode: known URLs
only», а не видавав це за вичерпний перелік.
"""

from __future__ import annotations

import urllib.error
from collections.abc import Callable, Iterable

from app.monitor.redirects.contract import RedirectRule, RedirectSourceResult
from app.tools.http_safety import HostThrottle, UnsafeUrlError, safe_host
from app.tools.redirects.trace import RawResponse, fetch_no_redirect

__all__ = ["Fetcher", "SOURCE", "probe_redirects"]

SOURCE = "probe"
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
REQUEST_TIMEOUT_SECONDS = 10.0

_THROTTLE = HostThrottle()

Fetcher = Callable[[str], RawResponse]


def _default_fetcher(url: str) -> RawResponse:
    host = safe_host(url)
    with _THROTTLE.slot(host):
        return fetch_no_redirect(url, timeout=REQUEST_TIMEOUT_SECONDS)


def _rule_from_response(url: str, response: RawResponse) -> RedirectRule | None:
    if response.status not in REDIRECT_STATUSES:
        return None
    locations = response.values("location")
    target = locations[0].strip() if locations and locations[0].strip() else None
    redirect_by = response.values("x-redirect-by")
    # `X-Redirect-By` (WP 5.1+) називає виконавця редіректу (task-51 §1.5) —
    # найближче до «хто», що можна отримати без доступу до адмінки.
    title = f"via: {redirect_by[0].strip()}" if redirect_by and redirect_by[0].strip() else None
    return RedirectRule(
        rule_key=url,
        source_url=url,
        target_url=target,
        http_code=response.status,
        match_type="url",
        regex=False,
        status="enabled",
        group_name=None,
        title=title,
        external_id=None,
    )


def probe_redirects(urls: Iterable[str], *, fetcher: Fetcher | None = None) -> RedirectSourceResult:
    """Перевірити кожен `url` окремим запитом без слідування за 3xx і

    зібрати з нього правило, якщо перша відповідь — редірект.

    Збій одного URL не відкидає результати решти — та сама ізоляція
    клітинки, що й у решті монітора (CLAUDE.md: клітинка `(client, provider)`
    не валить сусідні). Провал усіх URL — це вже `redirects_unavailable`, а
    не тихий «правил немає».
    """
    fetch = fetcher or _default_fetcher
    rules: list[RedirectRule] = []
    checked = 0
    failures = 0

    for url in urls:
        checked += 1
        try:
            response = fetch(url)
        except (UnsafeUrlError, urllib.error.URLError, TimeoutError, OSError):
            failures += 1
            continue
        rule = _rule_from_response(url, response)
        if rule is not None:
            rules.append(rule)

    if checked == 0:
        return RedirectSourceResult(
            ok=False,
            source=SOURCE,
            unavailable_reason="unavailable",
            detail="No URLs were available to probe.",
        )
    if failures == checked:
        return RedirectSourceResult(
            ok=False,
            source=SOURCE,
            unavailable_reason="unavailable",
            detail=f"All {checked} probed URLs failed to respond.",
        )
    return RedirectSourceResult(
        ok=True,
        source=SOURCE,
        rules=tuple(rules),
        detail=f"probe mode: checked {checked} known URLs, {failures} failed to respond.",
    )
