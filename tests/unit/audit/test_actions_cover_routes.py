"""Запобіжник для `app/audit/actions.py` (task-72).

Мапа дій — явна навмисно (щоб `action` було читабельним для людини), а ціна
явності — файл, який можна забути доповнити. Цей тест ловить забудькуватість:
кожен мутуючий маршрут застосунку має власний запис у мапі.
"""

from __future__ import annotations

from harness.routes import method_path_pairs

from app.audit.actions import ROUTE_ACTIONS

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def test_every_mutating_route_has_an_explicit_action() -> None:
    from app.main import app

    routes = method_path_pairs(app)
    # Без цього assert'у тест лишався зеленим, коли обхід маршрутів
    # ламався (FastAPI 0.141, `_IncludedRouter`): порожній перелік дає
    # порожній `missing`. Запобіжник, який нічого не перевіряє, гірший
    # за відсутній — його не видно в червоному.
    assert len(routes) > 100, "маршрути не зібрались — тест перестав щось перевіряти"

    missing = [
        (method, path)
        for method, path in routes
        if method in _MUTATING and (method, path) not in ROUTE_ACTIONS
    ]

    assert missing == [], (
        f"маршрути без запису в ROUTE_ACTIONS (додай у app/audit/actions.py): {missing}"
    )


def test_action_names_are_short_and_lowercase() -> None:
    for spec in ROUTE_ACTIONS.values():
        assert spec.action == spec.action.lower()
        assert len(spec.action) <= 80
        assert " " not in spec.action
