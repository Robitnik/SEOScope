"""Кореневий conftest (task-13): маркери, гейт live-тестів і спільні фікстури.

Фікстури тут навмисно названі інакше, ніж локальні у `tests/unit/*/conftest.py`
(`temp_db`, `api_client`, `seed_client` замість `engine`, `client`, `session`),
щоб не перевизначати їх мовчки: власник модуля лишається господарем своїх
юніт-фікстур, а наскрізні набори беруть цей harness явно.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# `tests/` кладеться в sys.path явно, а harness імпортується як top-level
# `harness`, а не `tests.harness`: AISeoTools має власний пакет `tests`, який
# у контейнері стоїть у site-packages і перекрив би наш (перевірено —
# `import tests` резолвиться у `/seo/AISeoTools/tests`).
TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

from harness import (  # noqa: E402 - лише після налаштування sys.path
    FakeScenario,
    TempDatabase,
    register_fakes,
    reset_registry,
    use_temp_database,
)

_MARKERS = {
    "integration": "наскрізний набір: справжні API+worker+тимчасова SQLite (PLAN §15)",
    "contract": "контракт/регресія AISeoTools: сигнатури й стара test suite (PLAN §15)",
    "e2e": "браузерний прогін фронту; потребує node+chromium, поза pytest",
    "live": "metered live smoke по справжньому провайдеру; НІКОЛИ у звичайній CI",
}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Запустити metered live smoke (витрачає API units і чіпає справжні сесії).",
    )


def pytest_configure(config: pytest.Config) -> None:
    for name, description in _MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {description}")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Live-тести пропускаються завжди, доки не переданий `--live`.

    Гейт саме тут, а не в CI-конфізі: інакше будь-який локальний `pytest`
    спалив би units і поламав browser-сесії (ризик із плану task-13).
    """
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="metered live smoke; додай --live, щоб запустити")
    for item in items:
        # Саме маркер, а не `"live" in item.keywords`: keywords містять ще й
        # імена батьківських вузлів, тож усе в теці `tests/live/` пропускалось
        # би незалежно від маркера.
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip_live)


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TempDatabase]:
    """Ізольована SQLite, на яку націлений увесь процес (API, worker, тест)."""
    with use_temp_database(tmp_path, monkeypatch) as database:
        yield database


@pytest.fixture
def migrated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TempDatabase]:
    """Те саме, але схема отримана через `alembic upgrade head`, як у проді."""
    with use_temp_database(tmp_path, monkeypatch, use_migrations=True) as database:
        yield database


@pytest.fixture
def fake_adapters(monkeypatch: pytest.MonkeyPatch) -> dict[Any, Any]:
    """Чистий реєстр + `FakeAdapter` на кожен із чотирьох провайдерів."""
    reset_registry(monkeypatch)
    return register_fakes()


@pytest.fixture
def api_client(temp_db: TempDatabase) -> Iterator[Any]:
    """`TestClient` поверх справжнього `app.main:app`, підключеного до `temp_db`."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def seed_client(temp_db: TempDatabase) -> Any:
    """Фабрика клієнта з інтеграціями під сценарії `FakeAdapter`.

    Повертає `client_id`; `scenarios` мапить провайдера на `FakeScenario`,
    а відсутній у мапі провайдер лишається без інтеграції — рівно та
    ситуація «новий клієнт не заведений у сервісі» (PLAN §9.2).
    """
    from app.db.models import Client, ClientIntegration

    def _seed(
        *,
        name: str = "Acme",
        slug: str = "acme",
        domain: str = "acme.example.com",
        timezone: str = "America/New_York",
        scenarios: dict[Any, FakeScenario] | None = None,
    ) -> str:
        with temp_db.session_scope() as session:
            client = Client(name=name, slug=slug, domain=domain, timezone=timezone)
            session.add(client)
            session.flush()
            for provider, scenario in (scenarios or {}).items():
                session.add(
                    ClientIntegration(
                        client_id=client.id,
                        provider=provider,
                        enabled=True,
                        config_json=scenario.as_config(),
                    )
                )
            session.flush()
            return client.id

    return _seed
