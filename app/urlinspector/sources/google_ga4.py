"""GA4-панелі URL Inspector'а з прямого Google API (task-88 §2.3).

Той самий рівень, що `google_gsc.py`: переклад у DTO + бакетизація + стани.
Уся семантика (дві дименсії сторінки, `inListFilter`, ліміти запиту, обхід
для назв конверсій із пробілом) лежить у `app/urlinspector/google/ga4.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Executor
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial

from app.schemas.url_inspector import (
    UrlGoogleBreakdownDTO,
    UrlGoogleBreakdownRowDTO,
    UrlGoogleGa4DTO,
    UrlGoogleKeyEventDTO,
    UrlGoogleMetricDTO,
    UrlGoogleSeriesDTO,
    UrlGoogleSeriesPointDTO,
    UrlRealtimeDTO,
    UrlSourceStateDTO,
)
from app.urlinspector.google.fanout import defer, fanout
from app.urlinspector.google.ga4 import (
    LANDING_FIELD,
    LANDING_METRICS,
    PAGE_FIELD,
    PAGE_METRICS,
    SERIES_METRICS,
    Ga4KeyedRow,
    Ga4UrlClient,
)
from app.urlinspector.google.units import unit_of
from app.urlinspector.google.window import Bucket, GoogleWindow, bucket_of

# Метрики, що складаються по бакету. Решта (ratio/position/seconds-середні)
# зводиться середнім із вагою `sessions`, а без ваги — не зводиться взагалі.
_ADDITIVE = frozenset(
    {
        "sessions",
        "totalUsers",
        "newUsers",
        "engagedSessions",
        "screenPageViews",
        "keyEvents",
        "eventCount",
        "activeUsers",
        "scrolledUsers",
        "userEngagementDuration",
    }
)


def collect_ga4_core(
    client: Ga4UrlClient, *, window: GoogleWindow, executor: Executor | None = None
) -> UrlGoogleGa4DTO:
    """Тотали (landing + page) + серія + конверсії за назвою — швидка хвиля.

    `landing_totals` — це сама по собі два запити (15 метрик не влазять в
    ліміт 10, §0.8), тож усього тут п'ять звернень, і всі незалежні.
    """
    deferred_landing = defer(
        lambda: client.landing_totals(window.start, window.end, executor=executor),
        executor=executor,
    )
    deferred_page = defer(
        lambda: client.page_totals(window.start, window.end, executor=executor),
        executor=executor,
    )
    deferred_series = defer(
        lambda: client.daily_series(window.start, window.end), executor=executor
    )
    deferred_events = defer(
        lambda: client.key_events_by_name(window.start, window.end), executor=executor
    )
    landing = deferred_landing.result()
    page = deferred_page.result()
    series_rows = deferred_series.result()
    key_events = deferred_events.result()

    has_any = any(v is not None for v in landing.values()) or any(
        v is not None for v in page.values()
    )
    return UrlGoogleGa4DTO(
        state=UrlSourceStateDTO(
            source="ga4",
            state="has_data" if has_any else "no_data",
            detail=None if has_any else "No GA4 rows for this URL in this window.",
        ),
        landing_totals=_metrics_list(landing, LANDING_METRICS),
        page_totals=_metrics_list(page, PAGE_METRICS),
        series=_series_dtos(series_rows, window.bucket),
        key_events=[
            UrlGoogleKeyEventDTO(
                name=row.keys[0],
                key_events=row.metrics.get("keyEvents"),
                event_count=row.metrics.get("eventCount"),
            )
            for row in sorted(
                key_events,
                key=lambda r: -(r.metrics.get("keyEvents") or Decimal(0)),
            )
            # Лишаємо лише те, що справді сталося на цій сторінці. Список
            # НАЛАШТОВАНИХ конверсій property приклеює оркестратор окремим
            # полем — і живий прогін показав, чому це має бути саме так:
            # єдина конверсія на цій сторінці (`Address`) взагалі відсутня в
            # metadata-списку property, тобто той список не є ні повним, ні
            # авторитетним для «що тут сталося». А додати налаштовану-але-
            # відсутню конверсію рядком із `0` не можна: GA4 не віддає рядка
            # для події, якої не було, тож ми не знаємо, це `0` чи «немає
            # даних» (PLAN §12).
            if (row.metrics.get("keyEvents") or Decimal(0)) > 0
        ],
    )


def collect_ga4_breakdowns(
    client: Ga4UrlClient, *, window: GoogleWindow, executor: Executor | None = None
) -> list[UrlGoogleBreakdownDTO]:
    """Усі GA4-зрізи URL (§4, пп. 15, 16, 19, 22, 25, 26, 27, 29) — хвиля 2."""
    specs: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...], str, int, str | None], ...] = (
        (
            "ga4_channel",
            "Channels",
            ("sessionDefaultChannelGroup",),
            ("sessions", "totalUsers", "keyEvents"),
            LANDING_FIELD,
            30,
            None,
        ),
        (
            "ga4_first_user_channel",
            "First-touch channels",
            ("firstUserDefaultChannelGroup",),
            ("sessions", "totalUsers", "keyEvents"),
            LANDING_FIELD,
            30,
            "How the user first ever arrived on the site, not how this session "
            "started — the same attribution split the client report uses.",
        ),
        (
            "ga4_source_medium",
            "Sources",
            ("sessionSourceMedium",),
            ("sessions", "totalUsers", "keyEvents"),
            LANDING_FIELD,
            50,
            None,
        ),
        (
            "ga4_device_country",
            "Devices and countries",
            ("deviceCategory", "country"),
            ("sessions", "totalUsers", "keyEvents"),
            LANDING_FIELD,
            200,
            None,
        ),
        (
            "ga4_city",
            "Cities",
            ("city",),
            ("sessions", "totalUsers", "keyEvents"),
            LANDING_FIELD,
            50,
            None,
        ),
        (
            "ga4_language",
            "Languages",
            ("languageCode",),
            ("sessions", "totalUsers"),
            LANDING_FIELD,
            30,
            None,
        ),
        (
            "ga4_new_returning",
            "New vs returning",
            ("newVsReturning",),
            ("sessions", "totalUsers", "keyEvents"),
            LANDING_FIELD,
            10,
            None,
        ),
        (
            "ga4_page_title",
            "Page titles GA4 recorded",
            ("pageTitle",),
            ("sessions", "screenPageViews"),
            LANDING_FIELD,
            25,
            "More than one title on a single page usually means the title changed "
            "during the window — worth comparing with the archive.",
        ),
        (
            "ga4_events",
            "Events on this page",
            ("eventName",),
            ("eventCount", "totalUsers"),
            PAGE_FIELD,
            50,
            None,
        ),
    )

    tasks: list[Callable[[], list[Ga4KeyedRow]]] = [
        partial(
            client.breakdown,
            window.start,
            window.end,
            dimensions=spec[2],
            metrics=spec[3],
            scope=spec[4],
            limit=spec[5],
        )
        for spec in specs
    ]
    # Наступні сторінки їдуть у тому самому розкладі, хоч і мають інший
    # фільтр — питання «куди пішли звідси», а не «що сталося тут».
    tasks.append(partial(client.next_pages, window.start, window.end))
    results = fanout(tasks, executor=executor)

    out: list[UrlGoogleBreakdownDTO] = []
    for (key, label, dimensions, metrics, scope, limit, note), result in zip(
        specs, results[: len(specs)], strict=True
    ):
        if isinstance(result, BaseException):
            # Збій ОДНОГО зрізу не гасить решту (§2.5).
            out.append(
                UrlGoogleBreakdownDTO(
                    key=key,
                    label=label,
                    source="ga4",
                    dimensions=list(dimensions),
                    scope=scope,
                    state=UrlSourceStateDTO(
                        source="ga4",
                        state="failed",
                        detail=f"{type(result).__name__}: {result}",
                    ),
                    row_limit=limit,
                    note=note,
                )
            )
            continue
        ranked = sorted(result, key=partial(_rank_key, metrics))
        out.append(
            UrlGoogleBreakdownDTO(
                key=key,
                label=label,
                source="ga4",
                dimensions=list(dimensions),
                scope=scope,
                state=UrlSourceStateDTO(
                    source="ga4",
                    state="has_data" if ranked else "no_data",
                    detail=None if ranked else "No rows in this dimension for this URL.",
                ),
                rows=[
                    UrlGoogleBreakdownRowDTO(
                        keys=list(row.keys), metrics=_metrics_list(row.metrics, metrics)
                    )
                    for row in ranked
                ],
                row_limit=limit,
                truncated=len(ranked) >= limit,
                note=note,
            )
        )

    next_result = results[-1]
    next_rows = [] if isinstance(next_result, BaseException) else next_result
    next_metrics = ("screenPageViews", "sessions")
    out.append(
        UrlGoogleBreakdownDTO(
            key="ga4_next_pages",
            label="Where visitors went next",
            source="ga4",
            dimensions=["pagePath"],
            scope="pageReferrer",
            state=UrlSourceStateDTO(
                source="ga4",
                state="failed"
                if isinstance(next_result, BaseException)
                else ("has_data" if next_rows else "no_data"),
                detail=f"{type(next_result).__name__}: {next_result}"
                if isinstance(next_result, BaseException)
                else (
                    None
                    if next_rows
                    else "GA4 recorded no onward navigation from this page."
                ),
            ),
            rows=[
                UrlGoogleBreakdownRowDTO(
                    keys=list(row.keys), metrics=_metrics_list(row.metrics, next_metrics)
                )
                for row in sorted(next_rows, key=partial(_rank_key, next_metrics))
            ],
            row_limit=20,
            truncated=False,
            note="Matched on the referrer containing this page's path, so a page "
            "linked from a similar path can appear here.",
        )
    )
    return out


def collect_realtime(client: Ga4UrlClient) -> UrlRealtimeDTO:
    """Активні зараз (§4, п. 28). Значення завжди по property — GA4 Realtime
    не приймає фільтр по сторінці, і DTO це фіксує полем `scope`."""
    realtime = client.realtime_active_users()
    return UrlRealtimeDTO(
        state=UrlSourceStateDTO(
            source="ga4",
            state="has_data" if realtime.active_users is not None else "no_data",
            detail=None
            if realtime.active_users is not None
            else "No active users on the property right now.",
        ),
        active_users=realtime.active_users,
        generated_at=datetime.now(UTC),
    )


# ── переклад у DTO ───────────────────────────────────────────────────────


def _metrics_list(
    values: dict[str, Decimal | None], order: tuple[str, ...]
) -> list[UrlGoogleMetricDTO]:
    return [
        UrlGoogleMetricDTO(metric=name, unit=unit_of(name), value=values.get(name))
        for name in order
    ]


def _rank_key(metrics: tuple[str, ...], row: Ga4KeyedRow) -> Decimal:
    """Сортувальний ключ: перша присутня метрика зі спека, зі знаком мінус."""
    return -_rank_value(row, metrics)


def _rank_value(row: Ga4KeyedRow, metrics: tuple[str, ...]) -> Decimal:
    for name in metrics:
        value = row.metrics.get(name)
        if value is not None:
            return value
    return Decimal(0)


def _series_dtos(
    rows: list[Ga4KeyedRow], bucket: Bucket
) -> dict[str, UrlGoogleSeriesDTO]:
    return {
        metric: UrlGoogleSeriesDTO(
            metric=metric,
            unit=unit_of(metric),
            bucket=bucket,
            points=_bucketize(rows, metric, bucket),
        )
        for metric in SERIES_METRICS
    }


def _bucketize(
    rows: list[Ga4KeyedRow], metric: str, bucket: Bucket
) -> list[UrlGoogleSeriesPointDTO]:
    grouped: dict[tuple[date, date], list[tuple[Decimal | None, Decimal | None]]] = {}
    for row in rows:
        day = _parse_day(row.keys[0] if row.keys else "")
        if day is None:
            continue
        key = bucket_of(day, bucket)
        grouped.setdefault(key, []).append(
            (row.metrics.get(metric), row.metrics.get("sessions"))
        )

    points: list[UrlGoogleSeriesPointDTO] = []
    for (start, end), entries in sorted(grouped.items()):
        present = [(v, w) for v, w in entries if v is not None]
        if not present:
            value = None
        elif metric in _ADDITIVE:
            value = sum((v for v, _ in present), Decimal(0))
        else:
            weights = [w or Decimal(0) for _, w in present]
            total = sum(weights, Decimal(0))
            value = (
                sum((v * w for (v, _), w in zip(present, weights, strict=True)), Decimal(0))
                / total
                if total
                else None
            )
        points.append(
            UrlGoogleSeriesPointDTO(
                period_start=start,
                period_end=end,
                period_days=(end - start).days + 1,
                value=value,
            )
        )
    return points


def _parse_day(text: str) -> date | None:
    text = text.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None
