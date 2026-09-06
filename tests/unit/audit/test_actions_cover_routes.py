"""Запобіжник для `app/audit/actions.py` (task-72).

Мапа дій — явна навмисно (щоб `action` було читабельним для людини), а ціна
явності — файл, який можна забути доповнити. Цей тест ловить забудькуватість:
кожен мутуючий маршрут застосунку має власний запис у мапі.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from app.audit.actions import ROUTE_ACTIONS

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def test_every_mutating_route_has_an_explicit_action() -> None:
    from app.main import app

    missing: list[tuple[str, str]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted((route.methods or set()) & _MUTATING):
            if (method, route.path) not in ROUTE_ACTIONS:
                missing.append((method, route.path))

    assert missing == [], (
        f"маршрути без запису в ROUTE_ACTIONS (додай у app/audit/actions.py): {missing}"
    )


def test_action_names_are_short_and_lowercase() -> None:
    for spec in ROUTE_ACTIONS.values():
        assert spec.action == spec.action.lower()
        assert len(spec.action) <= 80
        assert " " not in spec.action
