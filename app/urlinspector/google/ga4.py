"""GA4 Data API про один URL (task-88 §3.2).

Чотири речі, які цей модуль тримає в одному місці, бо кожна з них уже раз
з'їла живий прогін:

**1. Дві дименсії сторінки — дві різні відповіді (§0.4).** `landingPage`
відповідає на «скільки СЕСІЙ увійшло на сайт через цю сторінку»,
`pagePath` — на «скільки ПЕРЕГЛЯДІВ мала ця сторінка в будь-якому місці
шляху». Числа на тому самому URL відрізняються (32 сесії проти 52) і жодне
не є «правильнішим» — це різні питання, і панель мусить показувати їх
окремо, а не одне під іменем іншого.

**2. Кінцевий слеш зливає лише `landingPage`.** Виміряно: `landingPage` сама
зводить `/a` і `/a/` в один рядок, а `pagePath` і
`landingPagePlusQueryString` — ні, і `EXACT` по одному написанню губить
13-19 % даних. Тому кожен фільтр тут — `inListFilter` з обома варіантами
(`ga4_landing_variants`), навіть де дименсія зливає сама: так правило одне
на всі запити й не залежить від того, яку дименсію взяли.

**3. Ліміти запиту (§0.8).** ≤10 метрик і ≤8 дименсій на `runReport`, і 11-та
метрика падає непрозорим `400 INVALID_ARGUMENT`. Повний набір landing-метрик
— 15 полів, тож `_batched` є єдиною дорогою метрик у запит.

**4. Конверсії за назвою — тільки через `eventName`.** `keyEvents:<name>`
падає, якщо в назві є пробіл (`keyEvents:Contact Us` → 400, і `%20` не
рятує). Дименсія `eventName` + метрика `keyEvents` дає ті самі числа для
будь-якої назви й одним запитом на всі конверсії.

`engagementRate`/`bounceRate` віддаються як частка `0..1` — рівно як їх дає
API. У БД той самий показник лежить відсотком (Reporting Ninja), і змішати
їх означає повторити пастку ×100 з task-83 §7.3.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Any

from app.integrations.google_api.auth import GoogleApiSession
from app.integrations.google_api.errors import GoogleApiError
from app.urlinspector.google.fanout import fanout

_GA4_API_BASE = "https://analyticsdata.googleapis.com/v1beta"
_PAGE_LIMIT = 100_000

# Ліміти одного `runReport`, виміряні бісекцією (§0.8). 11 метрик і 9
# дименсій дають 400 INVALID_ARGUMENT без жодної підказки, яке саме поле
# зайве.
MAX_METRICS_PER_REQUEST = 10
MAX_DIMENSIONS_PER_REQUEST = 8

# Session-scoped: «що сталося з сесіями, які увійшли через цю сторінку».
LANDING_METRICS: tuple[str, ...] = (
    "sessions",
    "totalUsers",
    "newUsers",
    "engagedSessions",
    "engagementRate",
    "bounceRate",
    "averageSessionDuration",
    "userEngagementDuration",
    "screenPageViews",
    "screenPageViewsPerSession",
    "keyEvents",
    "sessionKeyEventRate",
    "active1DayUsers",
    "active7DayUsers",
    "active28DayUsers",
)

# Page-scoped: «що сталося на цій сторінці, куди б людина не увійшла».
# `keyEvents` тут свідомо немає — з фільтром по `pagePath` вона віддає
# `None` (§0.8), бо конверсія не належить сторінці, а сесії.
PAGE_METRICS: tuple[str, ...] = (
    "screenPageViews",
    "activeUsers",
    "totalUsers",
    "userEngagementDuration",
    "eventCount",
    "scrolledUsers",
)

# Метрики серії — навмисно короткий набір: серія малюється, а не читається
# таблицею, і 15 ліній на графіку нікому не потрібні.
SERIES_METRICS: tuple[str, ...] = (
    "sessions",
    "totalUsers",
    "screenPageViews",
    "engagementRate",
    "keyEvents",
)

# Метрики GSC всередині GA4 (потребують GSC<->GA4-лінку). Працюють ЛИШЕ з
# дименсією `landingPagePlusQueryString` самою — див. §0.8.
ORGANIC_SEARCH_METRICS: tuple[str, ...] = (
    "organicGoogleSearchClicks",
    "organicGoogleSearchImpressions",
    "organicGoogleSearchAveragePosition",
    "organicGoogleSearchClickThroughRate",
)

LANDING_FIELD = "landingPage"
PAGE_FIELD = "pagePath"
ORGANIC_FIELD = "landingPagePlusQueryString"


@dataclass(frozen=True)
class Ga4KeyedRow:
    keys: tuple[str, ...]
    metrics: dict[str, Decimal | None]


@dataclass(frozen=True)
class Ga4Realtime:
    active_users: Decimal | None
    # Realtime API не приймає фільтр по `pagePath`, тому значення завжди
    # по property, а не по сторінці. Прапорець існує, щоб UI не міг подати
    # його як «зараз на цій сторінці».
    scope: str = "property"


class Ga4UrlClient:
    """Усі запити GA4, звужені до одного URL."""

    def __init__(
        self, session: GoogleApiSession, *, property_id: str, path_variants: Sequence[str]
    ) -> None:
        self._session = session
        self._property = property_id
        self._variants = list(path_variants)

    # ── тотали ───────────────────────────────────────────────────────────

    def landing_totals(
        self, start: date, end: date, *, executor: Executor | None = None
    ) -> dict[str, Decimal | None]:
        """Landing-scoped тотали за вікно, у пачках по ≤10 метрик.

        15 метрик не влазять у ліміт 10 (§0.8), тож це ЗАВЖДИ два запити —
        і послідовно вони коштували 796 мс проти ~400 мс паралельно.
        """
        return self._totals(start, end, LANDING_FIELD, LANDING_METRICS, executor)

    def page_totals(
        self, start: date, end: date, *, executor: Executor | None = None
    ) -> dict[str, Decimal | None]:
        """Page-scoped тотали за вікно."""
        return self._totals(start, end, PAGE_FIELD, PAGE_METRICS, executor)

    def organic_search_totals(self, start: date, end: date) -> dict[str, Decimal | None]:
        """GSC-метрики очима GA4 (§0.10) — вбудована звірка join'у по URL.

        Живо збіглися з прямим GSC API до значущої цифри (3 / 47 / 9.5531948),
        тому розбіжність тут читається як «зламався наш матчинг URL», а не
        як «розійшлись дані».

        ⚠️ Ці метрики існують ЛИШЕ у property, зв'язаному з Search Console.
        Без лінку API відповідає `400 INVALID_ARGUMENT` — не «нуль рядків».
        Це нормальний стан половини портфеля (виміряно гейтом G2: 2 клієнти
        з 4), тому 400 тут — порожній результат, а не збій: інакше кожне
        відкриття сторінки такого клієнта писало б у лог трейсбек і фарбувало
        звірку в `failed`, хоча лагодити нічого не треба.
        """
        out: dict[str, Decimal | None] = dict.fromkeys(ORGANIC_SEARCH_METRICS)
        for batch in _batched(ORGANIC_SEARCH_METRICS, MAX_METRICS_PER_REQUEST):
            try:
                rows = self._run(
                    start,
                    end,
                    dimensions=[ORGANIC_FIELD],
                    metrics=batch,
                    dimension_filter=self._in_list(ORGANIC_FIELD),
                    limit=len(self._variants) + 1,
                )
            except GoogleApiError as exc:
                if "400" in str(exc):
                    return out
                raise
            # Дименсія тут обов'язкова (без неї 400), тож рядків може бути
            # кілька — по одному на написання шляху. Складаємо кількісні,
            # а позицію/CTR лишаємо лише коли рядок один: усереднити їх
            # без ваги неможливо, а вигадати вагу — гірше за `None`.
            self._merge_organic(out, rows)
        return out

    # ── серії ────────────────────────────────────────────────────────────

    def daily_series(self, start: date, end: date) -> list[Ga4KeyedRow]:
        """Денна landing-серія. День, якого API не віддав, просто відсутній —
        ніколи не синтезується як нуль (PLAN §12)."""
        return self._run(
            start,
            end,
            dimensions=["date"],
            metrics=SERIES_METRICS,
            dimension_filter=self._in_list(LANDING_FIELD),
        )

    # ── зрізи ────────────────────────────────────────────────────────────

    def breakdown(
        self,
        start: date,
        end: date,
        *,
        dimensions: Sequence[str],
        metrics: Sequence[str],
        scope: str = LANDING_FIELD,
        limit: int = 200,
    ) -> list[Ga4KeyedRow]:
        """Довільний зріз URL. `scope` вибирає, ЯКЕ питання ставиться:
        `landingPage` — про сесії входу, `pagePath` — про перегляди."""
        return self._run(
            start,
            end,
            dimensions=dimensions,
            metrics=metrics,
            dimension_filter=self._in_list(scope),
            limit=limit,
        )

    def next_pages(self, start: date, end: date, *, limit: int = 20) -> list[Ga4KeyedRow]:
        """Куди люди йдуть із цієї сторінки (§4, п. 18).

        Фільтр по `pageReferrer` — `CONTAINS` по шляху, а не `EXACT`: реферер
        зберігається повним URL зі схемою й хостом, тож точний збіг зі
        шляхом не спрацював би ніколи.
        """
        longest = max(self._variants, key=len) if self._variants else ""
        return self._run(
            start,
            end,
            dimensions=[PAGE_FIELD],
            metrics=["screenPageViews", "sessions"],
            dimension_filter={
                "filter": {
                    "fieldName": "pageReferrer",
                    "stringFilter": {"matchType": "CONTAINS", "value": longest},
                }
            },
            limit=limit,
        )

    def key_events_by_name(
        self, start: date, end: date, *, limit: int = 50
    ) -> list[Ga4KeyedRow]:
        """Конверсії на сторінці, розкладені за назвою події (§4, п. 17).

        Свідомо через дименсію `eventName`, а не `keyEvents:<name>`: остання
        падає на будь-якій назві з пробілом (§0.8), тобто набір конверсій
        клієнта тихо різався б за наявністю пробілу в назві.
        """
        return self._run(
            start,
            end,
            dimensions=["eventName"],
            metrics=["keyEvents", "eventCount"],
            dimension_filter=self._in_list(LANDING_FIELD),
            limit=limit,
        )

    def page_events(self, start: date, end: date, *, limit: int = 50) -> list[Ga4KeyedRow]:
        """Усі події сторінки (§4, п. 29) — page-scoped, на відміну від
        конверсій вище."""
        return self._run(
            start,
            end,
            dimensions=["eventName"],
            metrics=["eventCount", "totalUsers"],
            dimension_filter=self._in_list(PAGE_FIELD),
            limit=limit,
        )

    # ── realtime ─────────────────────────────────────────────────────────

    def realtime_active_users(self) -> Ga4Realtime:
        """Активні зараз (§4, п. 28). Окремий бюджет і окремий виклик: це
        значення істинне лише в момент запиту, тому воно ніколи не їде в
        одній відповіді з історичними тоталами."""
        payload = self._session.post_json(
            f"{_GA4_API_BASE}/{self._property}:runRealtimeReport",
            {"metrics": [{"name": "activeUsers"}], "limit": 1},
        )
        rows = payload.get("rows") or []
        if not rows:
            return Ga4Realtime(active_users=None)
        values = rows[0].get("metricValues") or []
        return Ga4Realtime(active_users=_num(values[0].get("value")) if values else None)

    # ── метадані property ────────────────────────────────────────────────

    def key_event_names(self) -> list[str]:
        """Назви key events, налаштованих у цьому property (§0.9).

        Читаються з `metadata`, а не прошиті: HWELL має `Contact Us`,
        `Email`, `Insurance Form`, `Phone`, `purchase`, і в кожного клієнта
        набір свій. Порожній список — валідна відповідь (конверсій не
        налаштовано), не помилка.
        """
        payload = self._session.get_json(f"{_GA4_API_BASE}/{self._property}/metadata")
        names: list[str] = []
        for metric in payload.get("metrics") or []:
            api_name = str((metric or {}).get("apiName") or "")
            prefix, sep, name = api_name.partition(":")
            if sep and prefix == "keyEvents" and name:
                names.append(name)
        return names

    # ── внутрішнє ────────────────────────────────────────────────────────

    def _in_list(self, field: str) -> dict[str, Any]:
        return {"filter": {"fieldName": field, "inListFilter": {"values": self._variants}}}

    def _totals(
        self,
        start: date,
        end: date,
        field: str,
        metrics: Sequence[str],
        executor: Executor | None = None,
    ) -> dict[str, Decimal | None]:
        out: dict[str, Decimal | None] = dict.fromkeys(metrics)
        results = fanout(
            [
                partial(
                    self._run,
                    start,
                    end,
                    dimensions=[],
                    metrics=batch,
                    dimension_filter=self._in_list(field),
                    limit=1,
                )
                for batch in _batched(metrics, MAX_METRICS_PER_REQUEST)
            ],
            executor=executor,
        )
        for result in results:
            if isinstance(result, BaseException):
                # Пачка метрик — це половина одного набору тоталів; віддати
                # половину під іменем цілого гірше за honest `failed` на всю
                # панель, тому тут виняток піднімається, а не глушиться.
                raise result
            if result:
                out.update(result[0].metrics)
        return out

    def _run(
        self,
        start: date,
        end: date,
        *,
        dimensions: Sequence[str],
        metrics: Sequence[str],
        dimension_filter: dict[str, Any] | None,
        limit: int = _PAGE_LIMIT,
    ) -> list[Ga4KeyedRow]:
        if len(metrics) > MAX_METRICS_PER_REQUEST:
            raise ValueError(
                f"GA4 accepts at most {MAX_METRICS_PER_REQUEST} metrics per request, "
                f"got {len(metrics)} — batch them through _batched()"
            )
        if len(dimensions) > MAX_DIMENSIONS_PER_REQUEST:
            raise ValueError(
                f"GA4 accepts at most {MAX_DIMENSIONS_PER_REQUEST} dimensions per "
                f"request, got {len(dimensions)}"
            )
        body: dict[str, Any] = {
            "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
            "metrics": [{"name": name} for name in metrics],
            "limit": limit,
        }
        if dimensions:
            body["dimensions"] = [{"name": name} for name in dimensions]
        if dimension_filter is not None:
            body["dimensionFilter"] = dimension_filter

        out: list[Ga4KeyedRow] = []
        offset = 0
        while True:
            payload = self._session.post_json(
                f"{_GA4_API_BASE}/{self._property}:runReport", {**body, "offset": offset}
            )
            rows = payload.get("rows") or []
            dim_headers = [
                str(h.get("name")) for h in payload.get("dimensionHeaders") or []
            ] or list(dimensions)
            met_headers = [
                str(h.get("name")) for h in payload.get("metricHeaders") or []
            ] or list(metrics)
            for row in rows:
                dim_values = [d.get("value") for d in row.get("dimensionValues") or []]
                met_values = [m.get("value") for m in row.get("metricValues") or []]
                out.append(
                    Ga4KeyedRow(
                        keys=tuple(
                            _normalize_dimension(name, value)
                            for name, value in zip(dim_headers, dim_values, strict=False)
                        ),
                        metrics={
                            name: _num(value)
                            for name, value in zip(met_headers, met_values, strict=False)
                        },
                    )
                )
            offset += len(rows)
            row_count = _int(payload.get("rowCount"))
            if not rows or row_count is None or offset >= row_count or offset >= limit:
                return out

    @staticmethod
    def _merge_organic(
        out: dict[str, Decimal | None], rows: Sequence[Ga4KeyedRow]
    ) -> None:
        if not rows:
            return
        if len(rows) == 1:
            out.update(rows[0].metrics)
            return
        for name in rows[0].metrics:
            values = [r.metrics.get(name) for r in rows]
            present = [v for v in values if v is not None]
            if not present:
                out[name] = None
            elif name.endswith(("Clicks", "Impressions")):
                out[name] = sum(present, Decimal(0))
            else:
                # AveragePosition / ClickThroughRate по кількох рядках без ваги
                # звести неможливо — `None` честніший за середнє з середніх.
                out[name] = None


def _batched(items: Sequence[str], size: int) -> Iterator[tuple[str, ...]]:
    for start in range(0, len(items), size):
        yield tuple(items[start : start + size])


def _normalize_dimension(name: str, value: object) -> str:
    text = str(value or "").strip()
    if name == "date" and len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def _num(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
