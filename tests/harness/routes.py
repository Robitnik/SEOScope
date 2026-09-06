"""Плоский перелік маршрутів застосунку — спільна основа для guard-тестів.

Чому окремий модуль, а не `for route in app.routes` на місці: **у FastAPI
0.141 `include_router()` більше не розкладає маршрути в `app.routes`**. Він
кладе туди по одному об'єкту `fastapi.routing._IncludedRouter` на роутер, а
самі `APIRoute` лежать усередині, у його `.routes`. Наївний
`isinstance(route, APIRoute)` по `app.routes` тому дає **порожній список** —
і це найгірший різновид поломки для запобіжника:

- `tests/unit/api/test_auth_coverage.py` (task-71) впав голосно лише тому,
  що там є `assert len(routes) > 100`;
- `tests/unit/audit/test_actions_cover_routes.py` (task-72) такого assert'у
  не мав і **проходив зеленим, не перевіряючи жодного маршруту**.

Реальні маршрути дістаються з `_IncludedRouter.original_router.routes`:
`APIRouter(prefix=...)` застосовує префікс ще при додаванні маршруту, тож
`route.path` там уже повний (`/api/v1/dashboard`), а не відносний. Обхід
рекурсивний, бо вкладеність не гарантована однорівневою: роутер може
містити роутер. Функція не імпортує `app.*` — застосунок приходить
аргументом, як і решта `tests/harness` (див. docstring пакета).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi.routing import APIRoute
from starlette.routing import Route

_HIDDEN_METHODS = frozenset({"HEAD", "OPTIONS"})


def iter_routes(app: Any) -> Iterator[APIRoute | Route]:
    """Усі кінцеві маршрути застосунку, розгорнуті крізь вкладені роутери."""
    yield from _walk(app.routes)


def _walk(routes: Any) -> Iterator[APIRoute | Route]:
    for route in routes:
        if isinstance(route, APIRoute | Route):
            yield route
            continue
        # FastAPI >= 0.141: обгортка `_IncludedRouter` тримає справжній
        # роутер тут. `.routes` на ній немає — саме тому наївний обхід
        # мовчки давав нуль.
        included = getattr(route, "original_router", None)
        nested = getattr(included, "routes", None) if included is not None else None
        if nested is None:
            nested = getattr(route, "routes", None)
        if nested:
            yield from _walk(nested)


def iter_api_routes(
    app: Any, *, extra_paths: frozenset[str] = frozenset()
) -> Iterator[APIRoute | Route]:
    """Лише маршрути під `/api/`, плюс явно названі винятки (напр. `/healthz`)."""
    for route in iter_routes(app):
        path = getattr(route, "path", "")
        if path.startswith("/api/") or path in extra_paths:
            yield route


def method_path_pairs(
    app: Any, *, extra_paths: frozenset[str] = frozenset()
) -> list[tuple[str, str]]:
    """`[("GET", "/api/v1/clients"), ...]` — відсортовано, без HEAD/OPTIONS."""
    pairs: set[tuple[str, str]] = set()
    for route in iter_api_routes(app, extra_paths=extra_paths):
        for method in route.methods or set():
            if method in _HIDDEN_METHODS:
                continue
            pairs.add((method, route.path))
    return sorted(pairs)
