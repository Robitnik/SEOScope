"""Read-only зріз сусідніх таблиць, потрібний для причин (task-100 §1.7).

Нічого нового не збирається: усе, чим обґрунтовується «чому цей URL у
черзі», вже лежить у БД —

- `page_inventory_items` (task-57): `indexable`, `status`, `title`,
  `description` — і виключення, і мета для таблиці оператора;
- `gsc_dimension_metrics` (dimension=`page`): трафік по сторінках;
- `monitor_pages`/`monitor_changes` (task-35): коли сторінку востаннє
  міняли — це і є `stale_crawl`.

Ключова пастка трафіку ([[gsc-page-filter-fake-zeros]]): `impressions == 0`
означає ВІДСУТНІСТЬ рядка, а не нуль показів. Тому такі рядки сюди не
потрапляють узагалі — інакше «просіла» спрацьовувало б на кожній сторінці,
яку Google просто не показував у вибраному вікні.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import (
    DimensionKind,
    GscDimensionMetric,
    MonitorChange,
    PageInventoryItem,
)
from app.urlinspector.urlkey import inspector_key


@dataclass
class PageFacts:
    title: str | None = None
    description: str | None = None
    indexable: bool | None = None
    http_status: int | None = None
    clicks_recent: int | None = None
    impressions_recent: int | None = None
    clicks_previous: int | None = None
    impressions_previous: int | None = None
    last_changed_at: datetime | None = None


@dataclass
class ClientContext:
    """Усе, що потрібно `score` по одному клієнту, за кілька запитів."""

    pages: dict[str, PageFacts] = field(default_factory=dict)

    def get(self, url_key: str) -> PageFacts:
        return self.pages.get(url_key, PageFacts())


def _facts(context: ClientContext, key: str) -> PageFacts:
    return context.pages.setdefault(key, PageFacts())


def load_client_context(
    session: Session,
    *,
    client_id: str,
    today: date | None = None,
    window_days: int = 90,
) -> ClientContext:
    context = ClientContext()
    anchor = today or utcnow().date()
    recent_start = anchor - timedelta(days=window_days)
    previous_start = recent_start - timedelta(days=window_days)

    # --- інвентаризація сторінок -----------------------------------------
    rows = session.execute(
        select(
            PageInventoryItem.url,
            PageInventoryItem.title,
            PageInventoryItem.description,
            PageInventoryItem.indexable,
            PageInventoryItem.status,
        ).where(PageInventoryItem.client_id == client_id)
    ).all()
    for url, title, description, indexable, status in rows:
        facts = _facts(context, inspector_key(url))
        facts.title = title
        facts.description = description
        facts.indexable = indexable
        facts.http_status = status

    # --- трафік по сторінках ---------------------------------------------
    # `impressions > 0` — саме фільтр, а не `>= 0`: нульові рядки GSC
    # синтетичні (321 із 365 днів на живому вікні), і брати їх як дані
    # означало б малювати падіння там, де його не було.
    for start, end, is_recent in (
        (recent_start, anchor, True),
        (previous_start, recent_start, False),
    ):
        traffic = session.execute(
            select(
                GscDimensionMetric.dimension_value,
                func.sum(GscDimensionMetric.clicks),
                func.sum(GscDimensionMetric.impressions),
            )
            .where(
                GscDimensionMetric.client_id == client_id,
                GscDimensionMetric.dimension == DimensionKind.page,
                GscDimensionMetric.period_start >= start,
                GscDimensionMetric.period_end <= end,
                GscDimensionMetric.impressions > 0,
            )
            .group_by(GscDimensionMetric.dimension_value)
        ).all()
        for value, clicks, impressions in traffic:
            facts = _facts(context, inspector_key(value))
            if is_recent:
                facts.clicks_recent = int(clicks or 0)
                facts.impressions_recent = int(impressions or 0)
            else:
                facts.clicks_previous = int(clicks or 0)
                facts.impressions_previous = int(impressions or 0)

    # --- коли сторінку востаннє міняли ------------------------------------
    # `MonitorChange` не має FK на сторінку — `related_id` навмисно
    # нетипізований, а адреса живе в `subject` (task-35). Тому групуємо по
    # `subject` при `scope="page"`, а не джойнимось.
    #
    # Беруться ЛИШЕ значущі зміни (`severity != "info"`). Перший живий
    # прогін показав, чому: `page_body_changed`/`content_changed` мають
    # severity `info` і спрацьовують майже на кожній сторінці щогодини
    # (monitor ходить частіше, ніж Google краулить), через що 16 із 27
    # проіндексованих URL SWM отримали `stale_crawl`. З балом 60 цей шум
    # витіснив би з черги справжніх кандидатів. `warning`/`critical` — це
    # title/h1/description/canonical/status, тобто рівно те, через що
    # сторінку варто переіндексувати.
    changes = session.execute(
        select(MonitorChange.subject, func.max(MonitorChange.detected_at))
        .where(
            MonitorChange.client_id == client_id,
            MonitorChange.scope == "page",
            MonitorChange.severity != "info",
        )
        .group_by(MonitorChange.subject)
    ).all()
    for url, changed_at in changes:
        if changed_at is not None:
            _facts(context, inspector_key(url)).last_changed_at = changed_at

    return context
