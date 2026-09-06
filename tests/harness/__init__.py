"""Спільний тест-harness (task-13).

Один імпорт для всіх наскрізних наборів (`tests/integration`, `tests/contract`)
і для власників модулів, які хочуть підняти «справжню» БД/API/worker замість
ручного склеювання. Юніт-тести модуля лишаються на локальних фікстурах у
`tests/unit/<module>/` — цей пакет їх не замінює й не імпортує.

Пакет свідомо не імпортує `app.*` на рівні модуля: `tests/conftest.py`
підключає його раніше, ніж окремий тест встигне переставити `DATABASE_URL`,
а глобальні engine-и `app.db.session` кешуються при першому доступі.
"""

from __future__ import annotations

from harness.db import (
    TempDatabase,
    TempSqliteDatabase,
    build_test_engine,
    test_database_url,
    use_temp_database,
    use_temp_sqlite_database,
)
from harness.fakes import FakeScenario, register_fakes, reset_registry
from harness.fixtures import (
    FIXTURES_DIR,
    build_aiseotools_source,
    load_fixture,
    write_client_report,
)
from harness.secrets import (
    KNOWN_SECRET_MATERIAL,
    scan_engine_for_secrets,
    scan_sqlite_for_secrets,
    scan_text,
)
from harness.worker import running_worker, wait_for

__all__ = [
    "FIXTURES_DIR",
    "FakeScenario",
    "KNOWN_SECRET_MATERIAL",
    "TempDatabase",
    "TempSqliteDatabase",
    "build_aiseotools_source",
    "build_test_engine",
    "load_fixture",
    "register_fakes",
    "reset_registry",
    "running_worker",
    "scan_engine_for_secrets",
    "scan_sqlite_for_secrets",
    "scan_text",
    "test_database_url",
    "use_temp_database",
    "use_temp_sqlite_database",
    "wait_for",
    "write_client_report",
]
