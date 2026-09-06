"""Сканер секретів для gate «у БД/подіях/логах немає секретів» (PLAN §13, §18).

Працює двома шляхами:

- **canary** — тест кладе завідомо унікальні значення в оточення/конфіг і
  перевіряє, що вони не з'явились у файлі БД. Це надійніше за евристику:
  збіг означає реальний витік, а не схожий на ключ рядок.
- **евристика** — регулярки на впізнавані форми (Bearer, `api_key=`,
  `Set-Cookie`), щоб зловити витік, якого canary не передбачив.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - типи лише для mypy
    from sqlalchemy import Engine

# Значення, які тести підсовують застосунку як «секрети». Кожне унікальне і
# не трапляється у легітимних даних, тому будь-який збіг — це витік.
KNOWN_SECRET_MATERIAL = (
    "yovth-canary-api-key-8f3a1c",
    "yovth-canary-password-2b7d4e",
    "yovth-canary-session-cookie-5c9e0a",
)

_HEURISTICS = (
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]{12,}", re.IGNORECASE),
    re.compile(r"(?i)\b(api[_-]?key|password|secret|access[_-]?token)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bset-cookie\b\s*:"),
)


def scan_text(text: str, *, extra: tuple[str, ...] = ()) -> list[str]:
    """Повернути список знайдених у тексті секретів (порожній = чисто)."""
    found = [needle for needle in (*KNOWN_SECRET_MATERIAL, *extra) if needle in text]
    found.extend(match.group(0) for pattern in _HEURISTICS for match in pattern.finditer(text))
    return found


def scan_sqlite_for_secrets(db_path: Path, *, extra: tuple[str, ...] = ()) -> dict[str, list[str]]:
    """Пройти всі текстові значення всіх таблиць і повернути `{table.column: [знахідки]}`.

    Читає через SQLite, а не сирі байти файлу: так знахідка одразу вказує на
    конкретну колонку, і сканер не спотикається об залишки WAL/vacuum.
    """
    findings: dict[str, list[str]] = {}
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            for row in connection.execute(f'SELECT * FROM "{table}"'):  # noqa: S608 - імена з схеми
                for column in row.keys():
                    value = row[column]
                    if not isinstance(value, str | bytes):
                        continue
                    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
                    hits = scan_text(text, extra=extra)
                    if hits:
                        findings.setdefault(f"{table}.{column}", []).extend(hits)
    finally:
        connection.close()
    return findings


def scan_engine_for_secrets(
    engine: Engine, *, extra: tuple[str, ...] = ()
) -> dict[str, list[str]]:
    """`scan_sqlite_for_secrets`, but against any bound SQLAlchemy `Engine`
    (task-75 §9) — same `SELECT *` per table, same text/heuristic scan,
    dialect-agnostic so this works against the shared test harness whether
    it is pointed at SQLite or Postgres.
    """
    from sqlalchemy import inspect as sa_inspect

    findings: dict[str, list[str]] = {}
    inspector = sa_inspect(engine)
    with engine.connect() as connection:
        for table in inspector.get_table_names():
            if table == "alembic_version":
                continue
            result = connection.exec_driver_sql(f'SELECT * FROM "{table}"')  # noqa: S608
            columns = result.keys()
            for row in result:
                for column, value in zip(columns, row, strict=True):
                    if not isinstance(value, str | bytes):
                        continue
                    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
                    hits = scan_text(text, extra=extra)
                    if hits:
                        findings.setdefault(f"{table}.{column}", []).extend(hits)
    return findings
