"""stdio-транспорт (план §3.3, §10): жоден рядок логів не сміє потрапити
в stdout — це кадрування протоколу, а `app.core.logging.configure_logging`
свідомо пише саме туди (`app/mcp/stdio.py`'s docstring).

Окремий підпроцес, а не імпорт у тому самому процесі: інші тести цього
файлу вже могли завантажити `app.mcp.server` (і кожен модуль виконується
один раз на процес), тож повторний `import` тут нічого не показав би —
підпроцес ловить саме перший, повний ланцюжок імпорту.
"""

from __future__ import annotations

import subprocess
import sys


def test_importing_stdio_module_writes_nothing_to_stdout() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import app.mcp.stdio"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
