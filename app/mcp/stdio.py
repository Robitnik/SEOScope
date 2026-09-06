"""stdio-транспорт (план §3.3, хвиля 1): `python -m app.mcp.stdio`.

Хост (Claude Code / Claude Desktop) запускає це підпроцесом і говорить
JSON-RPC рядками через stdin/stdout. Тому **жоден рядок логів чи `print()`
не сміє потрапити в stdout** — це зламало б кадрування протоколу.

Свідомо НЕ викликає `app.core.logging.configure_logging()`: той хендлер
пише саме в `sys.stdout` (правильно для api/worker, де це йде в docker
logs) — увімкнути його тут означало б перемішати рядки логів із
JSON-RPC-кадрами в тому самому потоці. `tests/unit/mcp/test_stdio.py`
тримає цей інваріант тестом (перехоплює `sys.stdout` під час імпорту).
"""

from __future__ import annotations

from app.mcp.server import mcp


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
