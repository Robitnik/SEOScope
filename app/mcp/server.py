"""Побудова `MCPServer` (task-74, хвиля 1): офіційний SDK `mcp` ^2.1.1.

`mcp` — модуль-рівневий singleton, а не фабрика, свідомо: кожен файл у
`app/mcp/tools/**` реєструє свої функції декоратором `@mcp.tool()` на
рівні модуля (той самий патерн, що показаний у квікстарті SDK), тож
об'єкт має існувати ДО того, як ці модулі імпортуються.

Порядок нижче важливий і навмисний: `mcp = MCPServer(...)` створюється
ПЕРШИМ, а модулі з `@mcp.tool()`/`@mcp.resource()`/`@mcp.prompt()`
імпортуються ПІСЛЯ — лише заради побічного ефекту реєстрації (звідси
`# noqa: F401` — самі імена нам не потрібні). Це працює, бо на момент,
коли `app.mcp.tools.orientation` виконує `from app.mcp.server import mcp`,
Python уже поклав частково виконаний модуль `app.mcp.server` у
`sys.modules` з готовим атрибутом `mcp` — переставити ці два кроки
місцями означало б імпортувати порожній `mcp` до того, як хоч один tool
встиг зареєструватися.
"""

from __future__ import annotations

from mcp.server import MCPServer

from app.core.config import get_settings

_settings = get_settings()

mcp = MCPServer(
    name="yovth-clients-report",
    title=_settings.app_name,
    version="0.1.0",
    instructions=(
        "SEO/marketing dashboard for an agency's client portfolio: metrics, "
        "web archive history, site-change monitoring, and reports. Client "
        "tools take a client NAME or DOMAIN, not an internal id — call "
        "list_clients first if you don't know the exact spelling. Long-"
        "running actions (run_client_report, capture_url_now, "
        "run_monitor_check) return a job id; poll it with get_job_status. "
        "Read the catalog://metrics resource before calling any change in "
        "a report table an improvement or a regression — a metric's better "
        "direction is not always 'higher is better'."
    ),
)

# Побічний ефект реєстрації. `tools/list` (план §3.1: детермінований
# порядок — інакше ламається prompt cache на боці моделі) віддає tools у
# порядку реєстрації (`ToolManager._tools` — звичайний dict, порядок
# вставки стабільний у Python 3.7+); порядок імпортів тут ФІКСУЄ цей
# порядок так само надійно, як явний список назв.
from app.mcp import prompts, resources  # noqa: E402,F401
from app.mcp.tools import archive, jobs, monitor, orientation, reports  # noqa: E402,F401
