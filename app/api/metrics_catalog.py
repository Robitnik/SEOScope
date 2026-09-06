"""Метрики: єдина точка правди про семантику (task-74 §8).

Раніше каталог (`app.metrics.CATALOG`) читали лише зсередини застосунку —
`app/api/dashboard.py` для побудови карток. Цей роут — перший, хто робить
його доступним ЗОВНІ як просте `GET`: MCP-ресурс `catalog://metrics`
(`app/mcp/resources.py`) читає каталог через нього, а не імпортує
`app.metrics` напряму — той самий інваріант «MCP ходить лише в REST», що
тримає ролі (task-71) і журнал (task-72) робочими для викликів агента без
жодного нового рядка в тих модулях.

Read-only, без побічних ефектів, без параметрів — тому не потребує ні
сесії БД, ні залежностей окрім самого каталогу. Публічний і без
кешування (`ttlMs` — турбота MCP-шару, не REST-контракту).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import register_router
from app.metrics import CATALOG
from app.schemas.metrics import MetricSpec

router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])


@router.get("/catalog", response_model=list[MetricSpec])
async def get_metrics_catalog() -> list[MetricSpec]:
    return list(CATALOG.values())


register_router(router)
