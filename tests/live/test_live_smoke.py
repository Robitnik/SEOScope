"""Live smoke: гейт «не запускати в CI» + сам metered прогін (PLAN §15).

Запускається окремим процесом (`subprocess`), а не імпортом: `scripts/live_smoke.py`
на старті реєструє **справжні** адаптери в глобальному реєстрі, і затягувати
їх у процес pytest-а означало б, що будь-який наступний тест може випадково
смикнути браузер чи metered API.

Метеровані тести позначені `@pytest.mark.live` і за замовчуванням
пропускаються — вмикає їх лише `pytest --live` (див. `tests/conftest.py`).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "live_smoke.py"


def _run(args: list[str], **env_overrides: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **env_overrides}
    return subprocess.run(  # noqa: S603 - фіксована команда, аргументи з тесту
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def test_script_refuses_to_spend_units_without_explicit_consent(temp_db):
    """Без `--yes-spend-units` скрипт не має права нічого зібрати."""
    result = _run(
        ["--provider", "semrush", "--client", "acme.example.com", "--collect"],
        DATABASE_URL=f"sqlite+aiosqlite:///{temp_db.path}",
    )
    assert result.returncode == 2
    assert "yes-spend-units" in result.stderr


def test_script_is_not_collected_by_the_default_pytest_run():
    """`live`-тести мають бути вимкнені без `--live` (інакше CI спалить units)."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "tests/live", "-q", "--collect-only"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0
    assert "live" not in result.stderr


@pytest.mark.live
def test_control_client_preflight_passes_for_every_configured_provider():
    """Metered: перевіряє, що сесії/ключі живі перед release.

    Тільки `preflight()` — без `--collect`, тож units не витрачаються, але
    протухлий browser-профіль або відсутній ключ спливе одразу.
    """
    listing = _run(["--list"])
    assert listing.returncode == 0, listing.stderr
    rows = [line for line in listing.stdout.splitlines() if line.strip()]
    assert rows, "у БД немає жодного клієнта — нема на чому робити smoke"

    control_domain, _, providers = rows[0].split(None, 2)
    failures: dict[str, str] = {}
    for provider in (p.strip() for p in providers.split(",") if p.strip() != "—"):
        result = _run(["--provider", provider, "--client", control_domain, "--yes-spend-units"])
        if result.returncode != 0:
            failures[provider] = (result.stdout + result.stderr).strip().splitlines()[-1]

    assert not failures, f"preflight не пройшов для {control_domain}: {failures}"
