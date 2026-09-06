"""Фейковий REST API для tools/prompts/resources (task-74).

`FakeApi` реєструє точні `(method, path) -> handler` — без сюрпризів,
без regex-магії: якщо tool звертається кудись, чого тест не зареєстрував,
`AssertionError` називає точну адресу, а не мовчки повертає 404.

Кожен tool ходить через `app.mcp.apiclient.get_api_client()` — цей
conftest підміняє синглтон на клієнт із `httpx2.MockTransport`, тож жоден
тест тут не відкриває справжнього мережевого з'єднання.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import httpx2
import pytest

from app.mcp.apiclient import ApiClient, reset_api_client_for_tests

RouteHandler = Callable[[httpx2.Request], tuple[int, Any]]


class FakeApi:
    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], RouteHandler] = {}
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def add(self, method: str, path: str, handler: RouteHandler) -> None:
        self._routes[(method.upper(), path)] = handler

    def json(self, method: str, path: str, body: Any, *, status: int = 200) -> None:
        self.add(method, path, lambda _request: (status, body))

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        method = request.method.upper()
        path = request.url.path
        params = dict(request.url.params)
        self.calls.append((method, path, params))
        handler = self._routes.get((method, path))
        if handler is None:
            registered = ", ".join(f"{m} {p}" for m, p in self._routes)
            raise AssertionError(
                f"FakeApi: no route registered for {method} {path}. "
                f"Registered: {registered or '(none)'}"
            )
        status, body = handler(request)
        return httpx2.Response(status, json=body)


@pytest.fixture
def fake_api() -> Iterator[FakeApi]:
    api = FakeApi()
    client = ApiClient(
        base_url="http://test",
        token="test-token",
        transport=httpx2.MockTransport(api.handle),
    )
    reset_api_client_for_tests(client)
    try:
        yield api
    finally:
        reset_api_client_for_tests(None)
