"""Оркестратор Google-панелей URL Inspector'а (task-88 §2.2/§2.5).

Три хвилі, розділені за виміряною латентністю (§0.6), а не за смаком:

| Хвиля | Що | Бюджет |
|---|---|---|
| `core` | GSC серія/тотали/запити + GA4 тотали/серія/конверсії + звірка | 0.4-1.6 с |
| `breakdowns` | усі зрізи GSC і GA4 + типи пошуку | ~1-2 с |
| `index_status` | URL Inspection + сайтмапи | **6.6 с** |
| `realtime` | активні зараз | окремий, бо істинний лише зараз |

Ізоляція така сама, як у `service.py`: виняток в ОДНОМУ запиті стає станом
`failed` СВОЄЇ панелі, а не 500 на всю сторінку (PLAN §9.1, застосований до
read-шляху). `GoogleApiConfigError` (немає refresh token) і
`GoogleApiPermissionError` (немає доступу до property) — окремі стани
`not_configured`, бо це різні дії оператора: перше лагодить агенція, друге —
доступи в Google.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from typing import TypeVar

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.repositories.clients import ClientRepository
from app.integrations.google_api import (
    GoogleApiConfigError,
    GoogleApiPermissionError,
    GoogleApiSession,
    open_google_api_session,
)
from app.schemas.url_inspector import (
    UrlGoogleBreakdownDTO,
    UrlGoogleBreakdownsDTO,
    UrlGoogleCoreDTO,
    UrlGoogleCrossCheckDTO,
    UrlGoogleGa4DTO,
    UrlGoogleGscDTO,
    UrlGoogleWindowDTO,
    UrlIndexStatusDTO,
    UrlRealtimeDTO,
    UrlSourceStateDTO,
)
from app.urlinspector.google.ga4 import Ga4UrlClient
from app.urlinspector.google.gsc import GscUrlClient
from app.urlinspector.google.index import UrlInspectionClient
from app.urlinspector.google.target import GoogleTarget, resolve_google_target
from app.urlinspector.google.window import GoogleWindow, resolve_window
from app.urlinspector.resolve import resolve_client_for_url
from app.urlinspector.sources.google_ga4 import (
    collect_ga4_breakdowns,
    collect_ga4_core,
    collect_realtime,
)
from app.urlinspector.sources.google_gsc import (
    collect_gsc_breakdowns,
    collect_gsc_core,
    collect_search_types,
)
from app.urlinspector.sources.google_index import collect_index_status
from app.urlinspector.urlkey import ga4_landing_variants

logger = logging.getLogger(__name__)

T = TypeVar("T")


class GoogleTargetUnavailable(Exception):
    """Клієнта не знайдено / Google не налаштований — викличник перекладає це
    у стан панелі, а не в HTTP-помилку: сторінка `#/url` мусить лишитись
    сторінкою навіть коли жодне джерело недоступне."""

    def __init__(self, detail: str, *, state: str = "not_configured") -> None:
        super().__init__(detail)
        self.detail = detail
        self.state = state


def _window_dto(window: GoogleWindow) -> UrlGoogleWindowDTO:
    return UrlGoogleWindowDTO(
        key=window.key,
        start=window.start,
        end=window.end,
        days=window.days,
        bucket=window.bucket,
        at_gsc_history_edge=window.at_gsc_history_edge,
        clamped_to_gsc_history=window.clamped_to_gsc_history,
        requested_start=window.requested_start,
    )


def _resolve(session: Session, url: str) -> tuple[GoogleTarget, str]:
    resolved = resolve_client_for_url(url, ClientRepository(session).list_all())
    if resolved.client is None:
        raise GoogleTargetUnavailable(
            "No client matches this URL's host.", state="unknown_client"
        )
    target = resolve_google_target(session, client_id=resolved.client.id)
    if not target.has_any:
        raise GoogleTargetUnavailable(
            target.gsc_detail or target.ga4_detail or "Google isn't connected."
        )
    return target, resolved.client.id


def _guard(source: str, fn: Callable[[], T]) -> tuple[T | None, UrlSourceStateDTO | None]:
    """Виконує `fn`, перекладаючи будь-який збій у стан панелі.

    `GoogleApiConfigError`/`GoogleApiPermissionError` виділені навмисно: це не
    поломка, а відсутність налаштування, і фарбувати їх у `failed` означало б
    послати оператора шукати баг там, де треба видати доступ.
    """
    try:
        return fn(), None
    except GoogleApiConfigError as exc:
        return None, UrlSourceStateDTO(source=source, state="not_configured", detail=str(exc))
    except GoogleApiPermissionError as exc:
        return None, UrlSourceStateDTO(source=source, state="not_configured", detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — ізоляція клітинки, див. докстрінг модуля
        logger.exception("url-inspector google: %s failed", source)
        return None, UrlSourceStateDTO(
            source=source, state="failed", detail=f"{type(exc).__name__}: {exc}"
        )


def _clients(
    api: GoogleApiSession, target: GoogleTarget, url: str
) -> tuple[GscUrlClient | None, Ga4UrlClient | None]:
    gsc = (
        GscUrlClient(api, site_url=target.gsc_site_url) if target.gsc_site_url else None
    )
    ga4 = (
        Ga4UrlClient(
            api,
            property_id=target.ga4_property_id,
            path_variants=ga4_landing_variants(url),
        )
        if target.ga4_property_id
        else None
    )
    return gsc, ga4


# Координатори (`collect_*`) самі сидять у тому самому пулі й чекають на свої
# підзапити, тож пул мусить мати місце і на них, і на роботу — інакше три
# координатори займуть три слоти й заблокують себе назавжди. Стеля Google тут
# не близько: GSC дозволяє 1200 запитів/хв на property, ми робимо ~25 на
# відкриття сторінки.
_COORDINATOR_SLOTS = 4


def _pool_size(settings_workers: int) -> int:
    return max(8, settings_workers) + _COORDINATOR_SLOTS


# ── хвиля 1: core ────────────────────────────────────────────────────────


def inspect_url_google_core(
    session: Session, *, url: str, window_key: str | None = None
) -> UrlGoogleCoreDTO:
    target, _ = _resolve(session, url)
    window = resolve_window(window_key)
    settings = get_settings()

    with open_google_api_session(settings) as api:
        gsc_client, ga4_client = _clients(api, target, url)
        with ThreadPoolExecutor(
            max_workers=_pool_size(settings.url_inspector_google_workers)
        ) as pool:
            gsc_future = pool.submit(
                _guard,
                "gsc",
                lambda: collect_gsc_core(
                    gsc_client,
                    url=url,
                    window=window,
                    query_cap=settings.url_inspector_query_cap,
                    executor=pool,
                )
                if gsc_client
                else None,
            )
            # `key_event_names` — окремий GET на `metadata` property (375
            # дименсій / 101 метрика, ~1 с). Раніше на нього ЧЕКАЛИ, перш ніж
            # ставити в пул GA4-панель, і саме це робило хвилю 1 вдвічі
            # довшою (1.9 с проти 0.9 с). Тепер він їде паралельно, а до DTO
            # приклеюється після — його роль довідкова: «які конверсії взагалі
            # налаштовані в property», незалежно від того, що сталося на цій
            # сторінці.
            names_future = pool.submit(
                _guard, "ga4", lambda: ga4_client.key_event_names() if ga4_client else []
            )
            organic_future = pool.submit(
                _guard,
                "ga4_cross_check",
                lambda: ga4_client.organic_search_totals(window.start, window.end)
                if ga4_client
                else None,
            )
            ga4_future = pool.submit(
                _guard,
                "ga4",
                lambda: collect_ga4_core(ga4_client, window=window, executor=pool)
                if ga4_client
                else None,
            )
            gsc_dto, gsc_state = gsc_future.result()
            ga4_dto, ga4_state = ga4_future.result()
            organic, organic_state = organic_future.result()
            names, _names_state = names_future.result()

    gsc_dto = gsc_dto or UrlGoogleGscDTO(
        state=gsc_state
        or UrlSourceStateDTO(
            source="gsc", state="not_configured", detail=target.gsc_detail
        )
    )
    ga4_dto = ga4_dto or UrlGoogleGa4DTO(
        state=ga4_state
        or UrlSourceStateDTO(
            source="ga4", state="not_configured", detail=target.ga4_detail
        )
    )
    if names:
        ga4_dto = ga4_dto.model_copy(update={"key_event_names": list(names)})
    return UrlGoogleCoreDTO(
        input_url=url,
        window=_window_dto(window),
        transport=target.transport,
        gsc=gsc_dto,
        ga4=ga4_dto,
        cross_check=_cross_check(gsc_dto, organic, organic_state),
        generated_at=datetime.now(UTC),
    )


def _cross_check(
    gsc: UrlGoogleGscDTO,
    organic: dict[str, Decimal | None] | None,
    organic_state: UrlSourceStateDTO | None,
) -> UrlGoogleCrossCheckDTO:
    """Google проти Google (§0.10).

    Порівнюємо цілі клікі/покази точно, а позицію — з допуском 0.5, бо два
    шляхи усереднюють її на різних рівнях агрегації. Живий прогін дав збіг до
    значущої цифри, тож допуск тут — страховка від округлення, а не від
    очікуваної розбіжності.
    """
    totals = {item.metric: item.value for item in gsc.totals}
    gsc_clicks = totals.get("clicks")
    gsc_impressions = totals.get("impressions")
    gsc_position = totals.get("position")

    if organic is None:
        return UrlGoogleCrossCheckDTO(
            state=organic_state
            or UrlSourceStateDTO(
                source="ga4_cross_check",
                state="no_data",
                detail="GA4 has no Search Console link for this property.",
            ),
            gsc_clicks=gsc_clicks,
            gsc_impressions=gsc_impressions,
            gsc_position=gsc_position,
        )

    ga4_clicks = organic.get("organicGoogleSearchClicks")
    ga4_impressions = organic.get("organicGoogleSearchImpressions")
    ga4_position = organic.get("organicGoogleSearchAveragePosition")

    have_both = (gsc_impressions is not None) and (ga4_impressions is not None)
    matches: bool | None = None
    if have_both:
        matches = gsc_clicks == ga4_clicks and gsc_impressions == ga4_impressions
        if matches and gsc_position is not None and ga4_position is not None:
            matches = abs(gsc_position - ga4_position) <= Decimal("0.5")

    return UrlGoogleCrossCheckDTO(
        state=UrlSourceStateDTO(
            source="ga4_cross_check",
            state="has_data" if have_both else "no_data",
            detail=None
            if have_both
            else "Only one of the two paths returned data for this URL.",
        ),
        gsc_clicks=gsc_clicks,
        gsc_impressions=gsc_impressions,
        gsc_position=gsc_position,
        ga4_clicks=ga4_clicks,
        ga4_impressions=ga4_impressions,
        ga4_position=ga4_position,
        matches=matches,
    )


# ── хвиля 2: breakdowns ──────────────────────────────────────────────────


def inspect_url_google_breakdowns(
    session: Session, *, url: str, window_key: str | None = None
) -> UrlGoogleBreakdownsDTO:
    target, _ = _resolve(session, url)
    window = resolve_window(window_key)
    settings = get_settings()

    with open_google_api_session(settings) as api:
        gsc_client, ga4_client = _clients(api, target, url)
        with ThreadPoolExecutor(
            max_workers=_pool_size(settings.url_inspector_google_workers)
        ) as pool:
            gsc_future = pool.submit(
                _guard,
                "gsc",
                lambda: collect_gsc_breakdowns(
                    gsc_client, url=url, window=window, executor=pool
                )
                if gsc_client
                else [],
            )
            types_future = pool.submit(
                _guard,
                "gsc",
                lambda: collect_search_types(
                    gsc_client, url=url, window=window, executor=pool
                )
                if gsc_client
                else [],
            )
            ga4_future = pool.submit(
                _guard,
                "ga4",
                lambda: collect_ga4_breakdowns(ga4_client, window=window, executor=pool)
                if ga4_client
                else [],
            )
            gsc_rows, gsc_state = gsc_future.result()
            types, _types_state = types_future.result()
            ga4_rows, ga4_state = ga4_future.result()

    breakdowns = list(gsc_rows or []) + list(ga4_rows or [])
    # Збій цілої групи не має зникати зі сторінки: він стає одним рядком із
    # власним станом, інакше оператор побачить «GA4-зрізів немає» замість
    # «GA4-зрізи не завантажились».
    for state, key, label, source in (
        (gsc_state, "gsc_breakdowns", "Search Console breakdowns", "gsc"),
        (ga4_state, "ga4_breakdowns", "GA4 breakdowns", "ga4"),
    ):
        if state is not None:
            breakdowns.append(
                UrlGoogleBreakdownDTO(
                    key=key,
                    label=label,
                    source=source,  # type: ignore[arg-type]
                    dimensions=[],
                    state=state,
                )
            )
    return UrlGoogleBreakdownsDTO(
        input_url=url,
        window=_window_dto(window),
        transport=target.transport,
        breakdowns=breakdowns,
        search_types=list(types or []),
        generated_at=datetime.now(UTC),
    )


# ── хвиля 3: index status і realtime ─────────────────────────────────────


def inspect_url_index_status(session: Session, *, url: str) -> UrlIndexStatusDTO:
    target, _ = _resolve(session, url)
    if not target.gsc_site_url:
        raise GoogleTargetUnavailable(
            target.gsc_detail or "Search Console isn't connected for this client."
        )
    with open_google_api_session() as api:
        inspector = UrlInspectionClient(api, site_url=target.gsc_site_url)
        gsc = GscUrlClient(api, site_url=target.gsc_site_url)
        return collect_index_status(inspector, gsc, url=url)


def inspect_url_realtime(session: Session, *, url: str) -> UrlRealtimeDTO:
    target, _ = _resolve(session, url)
    if not target.ga4_property_id:
        raise GoogleTargetUnavailable(
            target.ga4_detail or "GA4 isn't connected for this client."
        )
    with open_google_api_session() as api:
        client = Ga4UrlClient(
            api,
            property_id=target.ga4_property_id,
            path_variants=ga4_landing_variants(url),
        )
        return collect_realtime(client)

