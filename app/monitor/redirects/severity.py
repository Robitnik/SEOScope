"""Серйозність змін редіректів (task-51 §3.4) — градація за ризиком,

підтверджено з користувачем 2026-08-19: більшість нових правил на активному
WP-сайті — це сам плагін (опція «Monitor changes to posts», group
`Modified Posts`), не подія, вартого уваги оператора щогодини. Тому НЕ «будь-
який новий редірект = warning», а класифікація по тому, що саме сталося.

Тимчасово self-contained: `app/monitor/severity.py` — спільна зона, зайнята
task-50 просто зараз (uncommitted). Коли вона звільниться, ці записи
переїдуть у `STATIC_SEVERITY`, а функції тут лишаються прошарком
класифікації, який викликає `runner.py`.
"""

from __future__ import annotations

from urllib.parse import urlsplit

CRITICAL = "critical"
WARNING = "warning"
INFO = "info"

REDIRECT_CHANGE_SEVERITY: dict[str, str] = {
    "redirect_offsite_added": CRITICAL,
    "redirect_added_on_live_url": WARNING,
    "redirect_target_changed": WARNING,
    "redirect_removed": WARNING,
    "redirect_added": INFO,
    "redirect_status_changed": INFO,
    "redirects_bulk_added": INFO,
    "redirects_unavailable": WARNING,
}

DEFAULT_BULK_THRESHOLD = 25


def severity_of_redirect_change(change_type: str) -> str:
    return REDIRECT_CHANGE_SEVERITY[change_type]


def _host(url: str | None) -> str | None:
    if not url:
        return None
    return (urlsplit(url).hostname or "").lower() or None


def is_offsite_target(target_url: str | None, *, client_domain: str) -> bool:
    """Той самий простий хост-збіг, що вже діє для `canonical_offsite`

    (`app/monitor/diff.py`) — без запиту, без PSL, `www.` ігнорується з обох
    боків. Ціль без хоста (відносний шлях, `None`) — не офсайт: редірект
    усередині сайту не може бути ознакою компрометації сам по собі.
    """
    target_host = _host(target_url)
    if target_host is None:
        return False
    return target_host.removeprefix("www.") != client_domain.lower().removeprefix("www.")


def classify_added_rule(*, offsite: bool, on_live_url: bool) -> str:
    """`change_type` для одного нового правила (§3.4). Офсайт переважає:

    правило одночасно офсайт і на живому URL — це насамперед офсайт
    (потенційна компрометація), а не просто «накрило сторінку».
    """
    if offsite:
        return "redirect_offsite_added"
    if on_live_url:
        return "redirect_added_on_live_url"
    return "redirect_added"


def is_bulk(added_count: int, *, threshold: int = DEFAULT_BULK_THRESHOLD) -> bool:
    """Понад `threshold` нових правил за один прогін — це один рух на сайті

    (наприклад, масове перейменування розділу), а не `threshold` окремих
    подій: агрегат замість спаму в стрічці (той самий патерн, що
    `sitemap_broken_urls`).
    """
    return added_count > threshold
