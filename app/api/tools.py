"""Каталог внутрішніх інструментів для сторінки Tools (task-27).

Реєстр у коді, а не в БД: набір інструментів — це частина застосунку, і
додавання наступного має бути одним записом тут, а не переверсткою сторінки
чи міграцією. Статистика в блоці — **реальна** (скільки звітів, коли
останній), а не декоративна; якщо інструменту ще нічим хвалитися, поля
лишаються `None`, а не `0` (PLAN §12).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import SessionDep
from app.db.models import AltTextReport, ContentQaPackage, RedirectCheck, SchemaMarkupCheck
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
    _ToolEntry(
        key="redirects",
        name="Redirect Checker",
        summary=(
            "Paste a link and see the whole redirect chain hop by hop — status, "
            "destination and what changed on every step."
        ),
        tags=("redirects", "http", "migration", "audit", "3xx", "seo"),
        route="#/tools/redirects",
        icon="arrow-right-circle",
    ),
    _ToolEntry(
        key="char-counter",
        name="Character Counter",
        summary=(
            "Type or paste text and watch the character count update live — "
            "handy for meta descriptions, title tags and character-limited copy."
        ),
        tags=("text", "characters", "copywriting", "meta", "counter"),
        route="#/tools/char-counter",
        icon="text-size",
    ),
    _ToolEntry(
        key="strategy-check",
        name="Strategy Check",
        summary=(
            "Upload an SEO strategy workbook and get every meta-length, brand-voice, "
            "slop and cannibalization issue back at once."
        ),
        tags=("seo", "strategy", "meta", "brand-voice", "audit", "xlsx"),
        route="#/tools/strategy-check",
        icon="file-spreadsheet",
    ),
    _ToolEntry(
        key="framework-check",
        name="Framework Check",
        summary=(
            "Upload a page or blog framework brief and catch heading, FAQ, "
            "brand-voice and slop issues before it reaches the copywriter."
        ),
        tags=("seo", "framework", "structure", "brand-voice", "audit", "docx"),
        route="#/tools/framework-check",
        icon="file-type-docx",
    ),
    _ToolEntry(
        key="strategy-package",
        name="Strategy Package Check",
        summary=(
            "Point at a strategy on Google Drive and get QA on the strategy, every "
            "linked framework and its published copy, plus cross-checks against "
            "each other and the live site — all in one run."
        ),
        tags=(
            "seo", "strategy", "framework", "copy", "brand-voice", "audit",
            "google-drive", "cross-check",
        ),
        route="#/tools/strategy-package",
        icon="file-stack",
    ),
    _ToolEntry(
        key="schema-markup",
        name="Schema Markup Checker",
        summary=(
            "Paste a URL, see what schema.org structured data is already there "
            "against live Google rich-result rules, and get a ready-to-paste "
            "JSON-LD snippet for what's missing."
        ),
        tags=("schema.org", "json-ld", "structured-data", "rich-results", "seo", "audit"),
        route="#/tools/schema-markup",
        icon="braces",
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


def _redirects_stat(session: Session) -> tuple[str | None, str | None]:
    total = session.scalar(select(func.count()).select_from(RedirectCheck)) or 0
    if not total:
        # «Ще не користувалися» — це не `0 checks` (PLAN §12).
        return None, None
    return ("check" if total == 1 else "checks"), str(total)


def _schema_markup_stat(session: Session) -> tuple[str | None, str | None]:
    total = session.scalar(select(func.count()).select_from(SchemaMarkupCheck)) or 0
    if not total:
        return None, None
    return ("check" if total == 1 else "checks"), str(total)


def _strategy_package_stat(session: Session) -> tuple[str | None, str | None]:
    total = session.scalar(select(func.count()).select_from(ContentQaPackage)) or 0
    if not total:
        return None, None
    return ("run" if total == 1 else "runs"), str(total)


# Статистику дає сам інструмент: хаб не знає, що для нього «одиниця роботи».
# `char-counter` тут навмисно немає: він нічого не зберігає в БД, тож
# stat_label/stat_value лишаються `None` (картка чесно показує «Not used
# yet»), а не `0 uses` заради заповнення (PLAN §12).
_STATS: dict[str, Callable[[Session], tuple[str | None, str | None]]] = {
    "alt-text": _alt_text_stat,
    "redirects": _redirects_stat,
    "schema-markup": _schema_markup_stat,
    "strategy-package": _strategy_package_stat,
}


@router.get("/tools", response_model=ToolsCatalogDTO)
async def list_tools(session: SessionDep) -> ToolsCatalogDTO:
    def _read(sync_session: Session) -> ToolsCatalogDTO:
        cards = []
        for entry in _TOOLS:
            stat = _STATS.get(entry.key)
            stat_label, stat_value = stat(sync_session) if stat else (None, None)
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
