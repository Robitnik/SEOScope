"""Search Analytics API про один URL (task-88 §3.1).

Три речі, які цей модуль існує щоб зробити правильно, і кожна з них
виміряна живо, а не взята з документації:

**1. Синтетичні нулі (§0.3).** Запит із фільтром по `page` і дименсією
`date` віддає рядок на КОЖЕН день вікна — навіть на дні, коли сторінка не
мала жодного показу, — і виглядає він як дані: `{"clicks": 0,
"impressions": 0, "ctr": 0, "position": 0}`. На 365-денному вікні реального
URL таких рядків **321 із 365**. Узяті як є, вони дають середню позицію
**1.17** замість справжніх **9.68**, тобто малюють сторінку першою в Google
цілком правдоподібним графіком. `position: 0` фізично неможлива, і
`impressions == 0` у GSC не існує як справжнє значення — тому такий рядок
тут стає відсутністю (`None`), а не нулем (PLAN §12). Справжній нуль
`clicks == 0` при `impressions > 0` нулем і лишається.

**2. Обидва написання шляху одним запитом (§0.4).** `equals` строго
рядковий: та сама сторінка без кінцевого слеша віддає синтетичний нуль,
тобто «даних немає». Групи `dimensionFilterGroups` між собою AND-яться, тож
«або зі слешем, або без» дає лише `includingRegex … /?$`. Регекс будується
через `re.escape` — без екранування точка в домені зробила б
`hearwellXservices.com` валідним матчем.

**3. Межа історії мовчазна (§0.7).** За 16 місяців API віддає `200 OK` з
нулем рядків. Обрізанням вікна займається `window.py`; тут лишається лише
не вигадувати рядків там, де їх немає.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urlsplit

from app.integrations.google_api.auth import GoogleApiSession

_GSC_API_BASE = "https://www.googleapis.com/webmasters/v3"
_ROW_LIMIT = 25000

# Типи пошуку, які вміє Search Analytics. `discover`/`googleNews` не
# приймають дименсію `query`, тому для них беремо лише тотали (§4, п. 14).
SEARCH_TYPES: tuple[str, ...] = ("web", "image", "video", "news", "discover", "googleNews")
_QUERYLESS_TYPES = frozenset({"discover", "googleNews"})


@dataclass(frozen=True)
class GscMetrics:
    """Один набір метрик GSC. `None` означає «Google не має даних», і це
    ніколи не те саме, що `0` — див. докстрінг модуля."""

    clicks: Decimal | None
    impressions: Decimal | None
    ctr: Decimal | None  # частка 0..1, як віддає API — НЕ відсоток
    position: Decimal | None

    @property
    def is_empty(self) -> bool:
        return self.impressions is None


EMPTY_METRICS = GscMetrics(clicks=None, impressions=None, ctr=None, position=None)


@dataclass(frozen=True)
class GscDayRow:
    day: date
    metrics: GscMetrics
    # День після `firstIncompleteDate` — Google його вже показує, але добу ще
    # не закрив (§0.7). Значення справжнє, але порівнювати його з повними
    # днями не можна.
    provisional: bool


@dataclass(frozen=True)
class GscKeyedRow:
    """Рядок будь-якої не-date дименсії: `query`, `device`, `country`,
    `searchAppearance`. `keys` лишається списком, бо двовимірні зрізи
    (`query x device`) використовують той самий тип."""

    keys: tuple[str, ...]
    metrics: GscMetrics


@dataclass(frozen=True)
class GscSeries:
    rows: list[GscDayRow]
    first_incomplete_date: date | None


class GscUrlClient:
    """Усі запити Search Analytics, звужені до одного URL."""

    def __init__(self, session: GoogleApiSession, *, site_url: str) -> None:
        self._session = session
        self._site_url = site_url
        self._path = (
            f"{_GSC_API_BASE}/sites/{quote(site_url, safe='')}/searchAnalytics/query"
        )

    # ── публічні запити ──────────────────────────────────────────────────

    def daily_series(
        self, url: str, start: date, end: date, *, search_type: str = "web"
    ) -> GscSeries:
        """Денна серія для URL. `dataState="all"` додає ~2 свіжі дні понад
        `final` і повертає `metadata.firstIncompleteDate` (§4, п. 10)."""
        payload = self._post(
            {
                **self._range(start, end),
                "dimensions": ["date"],
                "dimensionFilterGroups": self._page_filter(url),
                "type": search_type,
                "rowLimit": _ROW_LIMIT,
                "dataState": "all",
            }
        )
        incomplete = _parse_day(
            ((payload.get("metadata") or {}).get("firstIncompleteDate")) or ""
        )
        rows: list[GscDayRow] = []
        for raw in payload.get("rows") or []:
            keys = raw.get("keys") or []
            day = _parse_day(str(keys[0]) if keys else "")
            if day is None:
                continue
            metrics = _metrics(raw)
            if metrics.is_empty:
                # Синтетичний рядок — не «нуль показів», а «цього дня сторінки
                # в результатах не було». Викидаємо: серія має бути рідкою,
                # а не рівною довжині вікна.
                continue
            rows.append(
                GscDayRow(
                    day=day,
                    metrics=metrics,
                    provisional=incomplete is not None and day >= incomplete,
                )
            )
        rows.sort(key=lambda r: r.day)
        return GscSeries(rows=rows, first_incomplete_date=incomplete)

    def totals(
        self, url: str, start: date, end: date, *, search_type: str = "web"
    ) -> GscMetrics:
        """Агрегат за вікно — окремим запитом без дименсій, а не сумою серії.

        Позицію не можна складати: GSC усереднює її по показах сам, і сума
        денних значень дала б інше число. Той самий принцип, що в
        `app/reports/google_source.py` для тоталів звіту.
        """
        payload = self._post(
            {
                **self._range(start, end),
                "dimensionFilterGroups": self._page_filter(url),
                "type": search_type,
                "rowLimit": 1,
            }
        )
        rows = payload.get("rows") or []
        if not rows:
            return EMPTY_METRICS
        return _metrics(rows[0])

    def keyed(
        self,
        url: str,
        start: date,
        end: date,
        *,
        dimensions: list[str],
        search_type: str = "web",
        row_limit: int = _ROW_LIMIT,
    ) -> list[GscKeyedRow]:
        """Будь-який зріз URL за не-date дименсіями, з пагінацією.

        Пагінація не косметична: одна сторінка блогу цього портфеля дала
        **692** рядки в дименсії `query` (§0.2), і ліміт сторінки тут той
        самий 25 000, що й для будь-якого іншого запиту.
        """
        out: list[GscKeyedRow] = []
        for raw in self._paged(
            {
                **self._range(start, end),
                "dimensions": dimensions,
                "dimensionFilterGroups": self._page_filter(url),
                "type": search_type,
            },
            row_limit=row_limit,
        ):
            keys = tuple(str(k) for k in (raw.get("keys") or []))
            if not keys:
                continue
            metrics = _metrics(raw)
            if metrics.is_empty:
                continue
            out.append(GscKeyedRow(keys=keys, metrics=metrics))
        return out

    def search_type_totals(
        self, url: str, start: date, end: date
    ) -> dict[str, GscMetrics]:
        """Тотали URL у кожному типі пошуку (§4, п. 14).

        Порожній тип лишається в результаті з `EMPTY_METRICS`, а не
        випадає: «сторінка не показується в Картинках» — це відповідь, а
        зникла позиція — ні (frontend/AGENTS.md §6).
        """
        return {
            search_type: self.totals(url, start, end, search_type=search_type)
            for search_type in SEARCH_TYPES
        }

    def queries(
        self, url: str, start: date, end: date, *, search_type: str = "web"
    ) -> list[GscKeyedRow]:
        """Реальні пошукові запити сторінки (§0.2) — те, чого Reporting Ninja
        не вміє в принципі, і головна причина цієї таски."""
        if search_type in _QUERYLESS_TYPES:
            return []
        return self.keyed(
            url, start, end, dimensions=["query"], search_type=search_type
        )

    def sitemaps(self) -> list[dict[str, Any]]:
        """Сайтмапи property (§4, п. 23).

        ⚠️ `contents[].indexed` у цьому API **завжди `0`** (виміряно живо на
        сайті з 248 поданими URL) — це не «нічого не проіндексовано», а
        поле, яке Google перестав наповнювати. Викликач не має права його
        показувати.
        """
        payload = self._session.get_json(
            f"{_GSC_API_BASE}/sites/{quote(self._site_url, safe='')}/sitemaps"
        )
        raw = payload.get("sitemap")
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    # ── внутрішнє ────────────────────────────────────────────────────────

    @staticmethod
    def _range(start: date, end: date) -> dict[str, str]:
        return {"startDate": start.isoformat(), "endDate": end.isoformat()}

    @staticmethod
    def _page_filter(url: str) -> list[dict[str, Any]]:
        return [
            {
                "filters": [
                    {
                        "dimension": "page",
                        "operator": "includingRegex",
                        "expression": page_regex(url),
                    }
                ]
            }
        ]

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._session.post_json(self._path, payload)

    def _paged(
        self, payload: dict[str, Any], *, row_limit: int
    ) -> Iterator[dict[str, Any]]:
        start_row = 0
        while True:
            page = self._post({**payload, "rowLimit": row_limit, "startRow": start_row})
            rows = page.get("rows") or []
            yield from rows
            if len(rows) < row_limit:
                return
            start_row += row_limit


def page_regex(url: str) -> str:
    """RE2 для GSC, що ловить рівно цю сторінку в обох написаннях слеша.

    Схема й хост беруться з `url` як є (GSC зберігає повний URL, і property
    вже звужує нас до одного хоста), решта екранується. Регістрозалежний —
    так само, як сам GSC.
    """
    parts = urlsplit(url.strip())
    without_slash = url.rstrip("/") if (parts.path or "/") != "/" else url
    return f"^{re.escape(without_slash)}/?$"


def _metrics(raw: dict[str, Any]) -> GscMetrics:
    impressions = _num(raw.get("impressions"))
    if impressions is None or impressions == 0:
        # Єдина точка, де застосовується правило §0.3. Тримати його тут, а не
        # у викличниках, — щоб жодна нова панель не могла його забути.
        return EMPTY_METRICS
    return GscMetrics(
        clicks=_num(raw.get("clicks")),
        impressions=impressions,
        ctr=_num(raw.get("ctr")),
        position=_num(raw.get("position")),
    )


def _num(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_day(text: str) -> date | None:
    text = text.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None
