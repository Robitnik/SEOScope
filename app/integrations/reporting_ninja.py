"""`ProviderAdapter` for GA4 via Reporting Ninja (task-09; PLAN §2.2/§3.2/§8.2/§8.3).

Maps `seo_audit.dashboard.reporting_ninja.collect_ga4` (AISeoTools, task-08/09) onto the
`rn_*` catalog keys (task-04). `CollectionContext.integration_config` shape for this
provider — both non-secret external identifiers (PLAN §7):

    {"connection_key": "<ninja connection key>", "account_id": "properties/<id>"}

`REPORTING_NINJA_API_KEY` itself never lives here or in the DB: `ReportingNinjaClient`
reads it from AISeoTools' own `.env` (see AISeoTools `seo_audit/clientreport/api_client.py`).

`seo_audit` is only imported lazily, inside method bodies — never at module import time —
so that merely importing/registering this adapter (which `app/integrations/__init__.py`
does unconditionally) never fails in an environment without the local AISeoTools checkout
(e.g. CI; see compose.yaml/docker/entrypoint.sh). Actually calling `collect()`/`preflight()`
without it present degrades to `not_configured`, never a crash.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from app.integrations.google_api import (
    GOOGLE_API,
    Ga4AdminClient,
    Ga4Client,
    Ga4Row,
    GoogleApiConfigError,
    GoogleApiError,
    GoogleApiPermissionError,
    open_google_api_session,
    resolve_transport,
)
from app.integrations.progress import PhaseTracker
from app.integrations.registry import register_adapter
from app.metrics.catalog import CATALOG
from app.schemas.adapter import (
    CollectionContext,
    CollectionResult,
    DateRange,
    MetricValueResult,
    PreflightResult,
    ProgressEvent,
)
from app.schemas.analytics_datasets import (
    AnalyticsDatasetCollectionResult,
    AnalyticsDatasetValueResult,
)
from app.schemas.enums import MetricUnit, PeriodRole, Provider, Quality

# seo_audit.dashboard.reporting_ninja metric_key -> catalog key (task-04). Source-neutral
# names on the left (AISeoTools has no notion of this app's catalog), canonical on the right.
_METRIC_KEY_MAP: dict[str, str] = {
    "users": "rn_users",
    "organic_users": "rn_organic_users",
    "total_conversions": "rn_total_conversions",
    "organic_conversions": "rn_organic_conversions",
}


_GA4_DATASETS: dict[str, dict[str, Any]] = {
    "ga4_daily": {
        "dimension": "date",
        "metrics": {
            "users": "totalUsers",
            "sessions": "sessions",
            "conversions": "keyEvents",
            "engagement_rate": "engagementRate",
        },
        "sort": "date",
        "limit": 1000,
    },
    "ga4_channels": {
        "dimension": "sessionDefaultChannelGroup",
        "metrics": {
            "users": "totalUsers",
            "sessions": "sessions",
            "conversions": "keyEvents",
            "engagement_rate": "engagementRate",
        },
        "sort": "sessions",
        "limit": 100,
    },
    "ga4_landing_pages": {
        "dimension": "landingPage",
        "metrics": {
            "users": "totalUsers",
            "sessions": "sessions",
            "conversions": "keyEvents",
            "engagement_rate": "engagementRate",
            "views": "screenPageViews",
        },
        "sort": "sessions",
        "limit": 500,
    },
    "ga4_events": {
        "dimension": "eventName",
        "metrics": {
            "events": "eventCount",
            "conversions": "keyEvents",
            "users": "totalUsers",
        },
        "sort": "events",
        "limit": 250,
    },
    "ga4_devices": {
        "dimension": "deviceCategory",
        "metrics": {
            "users": "totalUsers",
            "sessions": "sessions",
            "conversions": "keyEvents",
        },
        "sort": "sessions",
        "limit": 50,
    },
    "ga4_countries": {
        "dimension": "country",
        "metrics": {
            "users": "totalUsers",
            "sessions": "sessions",
            "conversions": "keyEvents",
        },
        "sort": "sessions",
        "limit": 250,
    },
}

# task-44: `ga4_daily_channels` is NOT one more entry in `_GA4_DATASETS`
# above — that dict is built for a single dimension (`[dimension, *metrics]`
# -> one row per dimension value). Organic/Direct Conversions need TWO
# dimensions pivoted into metric keys (one row per DAY, with a column per
# channel), which the generic loop cannot express, so it gets its own branch
# in `_collect_datasets` — same precedent as the GBP branch below it (plan §2.4).
_CHANNEL_DIMENSION = "firstUserDefaultChannelGroup"
_CHANNEL_CONVERSIONS_FIELD = "keyEvents"
# GA4's own channel group labels, mapped onto this app's canonical field
# names — same `keyEvents` field id `ga4_daily` uses for `Total Conversions`,
# so total and its channel parts share one definition (plan §3.2).
_CHANNEL_METRIC_FIELDS: dict[str, str] = {
    "Organic Search": "conversions_organic",
    "Direct": "conversions_direct",
}

# ── task-89 §3.5/§7.1: the GA4 KPI cards (`rn_users`/`rn_organic_users`/…) ──
_KPI_USERS_METRIC = "totalUsers"
_KPI_CONVERSIONS_METRIC = _CHANNEL_CONVERSIONS_FIELD  # "keyEvents"
# task-89 §7.1, found live via the compare-transports gate (HWELL, 30d
# window): the KPI card's `organic_users`/`organic_conversions` do NOT use
# `firstUserDefaultChannelGroup` + exact "Organic Search" — that is task-84's
# `google_source.py` REPORT model, a deliberately different feature. The
# live dashboard card (`seo_audit.clientreport.ga4_fetcher.fetch_ga4_period`,
# its own docstring: "Strategy: one query per period with
# dimension=sessionMedium") uses `sessionMedium` and a SUBSTRING match
# (`"organic" in value.lower()`) — first cut was byte-for-byte wrong on this
# stat by 1.7-3% (2027 vs 2062 users, 33 vs 34 conversions on a 30-day
# window) despite `users`/`total_conversions` (unfiltered) matching exactly.
# Fixed and reverified byte-for-byte: 2027/2027, 33/33.
_KPI_MEDIUM_DIMENSION = "sessionMedium"


def _sum_organic_medium_kpi(rows: list[Any]) -> tuple[Decimal | None, Decimal | None]:
    """`(users, conversions)` summed over rows whose `sessionMedium` value
    CONTAINS "organic" (RN's own dashboard-card rule, not exact-match — a
    real GA4 property's `sessionMedium` values seen live: `"organic"`,
    `"(none)"`, `"referral"`, `"cpc"`; only `"organic"` itself has ever been
    observed containing that substring here, but matching the RN source's
    own `"organic" in v` rule verbatim is what makes this a true drop-in
    replacement). No matching row at all -> `(None, None)`, never a
    fabricated zero (PLAN §12)."""
    matched = [
        row
        for row in rows
        if "organic" in str(row.dimensions.get(_KPI_MEDIUM_DIMENSION, "")).strip().lower()
    ]
    if not matched:
        return None, None
    users = sum(
        (row.metrics.get(_KPI_USERS_METRIC) or Decimal(0) for row in matched), start=Decimal(0)
    )
    conversions = sum(
        (row.metrics.get(_KPI_CONVERSIONS_METRIC) or Decimal(0) for row in matched),
        start=Decimal(0),
    )
    return users, conversions


def query_daily_channels(
    client: Any,
    *,
    connection_key: str,
    account_id: str,
    start: date,
    end: date,
) -> tuple[list[tuple[str, dict[str, Decimal | None]]], bool]:
    """One two-dimension GA4 query -> one row per day of `{conversions_organic,
    conversions_direct}`, shared by the adapter's per-period collection below
    and the `backfill-ga4-channels` CLI (one definition everywhere, plan §1.3).

    A channel absent from a day's response is `None`, never `0` — but a day
    entirely absent from the response gets no row at all (PLAN §12: absence
    is never a fabricated zero, and never a fabricated presence either).
    """
    page = client.query_all(
        "ga4",
        connection_key,
        account_id,
        ["date", _CHANNEL_DIMENSION, _CHANNEL_CONVERSIONS_FIELD],
        start.isoformat(),
        end.isoformat(),
        page_size=10000,
        max_rows=50000,
    )
    by_day: dict[str, dict[str, Decimal | None]] = {}
    seen_days: set[str] = set()
    for row in page.get("rows", []):
        day = _normalize_dimension("date", row.get("date"))
        if not day:
            continue
        seen_days.add(day)
        channel = str(row.get(_CHANNEL_DIMENSION) or "").strip()
        field = _CHANNEL_METRIC_FIELDS.get(channel)
        if field is None:
            continue
        by_day.setdefault(day, {})[field] = _decimal(row.get(_CHANNEL_CONVERSIONS_FIELD))

    rows = [
        (
            day,
            {
                field: by_day.get(day, {}).get(field)
                for field in _CHANNEL_METRIC_FIELDS.values()
            },
        )
        for day in sorted(seen_days)
    ]
    return rows, bool(page.get("truncated"))


# ── task-83: the same two GA4 datasets, straight from the GA4 Data API ───────
# The GA4 field names on the right of `_GA4_DATASETS["ga4_daily"]["metrics"]`
# and `_CHANNEL_*` are exactly what `runReport` takes, so there is one
# definition of "what `ga4_daily` means" shared by both transports.

_GA4_DAILY_FIELDS: dict[str, str] = dict(_GA4_DATASETS["ga4_daily"]["metrics"])
# task-83 §7 (found live in the G2 gate): the GA4 Data API returns
# `engagementRate` as a fraction `0..1`, but Reporting Ninja — and therefore
# every `ga4_daily` row ever stored — scales it to a percentage. `ctr` in
# `gsc_daily` is the mirror case (RN percentage ÷ 100 to a fraction; the API
# already gives a fraction). The rule is transport PARITY: a row collected
# either way must be byte-identical bar `provenance.transport` (§2.3), so the
# API branch matches the stored convention, whatever it is.
_GA4_PERCENT_SCALED: frozenset[str] = frozenset({"engagement_rate"})


def query_ga4_daily_via_google_api(
    ga4: Ga4Client,
    *,
    account_id: str,
    start: date,
    end: date,
) -> list[tuple[str, dict[str, Decimal | None]]]:
    """`[date]` -> `{users, sessions, conversions, engagement_rate}`, oldest
    first. Only `keyEvents` is requested for conversions — `conversions` and
    `keyEvents` together error out (session memory / task-83 §3.3). A day the
    API omits gets no row (PLAN §12)."""
    rows = ga4.run_report(
        account_id,
        start,
        end,
        dimensions=["date"],
        metrics=list(_GA4_DAILY_FIELDS.values()),
    )
    out: list[tuple[str, dict[str, Decimal | None]]] = []
    for row in rows:
        day = row.dimensions.get("date", "")
        if not day:
            continue
        metrics: dict[str, Decimal | None] = {}
        for canonical, ga4_field in _GA4_DAILY_FIELDS.items():
            value = row.metrics.get(ga4_field)
            if value is not None and canonical in _GA4_PERCENT_SCALED:
                value = value * Decimal(100)
            metrics[canonical] = value
        out.append((day, metrics))
    out.sort(key=lambda item: item[0])
    return out


def query_ga4_daily_channels_via_google_api(
    ga4: Ga4Client,
    *,
    account_id: str,
    start: date,
    end: date,
) -> list[tuple[str, dict[str, Decimal | None]]]:
    """`[date, firstUserDefaultChannelGroup]` pivoted to `{conversions_organic,
    conversions_direct}` per day — the same shape and absence rules as
    `query_daily_channels` (a channel missing from a day is `None`, a real `0`
    stays `0`, a day the API omits gets no row). GA4 pagination reads every
    row, so there is no truncation to report."""
    rows = ga4.run_report(
        account_id,
        start,
        end,
        dimensions=["date", _CHANNEL_DIMENSION],
        metrics=[_CHANNEL_CONVERSIONS_FIELD],
    )
    by_day: dict[str, dict[str, Decimal | None]] = {}
    seen_days: set[str] = set()
    for row in rows:
        day = row.dimensions.get("date", "")
        if not day:
            continue
        seen_days.add(day)
        channel = str(row.dimensions.get(_CHANNEL_DIMENSION) or "").strip()
        field = _CHANNEL_METRIC_FIELDS.get(channel)
        if field is None:
            continue
        by_day.setdefault(day, {})[field] = row.metrics.get(_CHANNEL_CONVERSIONS_FIELD)
    return [
        (
            day,
            {
                field: by_day.get(day, {}).get(field)
                for field in _CHANNEL_METRIC_FIELDS.values()
            },
        )
        for day in sorted(seen_days)
    ]


def _derive_ga4_key_events(
    rows: list[Ga4Row], *, definition: dict[str, Any], role: PeriodRole, period: DateRange
) -> list[AnalyticsDatasetValueResult]:
    """`ga4_key_events` (task-89 §4.1) — the SAME `eventName` rows
    `ga4_events` already fetched (`definition` is `_GA4_DATASETS["ga4_events"]`),
    filtered to events with a real (non-`None`, non-zero) conversion count
    and re-sorted by it. This is a different LENS on data already in hand,
    never a second API call — `Total Conversions 100` becomes `Phone 29 /
    purchase 20 / Email 9 / Insurance Form 1`, not a guess."""
    conversions_field = definition["metrics"].get("conversions")
    if conversions_field is None:
        return []
    ranked: list[tuple[str, dict[str, Decimal | None]]] = []
    for row in rows:
        name = str(row.dimensions.get(definition["dimension"]) or "").strip()
        if not name:
            continue
        conversions = row.metrics.get(conversions_field)
        if not conversions:  # None or 0 -> not a key event for this row
            continue
        metrics = {
            canonical: row.metrics.get(ga4_field)
            for canonical, ga4_field in definition["metrics"].items()
        }
        ranked.append((name, metrics))
    ranked.sort(key=lambda item: item[1].get("conversions") or Decimal(0), reverse=True)
    return [
        AnalyticsDatasetValueResult(
            dataset_key="ga4_key_events",
            dimension_key=definition["dimension"],
            dimension_value=name,
            period_role=role,
            period_start=period.start,
            period_end=period.end,
            metrics=metrics,
            quality=Quality.complete,
            rank=rank,
        )
        for rank, (name, metrics) in enumerate(ranked, start=1)
    ]


def _worst_quality(*qualities: Quality) -> Quality:
    order = [Quality.complete, Quality.truncated, Quality.partial, Quality.unavailable]
    seen = [q for q in qualities if q is not None]
    if not seen:
        return Quality.unavailable
    return max(seen, key=order.index)


def _merge_dataset_results(
    base: AnalyticsDatasetCollectionResult,
    trends: AnalyticsDatasetCollectionResult,
) -> AnalyticsDatasetCollectionResult:
    """Fold the google_api trends datasets into the Reporting Ninja result for
    the rest (task-83 §6). The trends datasets have exactly ONE transport each
    (no per-dataset mixing); the snapshot provenance records both sources so it
    never lies about where a row came from."""
    base_datasets = list(base.provenance.get("datasets") or [])
    trends_datasets = list(trends.provenance.get("datasets") or [])
    return AnalyticsDatasetCollectionResult(
        provider=base.provider,
        quality=_worst_quality(base.quality, trends.quality),
        observed_at=min(base.observed_at, trends.observed_at),
        rows=[*base.rows, *trends.rows],
        warnings=[*base.warnings, *trends.warnings],
        error_code=trends.error_code or base.error_code,
        provenance={
            "transport": "reporting_ninja",
            "trends_ga4_transport": GOOGLE_API,
            "integration": "ga4",
            "datasets": [*base_datasets, *trends_datasets],
            "reporting_ninja": dict(base.provenance),
            "google_api": dict(trends.provenance),
        },
    )


_GBP_PERFORMANCE_METRICS = {
    "impressions": "business_impressions_total_calc",
    "interactions": "business_profile_interactions_calc",
    "calls": "call_clicks",
    "directions": "business_direction_requests",
    "website_clicks": "website_clicks",
    "bookings": "business_bookings",
}

# Стадії, які `seo_audit.dashboard.reporting_ninja.collect_ga4` емітить під час
# KPI-фази, у порядку виконання. Пакет пінований і `percent` не шле — позиція
# стадії в цьому списку і є вимірюваним кроком фази (task-32).
_KPI_STAGES = ("discover", "fetch_current", "fetch_previous")

_GBP_DATASET_KEYS = (
    "gbp_daily",
    "gbp_locations",
    "gbp_search_terms",
    "gbp_reviews",
)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _dataset_label(dataset_key: str) -> str:
    """`ga4_landing_pages` → `landing pages` — для UI-повідомлення прогресу."""
    return dataset_key.removeprefix("ga4_").removeprefix("gbp_").replace("_", " ")


def _normalize_dimension(field: str, value: Any) -> str:
    text = str(value or "").strip()
    if field == "date" and len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


class ReportingNinjaAdapter:
    provider = Provider.reporting_ninja

    def preflight(self, context: CollectionContext) -> PreflightResult:
        cfg = context.integration_config
        if not cfg.get("connection_key") or not cfg.get("account_id"):
            return PreflightResult(
                provider=self.provider,
                ok=False,
                reason_code="not_configured",
                message="Reporting Ninja: connection_key/account_id not set for this client.",
            )
        if not os.environ.get("REPORTING_NINJA_API_KEY"):
            return PreflightResult(
                provider=self.provider,
                ok=False,
                reason_code="auth_required",
                message="REPORTING_NINJA_API_KEY not configured.",
            )
        try:
            from seo_audit.clientreport.api_client import ReportingNinjaClient
        except ImportError:
            return PreflightResult(
                provider=self.provider,
                ok=False,
                reason_code="not_configured",
                message="AISeoTools not installed in this environment.",
            )
        ReportingNinjaClient().close()
        return PreflightResult(provider=self.provider, ok=True)

    def collect(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
        is_cancel_requested: Callable[[], bool],
    ) -> CollectionResult:
        try:
            return self._collect(context, emit)
        except BaseException as exc:  # noqa: BLE001 - never crash the cell (PLAN §9.2)
            return CollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=datetime.now(UTC),
                metrics=[],
                warnings=[f"unexpected error: {type(exc).__name__}"],
                error_code="adapter_error",
            )

    def _collect(
        self, context: CollectionContext, emit: Callable[[ProgressEvent], None]
    ) -> CollectionResult:
        # task-89 §3.5: the GA4 KPI cards (`rn_*`) get the same transport fork
        # as the trends datasets (task-83) — same `resolve_transport()` call,
        # same "never a silent fallback" rule.
        if resolve_transport(context.integration_config) == GOOGLE_API:
            return self._collect_google_api(context, emit)
        return self._collect_reporting_ninja(context, emit)

    def _collect_google_api(
        self, context: CollectionContext, emit: Callable[[ProgressEvent], None]
    ) -> CollectionResult:
        """GA4 KPI totals via the GA4 Data API (task-89 §3.5), reusing the
        SAME attribution model task-84's `app.reports.google_source` already
        established for report generation — totals from a dimension-less
        query, organic from an EXACT match on `firstUserDefaultChannelGroup`
        (never a substring, or "Organic Social"/"Organic Shopping" leak in).
        Re-implemented locally rather than imported: `app.reports.*` depends
        on `app.integrations.*`, never the other way — importing across that
        boundary would invert the dependency direction."""
        cfg = context.integration_config
        property_id = str(cfg.get("account_id") or "").strip()
        observed_at = datetime.now(UTC)

        if not property_id:
            return CollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=observed_at,
                metrics=[],
                warnings=["GA4: account_id (properties/<id>) not configured for this client."],
                error_code="not_configured",
            )
        try:
            session = open_google_api_session()
        except GoogleApiConfigError as exc:
            return CollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=observed_at,
                metrics=[],
                warnings=[f"google_api transport selected but not configured: {exc}"],
                error_code="not_configured",
            )

        warnings: list[str] = []
        metrics: list[MetricValueResult] = []
        completed = 0
        tracker = PhaseTracker(self.provider, emit, "kpi")
        tracker.begin("connect", message="Connecting to GA4 (Google API)")
        with session:
            ga4 = Ga4Client(session)
            for step, (role, date_range) in enumerate(
                (
                    (PeriodRole.current, context.current),
                    (PeriodRole.previous, context.previous),
                ),
                start=1,
            ):
                tracker.step(
                    "fetch_" + role.value,
                    completed=step, total=2,
                    message=f"GA4 KPI snapshot · {role.value}",
                )
                try:
                    totals = ga4.run_report(
                        property_id, date_range.start, date_range.end,
                        dimensions=[], metrics=[_KPI_USERS_METRIC, _KPI_CONVERSIONS_METRIC],
                    )
                    medium_rows = ga4.run_report(
                        property_id, date_range.start, date_range.end,
                        dimensions=[_KPI_MEDIUM_DIMENSION],
                        metrics=[_KPI_USERS_METRIC, _KPI_CONVERSIONS_METRIC],
                    )
                except GoogleApiPermissionError as exc:
                    return CollectionResult(
                        provider=self.provider,
                        quality=Quality.unavailable,
                        observed_at=observed_at,
                        metrics=metrics,
                        warnings=[*warnings, f"GA4 access denied via google_api: {exc}"],
                        error_code="not_configured",
                    )
                except GoogleApiError as exc:
                    warnings.append(
                        f"GA4 KPI via google_api failed for {role.value}: {type(exc).__name__}"
                    )
                    for catalog_key in _METRIC_KEY_MAP.values():
                        metrics.append(_unavailable_metric(catalog_key, role, date_range))
                    continue

                total_row = totals[0] if totals else None
                users = total_row.metrics.get(_KPI_USERS_METRIC) if total_row else None
                conversions = total_row.metrics.get(_KPI_CONVERSIONS_METRIC) if total_row else None
                organic_users, organic_conversions = _sum_organic_medium_kpi(medium_rows)
                field_values = {
                    "users": users,
                    "organic_users": organic_users,
                    "total_conversions": conversions,
                    "organic_conversions": organic_conversions,
                }
                row_quality = Quality.complete if total_row is not None else Quality.unavailable
                for field_key, catalog_key in _METRIC_KEY_MAP.items():
                    value = field_values.get(field_key)
                    metrics.append(
                        MetricValueResult(
                            metric_key=catalog_key,
                            period_role=role,
                            period_start=date_range.start,
                            period_end=date_range.end,
                            value_numeric=value,
                            unit=CATALOG[catalog_key].unit,
                            quality=row_quality if value is not None else Quality.unavailable,
                        )
                    )
                completed += 1

        tracker.done("kpi_complete", message="GA4 KPI snapshot ready via Google API")
        if completed == 0:
            quality = Quality.unavailable
        elif completed < 2:
            quality = Quality.partial
        else:
            quality = Quality.complete
        return CollectionResult(
            provider=self.provider,
            quality=quality,
            observed_at=observed_at,
            metrics=metrics,
            warnings=warnings,
            provenance={"transport": GOOGLE_API, "integration": "ga4", "property": property_id},
        )

    def _collect_reporting_ninja(
        self, context: CollectionContext, emit: Callable[[ProgressEvent], None]
    ) -> CollectionResult:
        from seo_audit.clientreport.api_client import ReportingNinjaClient
        from seo_audit.dashboard.models import DateRange as SeoDateRange
        from seo_audit.dashboard.reporting_ninja import collect_ga4

        cfg = context.integration_config
        current = SeoDateRange(start=context.current.start, end=context.current.end)
        previous = SeoDateRange(start=context.previous.start, end=context.previous.end)

        tracker = PhaseTracker(self.provider, emit, "kpi")
        tracker.begin("connect", message="Connecting to Reporting Ninja")
        with ReportingNinjaClient() as client:
            result = collect_ga4(
                client,
                ga4_ref={
                    "connection_key": cfg["connection_key"],
                    "account_id": cfg["account_id"],
                },
                current=current,
                previous=previous,
                on_event=tracker.bridge(_KPI_STAGES),
            )
        tracker.done("kpi_complete", message="GA4 KPI snapshot ready")

        metrics = _map_metrics(result, context.current, context.previous)
        return CollectionResult(
            provider=self.provider,
            quality=Quality(result.quality.value),
            observed_at=result.observed_at,
            metrics=metrics,
            warnings=list(result.warnings),
            error_code=result.error_code,
            provenance=dict(result.provenance),
        )

    def collect_datasets(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
    ) -> AnalyticsDatasetCollectionResult:
        """Collect GA4 daily and breakdown datasets via Reporting Ninja."""
        try:
            return self._collect_datasets(context, emit)
        except BaseException as exc:  # noqa: BLE001 - optional datasets never crash KPI sync
            return AnalyticsDatasetCollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=datetime.now(UTC),
                warnings=[f"unexpected dataset error: {type(exc).__name__}"],
                error_code="dataset_adapter_error",
            )

    def _collect_datasets(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
    ) -> AnalyticsDatasetCollectionResult:
        # task-83 (originally just `ga4_daily`/`ga4_daily_channels` for
        # `#/trends`), generalized by task-89 §3.5 to all six `_GA4_DATASETS`
        # entries: when this client's transport is `google_api`, every GA4
        # dataset the client page reads is collected straight from the GA4
        # Data API (free) instead of Reporting Ninja (paid proxy). GBP stays
        # Reporting Ninja-only always — the Google token this project holds
        # has no Business Profile scope (task-89 §0.15). The transport lives
        # in `config_json["transport"]`; absent = today's behaviour, so
        # nothing changes for anyone until a deliberate switch.
        trends_via_google = resolve_transport(context.integration_config) == GOOGLE_API
        rn_result = self._collect_datasets_reporting_ninja(
            context, emit, skip_trends_ga4=trends_via_google
        )
        if not trends_via_google:
            return rn_result
        try:
            trends_result = self._collect_datasets_google_api_trends(context, emit)
        except BaseException as exc:  # noqa: BLE001 - a google_api failure must not lose the RN datasets
            trends_result = AnalyticsDatasetCollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=datetime.now(UTC),
                warnings=[f"google_api trends transport error: {type(exc).__name__}"],
                error_code="dataset_adapter_error",
            )
        return _merge_dataset_results(rn_result, trends_result)

    def _collect_datasets_google_api_trends(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
    ) -> AnalyticsDatasetCollectionResult:
        """ALL six `_GA4_DATASETS` entries (`ga4_daily`, `ga4_channels`,
        `ga4_landing_pages`, `ga4_events`, `ga4_devices`, `ga4_countries`)
        PLUS the two-dimension `ga4_daily_channels` pivot, via the GA4 Data
        API (task-83 §5 for `ga4_daily`/`ga4_daily_channels`; generalized to
        the rest by task-89 §3.5, so the client page's breakdown table no
        longer needs Reporting Ninja at all when this transport is selected).

        Same row shape, `dataset_key`, `dimension_key` and metric names as
        the Reporting Ninja path for every one of them — the ONLY difference
        is `provenance.transport` (task-83 §2.3). Sort/cap/truncation mirrors
        `_collect_datasets_reporting_ninja`'s `append_dataset` exactly (fetch
        everything the API has, sort locally by `definition["sort"]`, cap at
        `definition["limit"]`, `Quality.truncated` if anything was cut) — no
        per-page `limit` passed to `Ga4Client.run_report`, so a client with
        more landing pages than the cap still gets every row counted for the
        truncation decision, same as RN's `max_rows=50000` client-side cap.
        Never a silent fallback to RN: a missing token or denied property is
        `not_configured` with a warning (§6.1.3)."""
        cfg = context.integration_config
        account_id = str(cfg.get("account_id") or "").strip()
        observed_at = datetime.now(UTC)

        if not account_id:
            return AnalyticsDatasetCollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=observed_at,
                warnings=["GA4: account_id (properties/<id>) not configured for this client."],
                error_code="not_configured",
            )
        try:
            session = open_google_api_session()
        except GoogleApiConfigError as exc:
            return AnalyticsDatasetCollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=observed_at,
                warnings=[f"google_api transport selected but not configured: {exc}"],
                error_code="not_configured",
            )

        warnings: list[str] = []
        values: list[AnalyticsDatasetValueResult] = []
        completed = 0
        key_events_datasets: list[str] = []
        # +1 dataset slot per period for the `ga4_daily_channels` pivot,
        # same accounting the RN path uses for `channel_steps` (plan §2.4).
        total_steps = (len(_GA4_DATASETS) + 1) * 2
        step = 0
        tracker = PhaseTracker(self.provider, emit, "datasets")
        tracker.begin(
            "datasets", message=f"Collecting {total_steps} GA4 datasets via Google API"
        )

        def append_api_dataset(
            *,
            dataset_key: str,
            dimension_key: str,
            role: PeriodRole,
            period: DateRange,
            rows: list[Ga4Row],
            definition: dict[str, Any],
        ) -> None:
            nonlocal completed
            normalized: list[tuple[str, dict[str, Decimal | None]]] = []
            for row in rows:
                dimension_value = str(row.dimensions.get(dimension_key) or "").strip()
                if not dimension_value:
                    continue
                metrics: dict[str, Decimal | None] = {}
                for canonical, ga4_field in definition["metrics"].items():
                    value = row.metrics.get(ga4_field)
                    if value is not None and canonical in _GA4_PERCENT_SCALED:
                        value = value * Decimal(100)
                    metrics[canonical] = value
                normalized.append((dimension_value, metrics))
            sort_key = definition["sort"]
            if sort_key == "date":
                normalized.sort(key=lambda item: item[0])
            else:
                normalized.sort(
                    key=lambda item: item[1].get(sort_key) or Decimal(0), reverse=True
                )
            capped = normalized[: definition["limit"]]
            truncated = len(normalized) > len(capped)
            row_quality = Quality.truncated if truncated else Quality.complete
            values.extend(
                AnalyticsDatasetValueResult(
                    dataset_key=dataset_key,
                    dimension_key=dimension_key,
                    dimension_value=dimension_value,
                    period_role=role,
                    period_start=period.start,
                    period_end=period.end,
                    metrics=metrics,
                    quality=row_quality,
                    rank=rank,
                )
                for rank, (dimension_value, metrics) in enumerate(capped, start=1)
            )
            completed += 1

        with session:
            ga4 = Ga4Client(session)
            for role, period in (
                (PeriodRole.current, context.current),
                (PeriodRole.previous, context.previous),
            ):
                for dataset_key, definition in _GA4_DATASETS.items():
                    step += 1
                    dimension = definition["dimension"]
                    tracker.step(
                        f"fetch_{role.value}_{dataset_key}",
                        completed=step,
                        total=total_steps,
                        message=(
                            f"GA4 {_dataset_label(dataset_key)} · {role.value} · "
                            f"{period.start} → {period.end}"
                        ),
                    )
                    try:
                        rows = ga4.run_report(
                            account_id, period.start, period.end,
                            dimensions=[dimension], metrics=list(definition["metrics"].values()),
                        )
                    except GoogleApiPermissionError as exc:
                        return AnalyticsDatasetCollectionResult(
                            provider=self.provider,
                            quality=Quality.unavailable,
                            observed_at=observed_at,
                            rows=values,
                            warnings=[*warnings, f"GA4 access denied via google_api: {exc}"],
                            error_code="not_configured",
                        )
                    except GoogleApiError as exc:
                        warnings.append(
                            f"{dataset_key} via google_api failed for {role.value}: "
                            f"{type(exc).__name__}"
                        )
                        continue
                    append_api_dataset(
                        dataset_key=dataset_key, dimension_key=dimension,
                        role=role, period=period, rows=rows, definition=definition,
                    )
                    # task-89 §4.1: `ga4_key_events` — the SAME `eventName`
                    # rows `ga4_events` just fetched, filtered to events with
                    # a real conversion count and re-sorted by it. Zero extra
                    # API calls: a different lens on data already in hand.
                    if dataset_key == "ga4_events":
                        key_event_values = _derive_ga4_key_events(
                            rows, definition=definition, role=role, period=period
                        )
                        if key_event_values:
                            values.extend(key_event_values)
                            key_events_datasets.append("ga4_key_events")

                # task-44: Organic/Direct Conversions — the same two-dimension
                # pivot the RN path uses, its own branch (plan §2.4/§3.2).
                step += 1
                tracker.step(
                    f"fetch_{role.value}_ga4_daily_channels",
                    completed=step, total=total_steps,
                    message=f"GA4 daily channels · {role.value} · {period.start} → {period.end}",
                )
                try:
                    channels = query_ga4_daily_channels_via_google_api(
                        ga4, account_id=account_id, start=period.start, end=period.end
                    )
                except GoogleApiPermissionError as exc:
                    return AnalyticsDatasetCollectionResult(
                        provider=self.provider,
                        quality=Quality.unavailable,
                        observed_at=observed_at,
                        rows=values,
                        warnings=[*warnings, f"GA4 access denied via google_api: {exc}"],
                        error_code="not_configured",
                    )
                except GoogleApiError as exc:
                    warnings.append(
                        f"ga4_daily_channels via google_api failed for {role.value}: "
                        f"{type(exc).__name__}"
                    )
                    continue
                values.extend(
                    AnalyticsDatasetValueResult(
                        dataset_key="ga4_daily_channels",
                        dimension_key="date",
                        dimension_value=day,
                        period_role=role,
                        period_start=period.start,
                        period_end=period.end,
                        metrics=metrics,
                        quality=Quality.complete,
                        rank=rank,
                    )
                    for rank, (day, metrics) in enumerate(channels, start=1)
                )
                completed += 1

            # task-89 §4.2: GA4 Admin API — property configuration, once per
            # collection (not per role/dataset: this is not period data).
            # A failure here is a warning, never a reason to lose the GA4
            # datasets already collected above.
            try:
                property_info = Ga4AdminClient(session).property_info(account_id)
            except GoogleApiError as exc:
                warnings.append(
                    f"GA4 Admin property info via google_api failed: {type(exc).__name__}"
                )
            else:
                if property_info.time_zone and property_info.time_zone != context.timezone:
                    warnings.append(
                        f"GA4 property timezone ({property_info.time_zone}) differs from "
                        f"this client's configured timezone ({context.timezone}) — daily "
                        "buckets in GA4 rows are cut in the PROPERTY's timezone, not ours."
                    )
        tracker.done("datasets_complete", message="GA4 datasets collected via Google API")

        if completed == 0:
            quality = Quality.unavailable
        elif completed < total_steps:
            quality = Quality.partial
        else:
            quality = Quality.complete
        return AnalyticsDatasetCollectionResult(
            provider=self.provider,
            quality=quality,
            observed_at=observed_at,
            rows=values,
            warnings=warnings,
            provenance={
                "transport": GOOGLE_API,
                "integration": "ga4",
                "datasets": [
                    *_GA4_DATASETS.keys(), "ga4_daily_channels",
                    *dict.fromkeys(key_events_datasets),
                ],
                "property": account_id,
            },
        )

    def _collect_datasets_reporting_ninja(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
        *,
        skip_trends_ga4: bool = False,
    ) -> AnalyticsDatasetCollectionResult:
        from seo_audit.clientreport.api_client import (
            APIError,
            RateLimitError,
            ReportingNinjaClient,
        )

        cfg = context.integration_config
        observed_at = datetime.now(UTC)
        warnings: list[str] = []
        values: list[AnalyticsDatasetValueResult] = []
        completed = 0
        truncated_any = False

        # task-83 (originally just `ga4_daily`/`ga4_daily_channels`),
        # generalized by task-89 §3.5 to ALL six `_GA4_DATASETS` entries: when
        # this client's GA4 datasets are served by the Data API,
        # `_collect_datasets_google_api_trends` now collects every one of
        # them, so none is collected here too — skipped so a day/row never
        # gets two versions (one per transport) in the same snapshot with an
        # identical `created_at` (§0.5 tie-break).
        active_ga4_datasets = {} if skip_trends_ga4 else dict(_GA4_DATASETS)
        collect_channels = not skip_trends_ga4
        channel_steps = 1 if collect_channels else 0

        # Скільки мережевих проходів попереду — відомо ДО циклу, тому відсоток
        # цієї фази вимірюваний, а не вигаданий (task-32): кожен GA4-датасет на
        # два періоди, плюс два періоди GBP і один прохід по відгуках.
        gbp_planned = bool(cfg.get("gbp_locations"))
        # task-44: `+1` dataset (`ga4_daily_channels`), `*2` for current+previous
        # like every other GA4 dataset here (plan §2.4).
        dataset_steps = (
            (len(active_ga4_datasets) + channel_steps) * 2 + (3 if gbp_planned else 0)
        )
        step = 0
        tracker = PhaseTracker(self.provider, emit, "datasets")
        tracker.begin(
            "datasets",
            message=f"Collecting {dataset_steps} GA4 datasets",
            details={"planned_steps": dataset_steps},
        )

        def append_dataset(
            *,
            dataset_key: str,
            dimension_key: str,
            role: PeriodRole,
            period: DateRange,
            rows: list[tuple[str, dict[str, Decimal | None]]],
            sort_key: str,
            limit: int,
        ) -> None:
            nonlocal completed, truncated_any
            if sort_key == "date":
                rows.sort(key=lambda item: item[0])
            else:
                rows.sort(
                    key=lambda item: item[1].get(sort_key) or Decimal(0), reverse=True
                )
            capped = rows[:limit]
            truncated = len(rows) > len(capped)
            truncated_any = truncated_any or truncated
            row_quality = Quality.truncated if truncated else Quality.complete
            values.extend(
                AnalyticsDatasetValueResult(
                    dataset_key=dataset_key,
                    dimension_key=dimension_key,
                    dimension_value=dimension_value,
                    period_role=role,
                    period_start=period.start,
                    period_end=period.end,
                    metrics=metrics,
                    quality=row_quality,
                    rank=rank,
                )
                for rank, (dimension_value, metrics) in enumerate(capped, start=1)
            )
            completed += 1

        with ReportingNinjaClient() as client:
            available = {field["field_id"] for field in client.list_fields("ga4")}
            for role, period in (
                (PeriodRole.current, context.current),
                (PeriodRole.previous, context.previous),
            ):
                for dataset_key, definition in active_ga4_datasets.items():
                    step += 1
                    stage = f"fetch_{role.value}_{dataset_key}"
                    label = _dataset_label(dataset_key)
                    dimension = definition["dimension"]
                    metric_fields = {
                        key: field
                        for key, field in definition["metrics"].items()
                        if field in available
                    }
                    if dimension not in available or not metric_fields:
                        warnings.append(f"{dataset_key}: required GA4 fields unavailable")
                        # Крок усе одно зараховано: інакше шкала фази ніколи не
                        # дійшла б до кінця на акаунті з неповним набором полів.
                        tracker.step(
                            stage,
                            completed=step,
                            total=dataset_steps,
                            level="warning",
                            message=f"{label} ({role.value}): fields unavailable",
                        )
                        continue
                    tracker.step(
                        stage,
                        completed=step,
                        total=dataset_steps,
                        message=(
                            f"GA4 {label} · {role.value} · "
                            f"{period.start} → {period.end}"
                        ),
                    )
                    try:
                        page = client.query_all(
                            "ga4",
                            cfg["connection_key"],
                            cfg["account_id"],
                            [dimension, *metric_fields.values()],
                            period.start.isoformat(),
                            period.end.isoformat(),
                            page_size=10000,
                            max_rows=50000,
                        )
                    except (APIError, RateLimitError, ValueError) as exc:
                        warnings.append(f"{dataset_key} {role.value} failed: {exc}")
                        continue

                    normalized = []
                    for row in page.get("rows", []):
                        dimension_value = _normalize_dimension(dimension, row.get(dimension))
                        if not dimension_value:
                            continue
                        normalized.append(
                            (
                                dimension_value,
                                {
                                    key: _decimal(row.get(field))
                                    for key, field in metric_fields.items()
                                },
                            )
                        )
                    append_dataset(
                        dataset_key=dataset_key,
                        dimension_key=dimension,
                        role=role,
                        period=period,
                        rows=normalized,
                        sort_key=definition["sort"],
                        limit=definition["limit"],
                    )
                    if page.get("truncated"):
                        truncated_any = True

                # task-44: Organic/Direct Conversions — one two-dimension
                # query per period, its own branch (plan §2.4/§3.2).
                # task-83: skipped here when `#/trends` reads this dataset from
                # the GA4 Data API instead (last statement in the loop body, so
                # `continue` is just an early wrap).
                if not collect_channels:
                    continue
                step += 1
                stage = f"fetch_{role.value}_ga4_daily_channels"
                if (
                    _CHANNEL_DIMENSION not in available
                    or _CHANNEL_CONVERSIONS_FIELD not in available
                ):
                    warnings.append("ga4_daily_channels: required GA4 fields unavailable")
                    tracker.step(
                        stage,
                        completed=step,
                        total=dataset_steps,
                        level="warning",
                        message=f"GA4 daily channels ({role.value}): fields unavailable",
                    )
                else:
                    tracker.step(
                        stage,
                        completed=step,
                        total=dataset_steps,
                        message=(
                            f"GA4 daily channels · {role.value} · "
                            f"{period.start} → {period.end}"
                        ),
                    )
                    try:
                        normalized, truncated = query_daily_channels(
                            client,
                            connection_key=cfg["connection_key"],
                            account_id=cfg["account_id"],
                            start=period.start,
                            end=period.end,
                        )
                    except (APIError, RateLimitError, ValueError) as exc:
                        warnings.append(f"ga4_daily_channels {role.value} failed: {exc}")
                    else:
                        append_dataset(
                            dataset_key="ga4_daily_channels",
                            dimension_key="date",
                            role=role,
                            period=period,
                            rows=normalized,
                            sort_key="date",
                            limit=1000,
                        )
                        if truncated:
                            truncated_any = True

            gbp_locations = cfg.get("gbp_locations") or []
            if gbp_locations:
                for role, period in (
                    (PeriodRole.current, context.current),
                    (PeriodRole.previous, context.previous),
                ):
                    step += 1
                    tracker.step(
                        f"fetch_{role.value}_gbp",
                        completed=step,
                        total=dataset_steps,
                        message=(
                            f"Business Profile · {role.value} · "
                            f"{len(gbp_locations)} locations"
                        ),
                    )
                    daily: dict[str, dict[str, Decimal]] = {}
                    location_rows: list[tuple[str, dict[str, Decimal | None]]] = []
                    search_terms: dict[str, Decimal] = {}
                    performance_success = 0
                    search_success = 0
                    for location in gbp_locations:
                        name = str(location.get("name") or location["account_id"])
                        try:
                            performance = client.query_all(
                                "google_business_profile",
                                location["connection_key"],
                                location["account_id"],
                                ["day", *_GBP_PERFORMANCE_METRICS.values()],
                                period.start.isoformat(),
                                period.end.isoformat(),
                                data_view="performance",
                                page_size=1000,
                                max_rows=5000,
                            )
                            performance_success += 1
                            totals = {key: Decimal(0) for key in _GBP_PERFORMANCE_METRICS}
                            for row in performance.get("rows", []):
                                day = _normalize_dimension("date", row.get("day"))
                                if not day:
                                    continue
                                day_metrics = daily.setdefault(
                                    day,
                                    {key: Decimal(0) for key in _GBP_PERFORMANCE_METRICS},
                                )
                                for key, field in _GBP_PERFORMANCE_METRICS.items():
                                    amount = _decimal(row.get(field)) or Decimal(0)
                                    day_metrics[key] += amount
                                    totals[key] += amount
                            location_rows.append(
                                (name, cast("dict[str, Decimal | None]", totals))
                            )
                        except (APIError, RateLimitError, ValueError, KeyError) as exc:
                            warnings.append(f"GBP performance {name} {role.value} failed: {exc}")

                        try:
                            terms_page = client.query_all(
                                "google_business_profile",
                                location["connection_key"],
                                location["account_id"],
                                ["search_terms", "search_impressions"],
                                period.start.isoformat(),
                                period.end.isoformat(),
                                data_view="search_terms",
                                page_size=10000,
                                max_rows=50000,
                            )
                            search_success += 1
                            for row in terms_page.get("rows", []):
                                term = str(row.get("search_terms") or "").strip()
                                if term:
                                    search_terms[term] = search_terms.get(
                                        term, Decimal(0)
                                    ) + (_decimal(row.get("search_impressions")) or Decimal(0))
                        except (APIError, RateLimitError, ValueError, KeyError) as exc:
                            warnings.append(f"GBP search terms {name} {role.value} failed: {exc}")

                    if performance_success:
                        append_dataset(
                            dataset_key="gbp_daily",
                            dimension_key="day",
                            role=role,
                            period=period,
                            rows=[
                                (day, cast("dict[str, Decimal | None]", metrics))
                                for day, metrics in daily.items()
                            ],
                            sort_key="date",
                            limit=1000,
                        )
                        append_dataset(
                            dataset_key="gbp_locations",
                            dimension_key="location",
                            role=role,
                            period=period,
                            rows=location_rows,
                            sort_key="interactions",
                            limit=100,
                        )
                    if search_success:
                        append_dataset(
                            dataset_key="gbp_search_terms",
                            dimension_key="search_terms",
                            role=role,
                            period=period,
                            rows=[
                                (
                                    term,
                                    cast(
                                        "dict[str, Decimal | None]",
                                        {"impressions": amount},
                                    ),
                                )
                                for term, amount in search_terms.items()
                            ],
                            sort_key="impressions",
                            limit=500,
                        )

                reviews: list[tuple[str, dict[str, Decimal | None]]] = []
                step += 1
                tracker.step(
                    "fetch_gbp_reviews",
                    completed=step,
                    total=dataset_steps,
                    message=(
                        "Business Profile lifetime reviews · "
                        f"{len(gbp_locations)} locations"
                    ),
                )
                for location in gbp_locations:
                    name = str(location.get("name") or location["account_id"])
                    try:
                        page = client.query_page(
                            "google_business_profile",
                            location["connection_key"],
                            location["account_id"],
                            ["reviews_lifetime", "average_rating_lifetime"],
                            context.current.start.isoformat(),
                            context.current.end.isoformat(),
                            data_view="reviews_lifetime",
                            limit=10,
                        )
                        row = (page.get("rows") or [{}])[0]
                        reviews.append(
                            (
                                name,
                                {
                                    "reviews": _decimal(row.get("reviews_lifetime")),
                                    "average_rating": _decimal(
                                        row.get("average_rating_lifetime")
                                    ),
                                },
                            )
                        )
                    except (APIError, RateLimitError, ValueError, KeyError) as exc:
                        warnings.append(f"GBP reviews {name} failed: {exc}")
                if reviews:
                    append_dataset(
                        dataset_key="gbp_reviews",
                        dimension_key="location",
                        role=PeriodRole.current,
                        period=context.current,
                        rows=reviews,
                        sort_key="reviews",
                        limit=100,
                    )

        tracker.done("datasets_complete", message=f"Collected {completed} GA4 datasets")

        expected = (len(active_ga4_datasets) + channel_steps) * 2
        if cfg.get("gbp_locations"):
            expected += 7
        if expected == 0:
            # task-89 §3.5: `skip_trends_ga4=True` (transport=google_api) can
            # leave this call with NOTHING assigned to it at all — every GA4
            # dataset is collected by `_collect_datasets_google_api_trends`
            # instead, and there is no GBP to fetch either. `completed == 0`
            # here means "correctly did nothing", not "tried and failed", so
            # it must never read as `unavailable` — `_merge_dataset_results`'
            # worst-of-both-sides quality would otherwise drag a fully
            # successful google_api collection down to `unavailable` (found
            # live while generalizing the skip from `ga4_daily` alone to all
            # six datasets).
            quality = Quality.complete
        elif completed == 0:
            quality = Quality.unavailable
        elif completed < expected:
            quality = Quality.partial
        elif truncated_any:
            quality = Quality.truncated
        else:
            quality = Quality.complete
        return AnalyticsDatasetCollectionResult(
            provider=self.provider,
            quality=quality,
            observed_at=observed_at,
            rows=values,
            warnings=warnings,
            provenance={
                "transport": "reporting_ninja",
                "integration": "ga4",
                "datasets": [
                    *active_ga4_datasets,
                    *(["ga4_daily_channels"] if collect_channels else []),
                    *_GBP_DATASET_KEYS,
                ],
            },
        )


def _map_metrics(result: Any, current: DateRange, previous: DateRange) -> list[MetricValueResult]:
    by_key = {(v.metric_key, v.period_role.value): v for v in result.values}
    out: list[MetricValueResult] = []
    for field_key, catalog_key in _METRIC_KEY_MAP.items():
        for role, date_range in ((PeriodRole.current, current), (PeriodRole.previous, previous)):
            found = by_key.get((field_key, role.value))
            if found is not None:
                out.append(
                    MetricValueResult(
                        metric_key=catalog_key,
                        period_role=role,
                        period_start=found.period_start,
                        period_end=found.period_end,
                        value_numeric=found.value_numeric,
                        unit=MetricUnit(found.unit.value),
                        quality=Quality(result.quality.value),
                    )
                )
            else:
                out.append(_unavailable_metric(catalog_key, role, date_range))
    return out


def _unavailable_metric(
    catalog_key: str, role: PeriodRole, date_range: DateRange
) -> MetricValueResult:
    return MetricValueResult(
        metric_key=catalog_key,
        period_role=role,
        period_start=date_range.start,
        period_end=date_range.end,
        value_numeric=None,
        unit=CATALOG[catalog_key].unit,
        quality=Quality.unavailable,
    )


def _bridge_emit(emit: Callable[[ProgressEvent], None]) -> Callable[[Any], None]:
    """Translate a `seo_audit.dashboard.events.ProgressEvent` into this app's own
    `ProgressEvent` and forward it. Best-effort in both directions: a broken callback or an
    unexpected event shape must never interrupt collection."""

    def _on_event(evt: Any) -> None:
        try:
            emit(
                ProgressEvent(
                    provider=Provider(evt.provider.value),
                    stage=evt.stage,
                    completed=evt.completed,
                    total=evt.total,
                    percent=evt.percent,
                    level=evt.level,
                    message=evt.message,
                    details=dict(evt.details),
                )
            )
        except Exception:  # noqa: BLE001 - progress is diagnostic, never load-bearing
            pass

    return _on_event


register_adapter(ReportingNinjaAdapter())
