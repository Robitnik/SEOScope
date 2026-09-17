"""GA4 Events by Month (task-98 §2.2): для кожного обраного місяця — усі
`eventName` властивості клієнта з їхніми цифрами.

Три рішення, кожне з живого вимірювання (план §1):

**1. Джерело — прямий GA4 Data API, не Reporting Ninja.** `account_id`
(`properties/<id>`) уже лежить у `client_integrations` в тому форматі, який
приймає Google, і один портфельний refresh-токен відкриває всі 14 property
(§1.1). `config_json["transport"]` тут свідомо НЕ читається: він вирішує, чим
наповнюються збережені метрики (`#/trends`, звіти), а це — разове живе
питання, за яке не має платити RN-квота.

**2. Один запит на МІСЯЦЬ, паралельно.** Холодний запит `yearMonth×eventName`
на 24 місяці по «важкому» property заміряно у **46 с** (77% від
`google_api_timeout_seconds=60`), тоді як 12 по-місячних запитів у 6 потоках —
1.95 с (§1.5). Плюс ізоляція, яку ця форма дає безкоштовно: місяць, що впав,
стає своєю вкладкою `failed`, а не валить прогін (PLAN §9.1 на read-шляху).

**3. Три порізи на кожен івент.** Таблиця оператора (скрін із GA4, SDS,
липень 2026) — це не один рядок на івент, а три: `All Users`, `Direct traffic`
і `Organic traffic`, у кожному `Event Count (%)`, `Total Users (%)` і
`Event count per user`. Визначення порізів звірене з живими числами
(`Book Now 704 (2.53%) / 437 (11.05%) / 1.61` і далі рядок у рядок): це
`sessionDefaultChannelGroup` з **CONTAINS**, не EXACT. Різниця не косметична —
`EXACT "Organic Search"` дає ті самі 70 подій, але відсотки 1.22%/8.11%
замість 1.16%/7.71%, бо знаменник втрачає Organic Social/Video. Відсоток
рахується від тоталів СВОГО порізу, тому кожен поріз потребує власного запиту
без дименсії.

**4. `totalUsers` НЕ адитивні по івентах.** Заміряно на SAPHNY 2026-08: сума
колонки Users по рядках = 227 237 проти property-тоталу 58 556 (×3.9) — один
користувач рахується в кожному івенті, який тригернув. Тому `MonthTotals.users`
/`sessions` беруться ОКРЕМИМ запитом без дименсії `eventName`, і жоден код тут
не сміє скласти колонку users. `event_count` навпаки адитивний — заміряно
збіг байт-у-байт (364 617 = 364 617), тому він таки сума.
"""

from __future__ import annotations

import calendar
import logging
import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import ClientIntegration
from app.integrations.google_api import (
    Ga4Client,
    GoogleApiConfigError,
    GoogleApiError,
    GoogleApiPermissionError,
    GoogleApiSession,
    open_google_api_session,
)
from app.schemas.enums import Provider

logger = logging.getLogger(__name__)

# Стільки місяців дозволено за прогін. При одному запиті на місяць і 6 потоках
# це 2-5 с (§1.5) — межа не проти Google (квота 25k токенів/property/добу), а
# проти книги Excel на 100 аркушів і таблиці, яку неможливо читати.
MAX_MONTHS = 24

# Дименсія й метрики зрізу. Чотири метрики з десяти дозволених на `runReport`
# (§1.2): `eventValue`/`newUsers` для більшості івентів — структурні нулі,
# колонка нулів інформацією не є.
EVENT_DIMENSION = "eventName"
EVENT_METRICS: tuple[str, ...] = (
    "eventCount",
    "totalUsers",
    "eventCountPerUser",
    "keyEvents",
)
TOTALS_METRICS: tuple[str, ...] = ("eventCount", "totalUsers", "keyEvents")
MONTH_DIMENSION = "yearMonth"
CHANNEL_DIMENSION = "sessionDefaultChannelGroup"


def _channel_filter(contains: str) -> dict[str, Any]:
    return {
        "filter": {
            "fieldName": CHANNEL_DIMENSION,
            "stringFilter": {"matchType": "CONTAINS", "value": contains},
        }
    }


@dataclass(frozen=True)
class Segment:
    key: str
    label: str
    dimension_filter: dict[str, Any] | None


# Порядок тут = порядок рядків під кожним івентом, як у таблиці оператора.
SEGMENTS: tuple[Segment, ...] = (
    Segment("all", "All Users", None),
    Segment("direct", "Direct traffic", _channel_filter("Direct")),
    Segment("organic", "Organic traffic", _channel_filter("Organic")),
)
SEGMENT_KEYS: tuple[str, ...] = tuple(segment.key for segment in SEGMENTS)

# GA4 доуточнює вчорашні дані ще ~48 год — місяць, що закінчився менш ніж дві
# доби тому, чесніше показати як `Partial`, ніж мовчки видати за остаточний.
SETTLING_HOURS = 48

_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")

MonthState = str  # "ok" | "no_data" | "failed"
ReportState = str  # "ok" | "partial" | "not_configured" | "failed"


class Ga4EventsConfigError(Exception):
    """GA4 для цього клієнта недоступний — у прогін не входимо взагалі.

    Це `not_configured`, а не `failed`: нічого не зламалось, просто немає чого
    питати (PLAN §12). `detail` — готовий до показу текст англійською.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class SegmentValues:
    """Один поріз одного івента — рівно те, що стоїть у клітинці таблиці."""

    event_count: int | None
    users: int | None
    # `eventCountPerUser` беремо з GA4, а не ділимо самі: на живій звірці
    # (SDS, липень 2026) метрика API збігається з аркушем оператора в усіх
    # рядках (1.61 / 1.30 / 1.18), тож власна арифметика тут лише додала б
    # ще одне місце, де можна розійтися.
    per_user: Decimal | None
    # Частка від тоталів СВОГО порізу (події і користувачі окремо), 0..100.
    # `None`, коли тотал невідомий/нульовий — не `0.0` (PLAN §12).
    event_share_pct: Decimal | None = None
    user_share_pct: Decimal | None = None


@dataclass(frozen=True)
class EventRow:
    event_name: str
    is_key_event: bool
    # Скільки конверсій дав цей івент у порізі `All Users` — не колонка
    # таблиці оператора, але саме воно вирішує, що підсвітити як ключове.
    key_events: int | None
    segments: dict[str, SegmentValues]

    @property
    def all_users(self) -> SegmentValues | None:
        return self.segments.get("all")


@dataclass(frozen=True)
class SegmentTotals:
    """Тотали порізу на рівні property — окремий запит без `eventName` (§4).
    `users` тут НЕ дорівнює сумі колонки Users і не повинен."""

    event_count: int | None
    users: int | None
    key_events: int | None


@dataclass(frozen=True)
class MonthResult:
    month: str  # "YYYY-MM"
    label: str  # "Aug 2026"
    state: MonthState
    is_partial: bool
    rows: list[EventRow]
    totals: dict[str, SegmentTotals]
    error: str | None = None

    @property
    def key_event_count(self) -> int:
        return sum(1 for row in self.rows if row.is_key_event)


@dataclass(frozen=True)
class ReportResult:
    property_id: str
    state: ReportState
    months: list[MonthResult]
    generated_at: datetime


def month_label(month: str) -> str:
    year, index = month.split("-")
    return f"{calendar.month_abbr[int(index)]} {year}"


def month_bounds(month: str) -> tuple[date, date]:
    """Перший і останній день місяця. Майбутнє не обрізається тут — GA4 сам
    віддає лише те, що вже сталося, і домальовувати «сьогодні» як кінець
    діапазону означало б непомітно змінити питання."""
    year, index = (int(part) for part in month.split("-"))
    return date(year, index, 1), date(year, index, calendar.monthrange(year, index)[1])


def normalize_months(months: Sequence[str]) -> list[str]:
    """Валідація + дедуп + сортування (найновіший перший — саме так їх читає
    оператор, і так само лягають вкладки)."""
    seen: set[str] = set()
    for raw in months:
        value = str(raw).strip()
        if not _MONTH_RE.match(value):
            raise ValueError(f"month must be YYYY-MM, got {value!r}")
        seen.add(value)
    if not seen:
        raise ValueError("at least one month is required")
    if len(seen) > MAX_MONTHS:
        raise ValueError(f"at most {MAX_MONTHS} months per report, got {len(seen)}")
    return sorted(seen, reverse=True)


def previous_month(month: str) -> str:
    year, index = (int(part) for part in month.split("-"))
    return f"{year - 1}-12" if index == 1 else f"{year}-{index - 1:02d}"


def is_partial_month(month: str, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    _, last_day = month_bounds(month)
    closed_at = datetime(last_day.year, last_day.month, last_day.day, tzinfo=UTC) + timedelta(
        days=1
    )
    return now < closed_at + timedelta(hours=SETTLING_HOURS)


def resolve_property_id(session: Session, *, client_id: str) -> str:
    """`properties/<id>` цього клієнта, або `Ga4EventsConfigError`.

    Джерело — інтеграція `reporting_ninja`: назва провайдера історична (там
    живе GA4-акаунт клієнта), а значення вже у форматі Google API — задокументовано
    вимірюванням task-83 §0.3 і перевірено тут на всіх 14 клієнтах (§1.1).
    """
    integration = session.scalars(
        select(ClientIntegration).where(
            ClientIntegration.client_id == client_id,
            ClientIntegration.provider == Provider.reporting_ninja,
        )
    ).first()
    if integration is None:
        raise Ga4EventsConfigError("GA4 isn't connected for this client.")
    account_id = str((integration.config_json or {}).get("account_id") or "").strip()
    if not account_id:
        raise Ga4EventsConfigError("This client's GA4 integration has no property id.")
    return account_id


def _int(value: Decimal | None) -> int | None:
    return None if value is None else int(value)


def _share(value: int | None, total: int | None) -> Decimal | None:
    if value is None or not total:
        return None
    return (Decimal(value) * 100 / Decimal(total)).quantize(Decimal("0.01"))


def _safe_error(exc: Exception) -> str:
    """Текст для UI. Секрети (токен, client secret) у `GoogleApiError` не
    потрапляють за побудовою (`auth.py` будує повідомлення з алловлиста), але
    `str()` довільного винятку сюди не йде все одно — лише клас (PLAN §13)."""
    if isinstance(exc, GoogleApiError):
        return str(exc)
    return f"GA4 request failed ({type(exc).__name__})."


@dataclass(frozen=True)
class _SegmentData:
    """Сирий результат одного порізу за один місяць."""

    rows: dict[str, dict[str, Decimal | None]]
    totals: SegmentTotals | None


def _fetch_segment(
    client: Ga4Client, *, property_id: str, month: str, segment: Segment
) -> _SegmentData:
    start, end = month_bounds(month)
    rows = client.run_report(
        property_id,
        start,
        end,
        dimensions=[EVENT_DIMENSION],
        metrics=list(EVENT_METRICS),
        dimension_filter=segment.dimension_filter,
    )
    totals_rows = client.run_report(
        property_id,
        start,
        end,
        dimensions=[],
        metrics=list(TOTALS_METRICS),
        dimension_filter=segment.dimension_filter,
    )
    by_name: dict[str, dict[str, Decimal | None]] = {}
    for row in rows:
        name = str(row.dimensions.get(EVENT_DIMENSION) or "").strip()
        if name:
            by_name[name] = row.metrics
    totals = None
    if totals_rows:
        metrics = totals_rows[0].metrics
        totals = SegmentTotals(
            event_count=_int(metrics.get("eventCount")),
            users=_int(metrics.get("totalUsers")),
            key_events=_int(metrics.get("keyEvents")),
        )
    return _SegmentData(rows=by_name, totals=totals)


def _build_month(
    month: str, *, data: dict[str, _SegmentData], now: datetime
) -> MonthResult:
    label = month_label(month)
    partial = is_partial_month(month, now=now)
    totals = {key: value.totals for key, value in data.items() if value.totals is not None}

    names: list[str] = []
    seen: set[str] = set()
    # Порядок і склад рядків задає поріз `All Users`: івент, який існує лише
    # в Organic, усе одно має рядок (з порожнім `All Users`), а не зникає.
    for segment in SEGMENTS:
        for name in data.get(segment.key, _SegmentData({}, None)).rows:
            if name not in seen:
                seen.add(name)
                names.append(name)

    rows: list[EventRow] = []
    for name in names:
        segments: dict[str, SegmentValues] = {}
        for segment in SEGMENTS:
            metrics = data.get(segment.key, _SegmentData({}, None)).rows.get(name)
            if metrics is None:
                continue
            segment_totals = totals.get(segment.key)
            event_count = _int(metrics.get("eventCount"))
            users = _int(metrics.get("totalUsers"))
            per_user_raw = metrics.get("eventCountPerUser")
            segments[segment.key] = SegmentValues(
                event_count=event_count,
                users=users,
                per_user=(
                    None
                    if per_user_raw is None
                    else Decimal(per_user_raw).quantize(Decimal("0.01"))
                ),
                event_share_pct=_share(
                    event_count, segment_totals.event_count if segment_totals else None
                ),
                user_share_pct=_share(users, segment_totals.users if segment_totals else None),
            )
        key_events = _int(
            data.get("all", _SegmentData({}, None)).rows.get(name, {}).get("keyEvents")
        )
        rows.append(
            EventRow(
                event_name=name,
                is_key_event=bool(key_events),
                key_events=key_events,
                segments=segments,
            )
        )
    rows.sort(
        key=lambda item: (
            -((item.all_users.event_count if item.all_users else None) or 0),
            item.event_name.lower(),
        )
    )
    if not rows:
        return MonthResult(
            month=month,
            label=label,
            state="no_data",
            is_partial=partial,
            rows=[],
            totals=totals,
        )
    return MonthResult(
        month=month, label=label, state="ok", is_partial=partial, rows=rows, totals=totals
    )


def _failed_month(month: str, *, error: str, now: datetime) -> MonthResult:
    return MonthResult(
        month=month,
        label=month_label(month),
        state="failed",
        is_partial=is_partial_month(month, now=now),
        rows=[],
        totals={},
        error=error,
    )


def _report_state(months: list[MonthResult]) -> ReportState:
    states = {month.state for month in months}
    if states == {"failed"}:
        return "failed"
    if "failed" in states:
        return "partial"
    return "ok"


def run_events_report(
    *,
    property_id: str,
    months: Sequence[str],
    settings: Settings | None = None,
    session_factory: Callable[[], GoogleApiSession] | None = None,
    now: datetime | None = None,
) -> ReportResult:
    """Прогін тула: property × обрані місяці × три порізи.

    БД сюди не приходить свідомо: `property_id` резолвить викличник (одним
    коротким читанням `resolve_property_id`), а ця функція — чиста мережа.
    Інакше довелося б тримати ще одну сесію відкритою на весь похід у Google,
    а в тестах — ризикувати тим самим класом дефекту, що вже стався живо:
    фонова робота на сесії, чий `DATABASE_URL` резолвиться пізніше за
    `monkeypatch` (CLAUDE.md, пастка для тестів).

    Одиниця роботи в пулі — `(місяць, поріз)`, а не місяць: так 3 місяці це 9
    паралельних задач замість 3, і жодна не тримає пул зайнятим довше за свій
    власний запит. Впав поріз — падає ЙОГО місяць (вкладка `failed`), сусідні
    місяці лишаються живими.

    `session_factory` існує для тестів (фейковий `GoogleApiSession`) — у
    проді це `open_google_api_session`.
    """
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    ordered = normalize_months(months)

    factory = session_factory or (lambda: open_google_api_session(settings))
    try:
        api_session = factory()
    except GoogleApiConfigError as exc:
        raise Ga4EventsConfigError(str(exc)) from None

    tasks = [(month, segment) for month in ordered for segment in SEGMENTS]
    workers = max(1, min(6, len(tasks)))
    collected: dict[str, dict[str, _SegmentData]] = {month: {} for month in ordered}
    errors: dict[str, str] = {}

    def _one(task: tuple[str, Segment]) -> tuple[str, Segment, _SegmentData | Exception]:
        month, segment = task
        try:
            return month, segment, _fetch_segment(
                client, property_id=property_id, month=month, segment=segment
            )
        except Exception as exc:  # ізоляція вкладки, не падіння прогону
            logger.warning(
                "ga4_events: %s/%s failed: %s", month, segment.key, type(exc).__name__
            )
            return month, segment, exc

    try:
        client = Ga4Client(api_session)
        if workers == 1:
            results = [_one(tasks[0])]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(_one, tasks))
    finally:
        close = getattr(api_session, "close", None)
        if callable(close):
            close()

    for month, segment, outcome in results:
        if isinstance(outcome, Exception):
            errors.setdefault(month, _safe_error(outcome))
        else:
            collected[month][segment.key] = outcome

    # Немає доступу до property — це не «місяць впав», це стан клієнта.
    if errors and len(errors) == len(ordered) and all(
        "permission" in message.lower() for message in errors.values()
    ):
        raise Ga4EventsConfigError(
            "This Google account has no access to the client's GA4 property."
        )

    months_out = [
        _failed_month(month, error=errors[month], now=now)
        if month in errors
        else _build_month(month, data=collected[month], now=now)
        for month in ordered
    ]
    months_out.sort(key=lambda item: item.month, reverse=True)
    return ReportResult(
        property_id=property_id,
        state=_report_state(months_out),
        months=months_out,
        generated_at=now,
    )


def available_months(
    *,
    property_id: str,
    settings: Settings | None = None,
    session_factory: Callable[[], GoogleApiSession] | None = None,
    since: date | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Місяці, у яких у property РЕАЛЬНО є події — один запит `yearMonth`
    (заміряно 1.0-1.3 с на 24 місяці, §1.6).

    Порожній список означає «GA4 не віддав жодного місяця», а не «даних нема
    за замовчуванням»: вибір у формі будується з цього списку, тож оператор не
    може обрати місяць, якого не існує. Історія доступна глибше 14 місяців
    (§1.4), тому вікно за замовчуванням — 36 місяців.
    """
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    start = since or (now.date().replace(day=1) - timedelta(days=365 * 3))
    factory = session_factory or (lambda: open_google_api_session(settings))
    try:
        api_session = factory()
    except GoogleApiConfigError as exc:
        raise Ga4EventsConfigError(str(exc)) from None
    try:
        rows = Ga4Client(api_session).run_report(
            property_id,
            start,
            now.date(),
            dimensions=[MONTH_DIMENSION],
            metrics=["eventCount"],
        )
    except GoogleApiPermissionError as exc:
        raise Ga4EventsConfigError(str(exc)) from None
    finally:
        close = getattr(api_session, "close", None)
        if callable(close):
            close()
    months = {
        f"{value[:4]}-{value[4:6]}"
        for value in (
            str(row.dimensions.get(MONTH_DIMENSION) or "").strip() for row in rows
        )
        if len(value) == 6 and value.isdigit()
    }
    return sorted(months, reverse=True)
