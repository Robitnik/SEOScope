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
    "auth": "тест сам керує авторизацією (task-71): гейт НЕ вимикається автоматично",
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


@pytest.fixture(autouse=True)
def _isolated_write_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """task-66: `app.db.writegate.DbWriteGate` is a process-wide singleton,
    lazily bound (on first use) to whatever `Settings` happens to be cached
    at that moment — which, absent a test-specific override, is the SAME
    `app_data/app.db` this container's own live api/worker processes use
    (tests run inside the same container/volume as production, per
    CLAUDE.md's Docker-only rule). Autouse, at the ROOT conftest, so EVERY
    test in the suite gets its own gate bound to a lock file under its own
    `tmp_path` — not just the directories whose own conftest already
    happens to override `DATABASE_URL` before touching the DB — and can
    never contend with production traffic for that OS-level flock.
    """
    from app.db import writegate as db_writegate

    gate = db_writegate.DbWriteGate(lock_path=tmp_path / "test.write.lock")
    monkeypatch.setattr(db_writegate, "_gate", gate)
    yield
    gate.stop()


@pytest.fixture(autouse=True)
def _isolated_ninja_names_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Reporting Ninja account snapshot (`app/reports/ninja_catalog.py`)
    is a file in `app_data/`, shared by the api and worker — i.e. THIS
    container's production state, for the same reason `_isolated_write_gate`
    explains: tests run inside it.

    Found while adding the module: a test that deletes
    `REPORTING_NINJA_EMAIL` to prove no browser is launched still got the
    live account's 32 real report names back, because the snapshot the
    running api had just written was sitting there and needed no
    credentials to read. Autouse, so no test can read the deployed snapshot
    or overwrite it with fixture names.
    """
    monkeypatch.setenv(
        "REPORTING_NINJA_NAMES_CACHE_PATH", str(tmp_path / "ninja_report_names.json")
    )


@pytest.fixture(autouse=True)
def _isolated_audit_sink(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """task-72: `AuditSink` (`app/audit/sink.py`) is the SAME class of
    process-wide singleton as `DbWriteGate` above, with the same failure
    mode: its background thread resolves `get_sync_sessionmaker()` at WRITE
    time, roughly `audit_flush_interval_s` after `submit()` — long enough
    for a test's own `monkeypatch.setenv("DATABASE_URL", ...)` to have
    already unwound. Not hypothetical: this is exactly how a test run wrote
    ~50 rows into this container's own PRODUCTION `audit_events` table
    (LIVE 2026-09-06, cleaned up by hand — see task-72's completion report).
    Autouse and unconditional, mirroring `_isolated_write_gate`: no test,
    present or future, gets to rely on remembering this itself.

    The `monkeypatch` parameter is otherwise unused here — its only job is
    to make this fixture a declared dependent of the test's `monkeypatch`
    instance, so pytest tears THIS down first and reverts `DATABASE_URL`
    only afterwards. Without it, ordering relative to some other fixture's
    own `monkeypatch` use is unspecified, and the race this fixture exists
    to close would simply move here instead.
    """
    yield
    from app.audit.sink import reset_audit_sink_for_tests

    reset_audit_sink_for_tests()


@pytest.fixture(autouse=True)
def _auth_disabled_unless_asked(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """task-71: увесь наявний набір тестів писався до появи авторизації.

    Вмикати гейт для нього означало б переписати сотні тестів під логін заради
    нуля додаткової впевненості — вони перевіряють поведінку ендпоінтів, а не
    доступ до них. Тому за замовчуванням авторизація в тестах вимкнена, а
    тести, які перевіряють САМЕ її, ставлять маркер `auth` і вмикають гейт
    самі. Покриття «жоден ендпоінт не лишився відкритим» дає окремий
    `tests/unit/api/test_auth_coverage.py`, а не кожен тест окремо.
    """
    from app.core.config import get_settings

    if request.node.get_closest_marker("auth"):
        yield
        return
    monkeypatch.setenv("AUTH_ENABLED", "false")
    # task-72: той самий привід, що auth. Без цього AuditMiddleware пише
    # через ГЛОБАЛЬНИЙ фоновий `AuditSink` на кожному з ~2500 наявних тестів
    # — а той потік лягає спати на `flush_interval_s` і будить лише ПІСЛЯ
    # того, як `monkeypatch` цього тесту вже відкотив `DATABASE_URL`. Це не
    # гіпотеза: саме так тестовий прогін дописав рядки в БОЙОВУ БД цього ж
    # контейнера (LIVE 2026-09-06, прибрано вручну — див. completion report).
    monkeypatch.setenv("AUDIT_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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
