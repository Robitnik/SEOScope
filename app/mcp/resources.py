"""Ресурс `catalog://metrics` (план §8): семантика зміни визначається
`better_direction`, а не знаком числа — проєктний інваріант, який без
цього ресурсу модель порушить першим-ліпшим падінням bounce rate.

Читається через `GET /api/v1/metrics/catalog` (новий, task-74 §8), а не
через прямий імпорт `app.metrics.CATALOG` — той самий інваріант §2, що й
для tools: MCP не бачить доменного шару, лише REST.
"""

from __future__ import annotations

import json

from app.mcp.apiclient import get_api_client
from app.mcp.server import mcp


@mcp.resource(
    "catalog://metrics",
    name="metrics_catalog",
    title="Metric catalog",
    mime_type="application/json",
    description=(
        "Every metric this system tracks: its key, label, unit, and — critically — "
        "`better_direction` ('up', 'down', or 'neutral'). Read this before describing "
        "any raw percentage change (e.g. in get_report_data's rows) as an improvement "
        "or a regression: a metric whose better_direction is 'down' (like bounce rate "
        "or average position) got BETTER when its value went down, not worse."
    ),
)
async def metrics_catalog() -> str:
    api = get_api_client()
    catalog = await api.get("/api/v1/metrics/catalog")
    return json.dumps(catalog, indent=2, sort_keys=True)
