"""DTO для Indexing Planner (task-100).

Форма дзеркалить `page_inventory.py`/`ga4_events.py`, але з однією
принциповою відмінністю: тул нічого не «запускає й повертає звіт» — він
тримає ЖИВИЙ стан, який щоночі перебудовується. Тому тут немає
`ReportDTO` зі знімком; є зрізи одного й того ж реєстру під різні екрани.

Три речі, які ці DTO навмисно НЕ роблять:

1. **Не підмінюють `None` нулем.** `indexed`/`candidates` у зведенні —
   `int | None`: клієнт, якого жодного разу не сканували, має показати
   «не знаємо», а не «0 у індексі» (PLAN §12, план §2.2).
2. **Не віддають готове речення причини.** `reason_code` + `reason` (докази
   як дані) — текст будує фронт. Інакше причину неможливо перерахувати
   заднім числом, а вона й так уже заморожена в рядку відправки.
3. **Не будують deep-link у GSC.** `gsc_link` приходить із того, що віддав
   `urlInspection`: його `id` — непрозорий хеш, а не адреса (план §2.5).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

# --- словники станів (той самий текст, що в БД і в UI) --------------------

SubmissionStatus = Literal["planned", "sent", "quota_blocked", "skipped", "cancelled"]
ScanStatus = Literal["running", "ok", "partial", "failed"]
ScanTrigger = Literal["scheduled", "manual"]
ScanMode = Literal["rotating", "full"]
ExcludedReason = Literal[
    "noindex",
    "robots_blocked",
    "non_canonical",
    # Окремо від `http_error`: редирект — не поламана сторінка, а інша
    # адреса. Злити їх в одне означало б послати оператора чинити те,
    # що не зламане (14 URL живо, план §1.6).
    "redirect",
    "http_error",
    "manual",
]
ReasonCode = Literal[
    "dropped_out",
    "never_indexed",
    "discovered_not_indexed",
    "crawled_not_indexed",
    "new_page",
    "stale_crawl",
    "traffic_lost",
    "no_traffic",
    "periodic_refresh",
]
# Що саме оператор має зробити з рядком. `review_then_submit` існує окремо
# від `submit` не для краси: `Crawled - currently not indexed` означає, що
# Google сторінку вже бачив і не взяв (78 URL, третина беклогу — план
# §1.5 п.3), і повторна відправка без правок контенту нічого не дасть.
UrlAction = Literal["submit", "review_then_submit"]


# --- налаштування ---------------------------------------------------------


class IndexingSettingsDTO(BaseModel):
    enabled: bool
    daily_quota: int
    resubmit_after_days: int
    scan_mode: ScanMode


class IndexingSettingsUpdate(BaseModel):
    enabled: bool | None = None
    daily_quota: int | None = Field(default=None, ge=1, le=50)
    resubmit_after_days: int | None = Field(default=None, ge=1, le=365)
    scan_mode: ScanMode | None = None


# --- клієнти й зведення ---------------------------------------------------


class IndexingClientDTO(BaseModel):
    id: str
    name: str
    # `False` = немає GSC-інтеграції. Такий клієнт (GOREPORT) чесно каже
    # `not_configured`, а не показує порожню чергу.
    configured: bool
    site_url: str | None = None
    settings: IndexingSettingsDTO

    # Усе нижче — `None`, доки клієнта жодного разу не сканували.
    urls_total: int | None = None
    indexed: int | None = None
    candidates: int | None = None
    excluded: int | None = None
    never_inspected: int | None = None
    coverage_ratio: Decimal | None = None
    last_scan_at: datetime | None = None
    last_scan_status: ScanStatus | None = None


class IndexingClientsDTO(BaseModel):
    clients: list[IndexingClientDTO] = []


class IndexingOverviewDTO(BaseModel):
    """Портфельне зведення для шапки сторінки."""

    clients_enabled: int
    clients_configured: int
    urls_total: int | None = None
    indexed: int | None = None
    candidates: int | None = None
    excluded: int | None = None
    never_inspected: int | None = None
    coverage_ratio: Decimal | None = None
    # Скільки слотів сумарно дають увімкнені клієнти на день.
    daily_capacity: int
    planned_today: int
    sent_today: int
    last_scan_at: datetime | None = None
    last_scan_status: ScanStatus | None = None
    # «Черга пройдеться за N робочих днів». `None`, коли черга порожня —
    # це не «0 днів», це «нема чого проходити».
    days_to_clear_queue: int | None = None
    cycle_holds: bool | None = None


# --- URL ------------------------------------------------------------------


class IndexingUrlDTO(BaseModel):
    id: str
    client_id: str
    client_name: str
    url: str

    # Мета зі своєї ж інвентаризації сторінок (task-57). `None` = сторінки
    # там немає; `""` = тег порожній — різні факти, і UI показує їх різно.
    title: str | None = None
    description: str | None = None

    # `None` = ще жодного разу не інспектували. Не `False`.
    is_indexed: bool | None = None
    coverage_state: str | None = None
    last_crawl_time: datetime | None = None
    inspected_at: datetime | None = None
    gsc_link: str | None = None

    excluded: bool = False
    excluded_reason: ExcludedReason | None = None

    reason_code: ReasonCode | None = None
    # Докази як дані: {"clicks_prev": 41, "clicks_now": 3, ...}
    reason: dict[str, Any] = {}
    action: UrlAction | None = None
    priority_score: Decimal | None = None

    last_submitted_at: datetime | None = None
    submissions_count: int = 0
    # Скільки днів лишилось до кінця cooldown; `None` = не в cooldown.
    cooldown_days_left: int | None = None
    in_sitemap: bool = True


class IndexingQueueDTO(BaseModel):
    urls: list[IndexingUrlDTO] = []
    total: int
    offset: int
    limit: int


# --- відправки й календар -------------------------------------------------


class IndexingSubmissionDTO(BaseModel):
    id: str
    client_id: str
    client_name: str
    indexing_url_id: str
    url: str
    planned_for: date
    status: SubmissionStatus

    # Заморожені на момент планування — не перечитуються з URL.
    reason_code: ReasonCode
    reason: dict[str, Any] = {}
    state_at_plan: str | None = None
    action: UrlAction | None = None

    submitted_at: datetime | None = None
    submitted_by: str | None = None
    state_at_submission: str | None = None
    indexed_at: datetime | None = None
    days_to_index: int | None = None
    note: str | None = None
    # Чи ще можна натиснути Undo (24 год від відмітки).
    undoable: bool = False
    gsc_link: str | None = None
    title: str | None = None


class IndexingDayGroupDTO(BaseModel):
    """Один клієнт в один день — група рядків на екрані Today."""

    client_id: str
    client_name: str
    daily_quota: int
    planned: int
    sent: int
    quota_blocked: int
    submissions: list[IndexingSubmissionDTO] = []
    # Черга клієнта вичерпана — порожній день це нормальний стан, а не
    # помилка (план §1.5 п.1: у семи з десяти клієнтів беклог ≤ 3 URL).
    queue_exhausted: bool = False


class IndexingTodayDTO(BaseModel):
    day: date
    groups: list[IndexingDayGroupDTO] = []
    planned_total: int
    sent_total: int


class IndexingCalendarCellDTO(BaseModel):
    client_id: str
    day: date
    planned: int
    sent: int
    reason_counts: dict[str, int] = {}


class IndexingCalendarDTO(BaseModel):
    days: list[date] = []
    # Сьогоднішній день заморожений: нічний переплан його не чіпає.
    frozen_day: date
    clients: list[IndexingClientDTO] = []
    cells: list[IndexingCalendarCellDTO] = []
    queue_size: int
    daily_capacity: int
    days_to_clear_queue: int | None = None
    cycle_holds: bool | None = None


# --- скани ----------------------------------------------------------------


class IndexingScanRequest(BaseModel):
    client_id: str | None = None
    full: bool = False


class IndexingSitemapErrorDTO(BaseModel):
    url: str
    error: str


class IndexingScanDTO(BaseModel):
    id: str
    client_id: str | None = None
    client_name: str | None = None
    status: ScanStatus
    trigger: ScanTrigger
    started_at: datetime
    finished_at: datetime | None = None
    # `None` = прогін упав до того, як порахував свій обсяг. Не `0`.
    urls_total: int | None = None
    urls_inspected: int = 0
    urls_failed: int = 0
    sitemap_added: int = 0
    sitemap_gone: int = 0
    sitemap_errors: list[IndexingSitemapErrorDTO] = []
    error_code: str | None = None
    error_message: str | None = None


# --- аналітика ------------------------------------------------------------


class IndexingFunnelDTO(BaseModel):
    planned: int
    sent: int
    quota_blocked: int
    indexed: int
    # Відправлені, які ще не зайшли і ще не «провалились» — спостереження
    # триває. Без цього числа медіана лагу їхала б униз після кожної
    # великої відправки (план §2.6).
    in_flight: int


class IndexingLagDTO(BaseModel):
    """Розподіл днів «відправили -> побачили в індексі»."""

    sample: int
    median_days: Decimal | None = None
    p25_days: Decimal | None = None
    p75_days: Decimal | None = None


class IndexingControlGroupDTO(BaseModel):
    """Контрольна група: сторінки, що зайшли в індекс БЕЗ нашої відправки.

    Це і є єдина чесна відповідь на питання «чи ручне індексування взагалі
    щось дає». Якщо різниці немає — тул зобов'язаний це показати.
    """

    submitted: IndexingLagDTO
    organic: IndexingLagDTO
    # `median(organic) - median(submitted)`; `None`, коли будь-яка з вибірок
    # порожня — різниця з невідомим не є нулем.
    difference_days: Decimal | None = None


class IndexingReasonStatDTO(BaseModel):
    reason_code: ReasonCode
    sent: int
    indexed: int
    in_flight: int
    median_days: Decimal | None = None
    # `None`, доки немає жодного завершеного спостереження.
    conversion: Decimal | None = None


class IndexingRetentionDTO(BaseModel):
    horizon_days: int
    observed: int
    still_indexed: int
    dropped: int


class IndexingClientStatDTO(BaseModel):
    client_id: str
    client_name: str
    urls_total: int | None = None
    coverage_ratio: Decimal | None = None
    queue: int | None = None
    sent_30d: int
    median_days: Decimal | None = None
    cycle_holds: bool | None = None


class IndexingAnalyticsDTO(BaseModel):
    window_days: int
    funnel: IndexingFunnelDTO
    lag: IndexingLagDTO
    control_group: IndexingControlGroupDTO
    by_reason: list[IndexingReasonStatDTO] = []
    retention: list[IndexingRetentionDTO] = []
    by_client: list[IndexingClientStatDTO] = []
