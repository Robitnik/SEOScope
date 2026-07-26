"""Каталог внутрішніх інструментів для сторінки Tools (task-27).

Реєстр у коді, а не в БД: набір інструментів — це частина застосунку, і
додавання наступного має бути одним записом тут, а не переверсткою сторінки
чи міграцією. Статистика в блоці — **реальна** (скільки звітів, коли
останній), а не декоративна; якщо інструменту ще нічим хвалитися, поля
лишаються `None`, а не `0` (PLAN §12).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import SessionDep
from app.db.models import AltTextReport
from app.schemas.tools import ToolCardDTO, ToolsCatalogDTO

router = APIRouter(prefix="/api/v1", tags=["tools"])


@dataclass(frozen=True)
class _ToolEntry:
    key: str
    name: str
    summary: str
    tags: tuple[str, ...]
    route: str
    icon: str


# Порядок тут = порядок блоків на сторінці.
_TOOLS: tuple[_ToolEntry, ...] = (
    _ToolEntry(
        key="alt-text",
        name="Image Alt Text",
        summary=(
            "Turn an image audit sheet into copy-ready WordPress media searches, "
            "then track which images already got their new alt."
        ),
        tags=("wordpress", "images", "accessibility", "audit", "csv", "xlsx"),
        route="#/tools/alt-text",
        icon="photo",
    ),
)


def _alt_text_stat(session: Session) -> tuple[str | None, str | None]:
    total = session.scalar(select(func.count()).select_from(AltTextReport)) or 0
    if not total:
        return None, None
    latest: datetime | None = session.scalar(select(func.max(AltTextReport.created_at)))
    label = "report" if total == 1 else "reports"
    if latest is None:
        return label, str(total)
    return label, str(total)


@router.get("/tools", response_model=ToolsCatalogDTO)
async def list_tools(session: SessionDep) -> ToolsCatalogDTO:
    def _read(sync_session: Session) -> ToolsCatalogDTO:
        cards = []
        for entry in _TOOLS:
            stat_label, stat_value = (None, None)
            if entry.key == "alt-text":
                stat_label, stat_value = _alt_text_stat(sync_session)
            cards.append(
                ToolCardDTO(
                    key=entry.key,
                    name=entry.name,
                    summary=entry.summary,
                    tags=list(entry.tags),
                    route=entry.route,
                    icon=entry.icon,
                    stat_label=stat_label,
                    stat_value=stat_value,
                )
            )
        return ToolsCatalogDTO(tools=cards)

    return await session.run_sync(_read)


register_router(router)
