"""DTO для GA4 Events by Month (task-98).

Форма дзеркалить `schema_markup.py`: create-запит, summary для історії, повний
DTO для сторінки прогону з shareable-лінком (`#/tools/ga4-events/<id>`).

Одна річ, яку ці DTO навмисно НЕ віддають, — «сума колонки Users». `totalUsers`
не адитивні по івентах (заміряно: ×3.9 на SAPHNY, план §1.6), тому тотали
місяця приїжджають окремим полем `totals`, порахованим власним запитом до GA4,
а не тим, що фронт міг би скласти з рядків.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

Ga4MonthState = Literal["ok", "no_data", "failed"]
Ga4ReportState = Literal["ok", "partial", "failed"]
# Порізи таблиці — ті самі ключі, що `service.SEGMENT_KEYS`.
Ga4Segment = Literal["all", "direct", "organic"]


class Ga4EventsReportRequest(BaseModel):
    client_id: str
    # "YYYY-MM", 1..24 штук (`service.MAX_MONTHS`).
    months: list[str] = Field(min_length=1)


class Ga4EventsClientDTO(BaseModel):
    id: str
    name: str
    # `False` = немає GA4-інтеграції/property; тул для нього чесно каже
    # `not_configured`, а не показує порожню таблицю.
    configured: bool
    property_id: str | None = None


class Ga4EventsClientsDTO(BaseModel):
    clients: list[Ga4EventsClientDTO] = []


class Ga4EventsMonthsDTO(BaseModel):
    """Місяці, у яких у property реально є події (один запит `yearMonth`)."""

    client_id: str
    months: list[str] = []


class Ga4EventSegmentDTO(BaseModel):
    """Одна клітинка таблиці оператора: поріз одного івента.

    `event_share_pct`/`user_share_pct` рахуються від тоталів ЦЬОГО порізу
    (`Ga4EventTotalsDTO` з тим самим ключем), не від загальних — саме так
    сходяться живі числа GA4 (1.16%, а не 1.22%).
    """

    event_count: int | None = None
    users: int | None = None
    per_user: Decimal | None = None
    event_share_pct: Decimal | None = None
    user_share_pct: Decimal | None = None


class Ga4EventRowDTO(BaseModel):
    event_name: str
    is_key_event: bool = False
    key_events: int | None = None
    # Ключі: "all" | "direct" | "organic" (`service.SEGMENT_KEYS`). Порізу,
    # якого GA4 не повернув для цього івента, тут просто немає — це не нуль.
    segments: dict[str, Ga4EventSegmentDTO] = {}


class Ga4EventTotalsDTO(BaseModel):
    """Тотали порізу на рівні property — з власного запиту без `eventName`.
    `users` тут НЕ дорівнює сумі однойменної колонки в рядках і не повинен:
    один користувач тригерить багато івентів."""

    event_count: int | None = None
    users: int | None = None
    key_events: int | None = None


class Ga4EventMonthDTO(BaseModel):
    month: str
    label: str
    state: Ga4MonthState
    # Місяць ще не закінчився, або від його кінця минуло <48 год (GA4 доуточнює).
    is_partial: bool = False
    rows: list[Ga4EventRowDTO] = []
    # Ключі ті самі, що в `Ga4EventRowDTO.segments`.
    totals: dict[str, Ga4EventTotalsDTO] = {}
    error: str | None = None


class Ga4EventsReportSummaryDTO(BaseModel):
    id: str
    client_id: str | None = None
    client_name: str | None = None
    property_id: str
    months: list[str] = []
    state: Ga4ReportState
    created_at: datetime


class Ga4EventsReportDTO(Ga4EventsReportSummaryDTO):
    months_data: list[Ga4EventMonthDTO] = []


class Ga4EventsReportsPageDTO(BaseModel):
    total: int
    limit: int
    offset: int
    reports: list[Ga4EventsReportSummaryDTO] = []
