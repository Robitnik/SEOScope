"""GSC-панелі URL Inspector'а з прямого Google API (task-88 §2.3).

Рівень «джерело панелі»: бере готовий `GscUrlClient` і перекладає його
дataclass'и в DTO, додаючи бакетизацію серії та стани покриття. Уся
семантика запитів (синтетичні нулі, регекс написань слеша, пагінація) лежить
у `app/urlinspector/google/gsc.py` і сюди не просочується.
"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import Executor
from datetime import date
from decimal import Decimal
from functools import partial

from app.schemas.url_inspector import (
    UrlGoogleBreakdownDTO,
    UrlGoogleBreakdownRowDTO,
    UrlGoogleGscDTO,
    UrlGoogleMetricDTO,
    UrlGoogleQueryDTO,
    UrlGoogleSearchTypeDTO,
    UrlGoogleSeriesDTO,
    UrlGoogleSeriesPointDTO,
    UrlSourceStateDTO,
)
from app.urlinspector.google.fanout import defer, fanout
from app.urlinspector.google.gsc import (
    SEARCH_TYPES,
    GscKeyedRow,
    GscMetrics,
    GscSeries,
    GscUrlClient,
)
from app.urlinspector.google.units import unit_of
from app.urlinspector.google.window import Bucket, GoogleWindow, bucket_of

# Скільком запитам сторінки дозволено доїхати до фронта. Виміряно: одна
# сторінка блогу цього портфеля має 692 запити (task-88 §0.2), і всі 692 в
# JSON — це кілограми даних, з яких оператор дивиться перші 30. Обрізаємо, але
# ЗАВЖДИ віддаємо повну кількість окремим полем, щоб «показано 200 із 692»
# було видно словами.
_QUERY_CAP = 200

# Метрики GSC у порядку, у якому їх читає людина.
_METRIC_ORDER: tuple[str, ...] = ("clicks", "impressions", "ctr", "position")


def collect_gsc_core(
    client: GscUrlClient,
    *,
    url: str,
    window: GoogleWindow,
    query_cap: int = _QUERY_CAP,
    executor: Executor | None = None,
) -> UrlGoogleGscDTO:
    """Серія + тотали + запити сторінки — швидка хвиля (§5).

    Три незалежні запити, тож вони йдуть паралельно: послідовно це 1.4 с,
    паралельно — стільки, скільки найповільніший (§0.6).
    """
    deferred_series = defer(
        lambda: client.daily_series(url, window.start, window.end), executor=executor
    )
    deferred_totals = defer(
        lambda: client.totals(url, window.start, window.end), executor=executor
    )
    deferred_queries = defer(
        lambda: client.queries(url, window.start, window.end), executor=executor
    )
    series = deferred_series.result()
    totals = deferred_totals.result()
    queries = deferred_queries.result()

    has_any = bool(series.rows) or not totals.is_empty or bool(queries)
    state = UrlSourceStateDTO(
        source="gsc",
        state="has_data" if has_any else "no_data",
        detail=None
        if has_any
        else _no_data_detail(window),
    )
    ranked = sorted(
        queries,
        key=lambda row: (
            -(row.metrics.clicks or Decimal(0)),
            -(row.metrics.impressions or Decimal(0)),
        ),
    )
    return UrlGoogleGscDTO(
        state=state,
        totals=_metrics_list(totals),
        series=_series_dtos(series, window.bucket),
        queries=[
            UrlGoogleQueryDTO(
                query=row.keys[0],
                clicks=row.metrics.clicks,
                impressions=row.metrics.impressions,
                ctr=row.metrics.ctr,
                position=row.metrics.position,
            )
            for row in ranked[:query_cap]
        ],
        queries_total=len(queries),
        queries_truncated=len(queries) > query_cap,
        first_incomplete_date=series.first_incomplete_date,
    )


def collect_gsc_breakdowns(
    client: GscUrlClient,
    *,
    url: str,
    window: GoogleWindow,
    executor: Executor | None = None,
) -> list[UrlGoogleBreakdownDTO]:
    """Зрізи GSC — ліниві (§5, хвиля 2)."""
    specs: tuple[tuple[str, str, tuple[str, ...], str | None], ...] = (
        ("gsc_device", "Devices", ("device",), None),
        ("gsc_country", "Countries", ("country",), None),
        (
            "gsc_search_appearance",
            "SERP appearance",
            ("searchAppearance",),
            "Rich-result appearances Google recorded for this page. Empty is a "
            "valid answer, not a failure — and this dimension cannot be crossed "
            "with any other one.",
        ),
        ("gsc_query_device", "Queries by device", ("query", "device"), None),
    )
    def fetch(dimensions: tuple[str, ...], row_limit: int) -> list[GscKeyedRow]:
        return client.keyed(
            url, window.start, window.end, dimensions=list(dimensions), row_limit=row_limit
        )

    limits = [50 if "query" in dimensions else 200 for _, _, dimensions, _ in specs]
    results = fanout(
        [
            partial(fetch, spec[2], limit)
            for spec, limit in zip(specs, limits, strict=True)
        ],
        executor=executor,
    )

    out: list[UrlGoogleBreakdownDTO] = []
    for (key, label, dimensions, note), row_limit, result in zip(
        specs, limits, results, strict=True
    ):
        if isinstance(result, BaseException):
            # Збій ОДНОГО зрізу не має гасити решту — картка лишається на
            # сторінці зі своїм станом (§2.5).
            out.append(
                UrlGoogleBreakdownDTO(
                    key=key,
                    label=label,
                    source="gsc",
                    dimensions=list(dimensions),
                    state=UrlSourceStateDTO(
                        source="gsc",
                        state="failed",
                        detail=f"{type(result).__name__}: {result}",
                    ),
                    row_limit=row_limit,
                    note=note,
                )
            )
            continue
        ranked = sort_key_clicks(result)
        out.append(
            UrlGoogleBreakdownDTO(
                key=key,
                label=label,
                source="gsc",
                dimensions=list(dimensions),
                state=UrlSourceStateDTO(
                    source="gsc",
                    state="has_data" if ranked else "no_data",
                    detail=None if ranked else "No rows in this dimension for this URL.",
                ),
                rows=[_breakdown_row(row) for row in ranked[:row_limit]],
                row_limit=row_limit,
                truncated=len(ranked) > row_limit,
                note=note,
            )
        )
    return out


def collect_search_types(
    client: GscUrlClient,
    *,
    url: str,
    window: GoogleWindow,
    executor: Executor | None = None,
) -> list[UrlGoogleSearchTypeDTO]:
    """Тотали URL у кожному типі пошуку (§4, п. 14).

    Порожній тип лишається в списку: «сторінка не показується в Картинках» —
    це відповідь, а зникла картка — ні (frontend/AGENTS.md §6). Так само
    порожнім лишається тип, запит по якому впав: `EMPTY_METRICS` тут означає
    «немає числа», і це саме те, що треба показати.
    """
    results = fanout(
        [
            partial(
                client.totals, url, window.start, window.end, search_type=search_type
            )
            for search_type in SEARCH_TYPES
        ],
        executor=executor,
    )
    return [
        UrlGoogleSearchTypeDTO(
            search_type=search_type,
            metrics=_metrics_list(None if isinstance(result, BaseException) else result),
            supports_queries=search_type not in ("discover", "googleNews"),
        )
        for search_type, result in zip(SEARCH_TYPES, results, strict=True)
    ]


# ── переклад у DTO ───────────────────────────────────────────────────────


def _metrics_list(metrics: GscMetrics | None) -> list[UrlGoogleMetricDTO]:
    values = {
        "clicks": metrics.clicks if metrics else None,
        "impressions": metrics.impressions if metrics else None,
        "ctr": metrics.ctr if metrics else None,
        "position": metrics.position if metrics else None,
    }
    return [
        UrlGoogleMetricDTO(metric=name, unit=unit_of(name), value=values[name])
        for name in _METRIC_ORDER
    ]


def _breakdown_row(row: GscKeyedRow) -> UrlGoogleBreakdownRowDTO:
    return UrlGoogleBreakdownRowDTO(
        keys=list(row.keys), metrics=_metrics_list(row.metrics)
    )


def _series_dtos(series: GscSeries, bucket: Bucket) -> dict[str, UrlGoogleSeriesDTO]:
    return {
        metric: UrlGoogleSeriesDTO(
            metric=metric,
            unit=unit_of(metric),
            bucket=bucket,
            points=_bucketize(series, metric, bucket),
        )
        for metric in _METRIC_ORDER
    }


def _bucketize(
    series: GscSeries, metric: str, bucket: Bucket
) -> list[UrlGoogleSeriesPointDTO]:
    """Денні рядки → бакети вікна.

    `clicks`/`impressions` складаються, `ctr`/`position` **важаться показами**
    — просте середнє тут було б неправильним: день з 1 показом на позиції 60
    не важить стілько ж, скільки день з 200 показами на позиції 3. Саме так
    агрегує сам GSC, і саме тому тотал панелі беруть окремим запитом, а не
    сумою серії.
    """
    grouped: dict[tuple[date, date], list[tuple[Decimal | None, Decimal | None, bool]]] = {}
    for row in series.rows:
        key = bucket_of(row.day, bucket)
        value = getattr(row.metrics, metric)
        grouped.setdefault(key, []).append(
            (value, row.metrics.impressions, row.provisional)
        )

    points: list[UrlGoogleSeriesPointDTO] = []
    for (start, end), entries in sorted(grouped.items()):
        present = [(v, w) for v, w, _ in entries if v is not None]
        provisional = any(p for _, _, p in entries)
        if not present:
            value = None
        elif metric in ("clicks", "impressions"):
            value = sum((v for v, _ in present), Decimal(0))
        else:
            weights = [w or Decimal(0) for _, w in present]
            total_weight = sum(weights, Decimal(0))
            value = (
                sum((v * w for (v, _), w in zip(present, weights, strict=True)), Decimal(0))
                / total_weight
                if total_weight
                else None
            )
        points.append(
            UrlGoogleSeriesPointDTO(
                period_start=start,
                period_end=end,
                period_days=(end - start).days + 1,
                value=value,
                provisional=provisional,
            )
        )
    return points


def _no_data_detail(window: GoogleWindow) -> str:
    """Порожнеча на межі історії й порожнеча всередині історії — різні
    відповіді, і UI мусить казати саме ту, що правда.

    Перевіряється `at_gsc_history_edge`, а не `clamped_to_gsc_history`:
    обрізання настає лише для вікна, ДОВШОГО за стелю, а найдовше наявне
    (`16m`) впирається в стелю не переходячи її — тобто по `clamped` цей
    текст не показався б ніколи (зловлено тестом).
    """
    if window.at_gsc_history_edge:
        return (
            "No Search Console rows for this URL in this window. It reaches "
            "Search Console's ~16 months of history, so an empty left edge "
            "may be that limit rather than no traffic."
        )
    return "No Search Console rows for this URL in this window."


def sort_key_clicks(rows: Sequence[GscKeyedRow]) -> list[GscKeyedRow]:
    """Публічний хелпер для тестів: той самий порядок, що бачить фронт."""
    return sorted(
        rows,
        key=lambda row: (
            -(row.metrics.clicks or Decimal(0)),
            -(row.metrics.impressions or Decimal(0)),
        ),
    )
