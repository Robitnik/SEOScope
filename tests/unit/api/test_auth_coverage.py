"""Головний запобіжник авторизації (task-71).

Тест не перевіряє поведінку конкретного ендпоінта — він перевіряє, що
ЖОДНОГО відкритого ендпоінта не лишилось. Параметризація по реальному
`app.routes` означає, що новий роутер від будь-якої майбутньої таски
з'явиться тут автоматично: якщо він доступний без сесії й не внесений у
`_PUBLIC` свідомо — тест червоний ще до того, як це побачить прод.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from harness.routes import method_path_pairs

pytestmark = pytest.mark.auth

# Свідомо публічні шляхи: рівно те, без чого не намалюється форма входу,
# плюс сам логін і health-чек. Розширювати цей список — рішення, яке має
# бути видно в рев'ю; саме тому він живе в тесті, а не виводиться з коду.
_PUBLIC = {
    "/api/v1/auth/login",
    "/api/v1/auth/session",
    "/healthz",
}


def _routes(app) -> list[tuple[str, str]]:
    # Обхід — у `harness.routes`: з FastAPI 0.141 `include_router()` кладе в
    # `app.routes` обгортку `_IncludedRouter`, а не самі `APIRoute`, тож
    # плоский прохід тихо дає нуль маршрутів.
    return method_path_pairs(app, extra_paths=frozenset({"/healthz"}))


def _concrete(path: str) -> str:
    """`/api/v1/clients/{client_id}` -> `/api/v1/clients/probe`.

    Значення параметра неважливе: анонімний запит має зупинитись на
    авторизації ЩЕ ДО того, як роутер спробує щось знайти в базі.
    """
    parts = []
    for segment in path.split("/"):
        parts.append("probe" if segment.startswith("{") else segment)
    return "/".join(parts)


def test_every_api_route_requires_a_session(auth_client: TestClient) -> None:
    from app.main import app

    routes = _routes(app)
    assert len(routes) > 100, "маршрути не зібрались — тест перестав щось перевіряти"

    open_routes: list[tuple[str, str, int]] = []
    for method, path in routes:
        if path in _PUBLIC:
            continue
        response = auth_client.request(method, _concrete(path), follow_redirects=False)
        # 401 — правильна відповідь; 403 теж прийнятна (CSRF/роль спрацювали
        # ще до хендлера). Будь-що інше означає, що анонім дійшов до логіки.
        if response.status_code not in {401, 403}:
            open_routes.append((method, path, response.status_code))

    assert open_routes == [], f"ендпоінти доступні без сесії: {open_routes}"


def test_public_paths_are_actually_reachable(auth_client: TestClient) -> None:
    """Зворотний бік: гейт не має закрити те, без чого неможливо залогінитись."""
    assert auth_client.get("/healthz").status_code == 200
    assert auth_client.get("/api/v1/auth/session").status_code == 200
    assert auth_client.get("/login.html").status_code == 200


def test_static_frontend_is_closed(auth_client: TestClient) -> None:
    for path in (
        "/",
        "/index.html",
        "/js/router.js",
        "/css/app.css",
        # Реплей архіву віддає СИРИЙ HTML сайтів клієнтів на тому ж origin —
        # закритий тим самим гейтом, що й API (task-71 §12).
        "/archive/view/snapshot/probe/20260101000000/http://example.com",
    ):
        response = auth_client.get(path, follow_redirects=False)
        assert response.status_code in {303, 401}, f"{path} відкрита анонімно"
