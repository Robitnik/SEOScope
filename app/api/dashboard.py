"""Dashboard endpoints (PLAN §10): the response is self-sufficient for
rendering a client card — current/previous, delta, quality, source and
freshness are all computed here; the frontend never recomputes business
semantics (PLAN §12, CLAUDE.md).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.orm import Session

from app.api import register_router
from app.api.deps import SessionDep
from app.db.models import Client, MetricValue, ProviderSnapshot, SyncJobProvider
from app.db.repositories.analytics_datasets import AnalyticsDatasetRepository
from app.db.repositories.cells import CellRepository
from app.db.repositories.clients import ClientRepository
from app.db.repositories.health import HealthHistoryRepository
from app.db.repositories.metrics import MetricRepository
from app.db.repositories.snapshots import SnapshotRepository
from app.db.repositories.tech_audits import (
    TechAuditReadRepository,
    audit_provenance,
    count_findings,
)
from app.metrics import (
    CATALOG,
    HealthResult,
    Periods,
    assess_quality,
    compute_delta,
    compute_health,
    metrics_for,
    resolve_periods,
    sort_clients,
)
from app.schemas.adapter import DateRange
from app.schemas.api import (
    ClientCardDTO,
    ClientHealthDTO,
    ClientMetricsHistoryDTO,
    DashboardResponse,
    DashboardSortMode,
    ErrorDTO,
    HealthPointDTO,
    MetricCardValue,
    MetricHistoryPointDTO,
    MetricHistorySeriesDTO,
    MetricTrendDTO,
    ProviderSummaryDTO,
    TechAuditCardDTO,
    TechAuditCardState,
)
from app.schemas.enums import (
    METRIC_PROVIDERS,
    CellStatus,
    ComparisonMode,
    PeriodKind,
    PeriodRole,
    Provider,
    Quality,
    SourceMode,
)

router = APIRouter(prefix="/api/v1", tags=["dashboard"])

# Header current/previous range shown alongside the client cards is a single
# display reference for the whole page (e.g. "13-19 July"); every card's
# own metrics still carry their *own* exact current/previous range computed
# in that client's timezone (`MetricCardValue.current_range`), which is what
# actually governs the numbers shown, not this header value.
_HEADER_REFERENCE_TZ = "America/New_York"
_TECH_AUDIT_STALE_AFTER = timedelta(days=30)


def _range_matches_period(row: MetricValue, period: PeriodKind) -> bool:
    if row.period_start is None or row.period_end is None:
        return False
    start, end = row.period_start, row.period_end
    if period == PeriodKind.week:
        return end - start == timedelta(days=6)
    if period == PeriodKind.month:
        return start.day == 1 and end + timedelta(days=1) == date(
            end.year + (1 if end.month == 12 else 0),
            1 if end.month == 12 else end.month + 1,
            1,
        )
    if period == PeriodKind.quarter:
        expected_start_month = ((end.month - 1) // 3) * 3 + 1
        return (
            start.day == 1
            and start.month == expected_start_month
            and start.year == end.year
            and end + timedelta(days=1)
            == date(
                end.year + (1 if end.month == 12 else 0),
                1 if end.month == 12 else end.month + 1,
                1,
            )
        )
    if period == PeriodKind.year:
        return start == date(start.year, 1, 1) and end == date(start.year, 12, 31)
    return False


def _history_for_period(
    rows: list[MetricValue], *, period: PeriodKind, comparison_kind: str, limit: int
) -> list[MetricValue]:
    """Filter period cadence and collapse repeated syncs of the same range."""
    filtered = (
        [row for row in rows if row.period_start is None and row.period_end is None]
        if comparison_kind == "snapshot"
        else [row for row in rows if _range_matches_period(row, period)]
    )
    latest_by_period: dict[tuple[date | None, date | None] | date, MetricValue] = {}
    for row in sorted(filtered, key=lambda item: item.created_at, reverse=True):
        key: tuple[date | None, date | None] | date
        if comparison_kind == "snapshot":
            key = row.created_at.date()
        else:
            key = (row.period_start, row.period_end)
        latest_by_period.setdefault(key, row)
    selected = list(latest_by_period.values())[:limit]
    return sorted(
        selected,
        key=lambda row: (row.period_end or row.created_at.date(), row.created_at),
    )


def _auth_state_from_cell(
    latest_cell: SyncJobProvider | None,
) -> Literal["auth_required", "failed"] | None:
    """task-21: what the *latest attempt* says about session health, distinct
    from whatever quality the last *successful* snapshot happened to have.

    `awaiting_auth` is the lane actively paused on a dead session;
    `failed`/`failed_interrupted` with `error_code == "auth_required"` is a
    pause that already timed out (or an older attempt from before this cell
    ever paused). Anything else returns `None` so callers fall back to their
    own existing logic untouched.
    """
    if latest_cell is None:
        return None
    if latest_cell.status == CellStatus.awaiting_auth:
        return "auth_required"
    if latest_cell.status in (CellStatus.failed, CellStatus.failed_interrupted):
        return "auth_required" if latest_cell.error_code == "auth_required" else "failed"
    return None


def _anchor_datetime(anchor: date | None) -> datetime:
    if anchor is None:
        return datetime.now(UTC)
    return datetime.combine(anchor, time(12, 0), tzinfo=UTC)


def _latest_gsc_aware_success(
    snapshots: SnapshotRepository,
    client_id: str,
    provider: Provider,
    *,
    before: datetime | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
) -> ProviderSnapshot | None:
    """`latest_success`, but for GSC an `api` (live Reporting Ninja) snapshot
    for the requested period always wins over an `auto`/`browser`/`pdf` one
    for that same period, regardless of which was collected more recently.

    GSC's non-`api` transports are lower fidelity (e.g. avg position off a
    browser export is a simple, not impressions-weighted, mean), so letting
    a later-inserted import silently outrank an existing `api` snapshot
    would swap in a less accurate number for no visible reason. Only when no
    `api` snapshot exists for the period do we fall back to another
    transport instead of showing nothing.
    """
    if provider == Provider.gsc:
        preferred = snapshots.latest_success(
            client_id,
            provider,
            before=before,
            period_start=period_start,
            period_end=period_end,
            source_mode=SourceMode.api,
        )
        if preferred is not None:
            return preferred
    return snapshots.latest_success(
        client_id,
        provider,
        before=before,
        period_start=period_start,
        period_end=period_end,
    )


def _period_end_bound(day: date) -> datetime:
    """End-of-day boundary for a period's last date, same convention as the
    health backfill's `as_of` (line ~592): `datetime.combine(day, time(23, 59), tzinfo=UTC)`.
    """
    return datetime.combine(day, time(23, 59), tzinfo=UTC)


def _build_provider_summary(
    session: Session,
    *,
    client_id: str,
    provider: Provider,
    periods: Periods,
    period_kind: PeriodKind,
    now: datetime,
    as_of: datetime | None = None,
) -> ProviderSummaryDTO:
    """`as_of` bounds which snapshots are visible, so this can rebuild what a
    card *would have shown* on a past date (used by the health backfill).
    `None` — the normal request path — means "everything known now".
    """
    clients = ClientRepository(session)
    integration = clients.get_integration(client_id, provider)
    if integration is None or not integration.enabled:
        return ProviderSummaryDTO(provider=provider, state="not_configured")

    cells = CellRepository(session)
    snapshots = SnapshotRepository(session)
    metrics_repo = MetricRepository(session)

    specs = metrics_for(provider)
    has_range_metrics = any(spec.comparison_kind == "range" for spec in specs)
    # `current` intentionally stays unbounded by the period's end for
    # snapshot metrics (Ahrefs/Semrush): it means "the latest known reading",
    # not "reading as of the period's last day" — a snapshot collected
    # moments ago must show up as `current` immediately, even under the
    # default `closed` comparison mode where "current period" is the last
    # *fully elapsed* week/month and so never includes today (verified by
    # `test_dashboard_keeps_last_success_visible_next_to_auth_required_badge`
    # and `test_dashboard_distinguishes_not_configured_failed_and_collected`,
    # both of which assert a just-collected snapshot is visible as current).
    # Only `previous` (below) is bound to its own period's end — that's the
    # PLAN §12 rule ("snapshot metric compared against the last valid
    # snapshot in the corresponding period") and it governs the comparison
    # target, not what counts as "current".
    #
    # `_latest_gsc_aware_success` drops the old blanket GSC-only
    # `source_mode == api` filter (it hid every legitimately matching
    # `auto`/`pdf`/`browser` GSC snapshot, not just wrong-cadence ones — the
    # exact `period_start`/`period_end` match below already guards cadence
    # independently of transport) while keeping `api` as the preferred
    # transport when one exists for the period, so a later-inserted lower-
    # fidelity import never silently outranks it.
    current_snapshot = _latest_gsc_aware_success(
        snapshots,
        client_id,
        provider,
        before=as_of or now,
        period_start=periods.current.start if has_range_metrics else None,
        period_end=periods.current.end if has_range_metrics else None,
    )
    if current_snapshot is None:
        # Keep the last collected range visible as stale when the requested
        # period has not been collected yet, but never substitute a different
        # cadence (the production defect was a monthly import labelled as a
        # weekly result). Exact range above always wins when both exist.
        fallback = _latest_gsc_aware_success(
            snapshots,
            client_id,
            provider,
            before=as_of or now,
        )
        if fallback is not None and (
            not has_range_metrics
            or any(
                row.period_role == PeriodRole.current
                and _range_matches_period(row, period_kind)
                for row in metrics_repo.for_snapshot(fallback.id)
            )
        ):
            current_snapshot = fallback
    latest_cell = cells.latest_for_client_provider(client_id, provider)

    last_attempt_at = None
    last_error_code = None
    if latest_cell is not None:
        last_attempt_at = (
            latest_cell.finished_at or latest_cell.started_at or latest_cell.created_at
        )
        if latest_cell.status in (
            CellStatus.failed,
            CellStatus.failed_interrupted,
            CellStatus.awaiting_auth,
        ):
            last_error_code = latest_cell.error_code
    if last_attempt_at is None:
        last_attempt_at = integration.last_attempt_at

    if current_snapshot is None:
        if latest_cell is not None and latest_cell.status == CellStatus.not_configured:
            # An integration row can exist while its adapter preflight still
            # rejects the configuration (for example, a Semrush domain with
            # no browser profile).  That is a setup state, not a provider
            # outage.  Reporting it as ``unavailable`` made the dashboard
            # show a red collection error after an otherwise healthy batch.
            return ProviderSummaryDTO(
                provider=provider,
                state="not_configured",
                source_mode=integration.source_mode,
                last_success_at=integration.last_success_at,
                last_attempt_at=last_attempt_at,
                last_error_code=latest_cell.error_code,
            )
        auth_state = _auth_state_from_cell(latest_cell)
        state: Literal["auth_required", "failed", "unavailable"] = (
            auth_state if auth_state is not None else "unavailable"
        )
        return ProviderSummaryDTO(
            provider=provider,
            state=state,
            source_mode=integration.source_mode,
            last_success_at=integration.last_success_at,
            last_attempt_at=last_attempt_at,
            last_error_code=last_error_code,
        )

    current_values = {
        (v.metric_key, v.period_role): v for v in metrics_repo.for_snapshot(current_snapshot.id)
    }
    # Bound to the *previous period's* end (PLAN §12), not
    # `current_snapshot.observed_at` — that used to pick whichever collection
    # ran right before this one (often the same batch, minutes apart),
    # comparing near-duplicate collections instead of period vs. period. Only
    # matters for `comparison_kind == "snapshot"` specs below (range specs
    # take their `previous` from `current_values`, the same snapshot's own
    # `PeriodRole.previous` rows) — harmless no-op for range providers.
    previous_snapshot = snapshots.latest_success(
        client_id,
        provider,
        before=min(current_snapshot.observed_at, _period_end_bound(periods.previous.end)),
    )
    previous_values = (
        {(v.metric_key, v.period_role): v for v in metrics_repo.for_snapshot(previous_snapshot.id)}
        if previous_snapshot is not None
        else {}
    )

    metrics: list[MetricCardValue] = []
    for spec in specs:
        current_range: DateRange | None = None
        previous_range: DateRange | None = None
        if spec.comparison_kind == "range":
            current_mv = current_values.get((spec.key, PeriodRole.current))
            previous_mv = current_values.get((spec.key, PeriodRole.previous))
            if current_mv is not None and current_mv.period_start and current_mv.period_end:
                current_range = DateRange(start=current_mv.period_start, end=current_mv.period_end)
            if previous_mv is not None and previous_mv.period_start and previous_mv.period_end:
                previous_range = DateRange(
                    start=previous_mv.period_start, end=previous_mv.period_end
                )
        else:
            current_mv = current_values.get((spec.key, PeriodRole.snapshot))
            previous_mv = previous_values.get((spec.key, PeriodRole.snapshot))

        raw_quality = current_mv.quality if current_mv is not None else Quality.unavailable
        quality = assess_quality(spec, raw_quality, current_snapshot.observed_at, now)
        delta = compute_delta(
            spec,
            current_mv.value_numeric if current_mv is not None else None,
            previous_mv.value_numeric if previous_mv is not None else None,
        )
        is_improvement = None if delta.sentiment == "neutral" else delta.sentiment == "good"

        metrics.append(
            MetricCardValue(
                metric_key=spec.key,
                label=spec.label,
                unit=spec.unit,
                comparison_kind=spec.comparison_kind,
                precision=spec.precision,
                is_derived=current_mv.is_derived if current_mv is not None else False,
                current_value=current_mv.value_numeric if current_mv is not None else None,
                current_text=current_mv.value_text if current_mv is not None else None,
                previous_value=previous_mv.value_numeric if previous_mv is not None else None,
                previous_text=previous_mv.value_text if previous_mv is not None else None,
                delta_abs=delta.delta_abs,
                delta_pct=delta.delta_pct,
                delta_reason=delta.reason,
                better_direction=spec.better_direction,
                is_improvement=is_improvement,
                quality=quality,
                source_mode=integration.source_mode,
                observed_at=current_snapshot.observed_at,
                data_through=current_snapshot.data_through,
                current_range=current_range,
                previous_range=previous_range,
            )
        )

    # The card's own state reflects the *collection attempt's* quality
    # (freshness-assessed), not a worst-of-all-metrics rollup — an optional
    # catalog metric an adapter hasn't populated yet must never drag a
    # genuinely `complete` snapshot down to `unavailable` (PLAN §12).
    provider_quality = (
        assess_quality(specs[0], current_snapshot.quality, current_snapshot.observed_at, now)
        if specs
        else current_snapshot.quality
    )

    # PLAN §12: the last-successful data (metrics/quality above) stays
    # visible even when the *latest attempt* since then hit a dead session —
    # only the outer `state` badge flips to `auth_required`, never the data.
    auth_state = _auth_state_from_cell(latest_cell)
    final_state: str = auth_state if auth_state == "auth_required" else provider_quality.value

    return ProviderSummaryDTO(
        provider=provider,
        state=final_state,  # type: ignore[arg-type]
        metrics=metrics,
        quality=provider_quality,
        source_mode=integration.source_mode,
        last_success_at=current_snapshot.observed_at,
        last_attempt_at=last_attempt_at,
        last_error_code=last_error_code,
        warnings=list(current_snapshot.warnings_json or []),
    )


# How much daily history the card's sparkline gets, and how far back the
# week-over-week arrow compares.
HEALTH_HISTORY_DAYS = 30
HEALTH_TREND_LOOKBACK_DAYS = 7

# Same window, reused for the GBP website-clicks trend block that sits above
# the Reporting Ninja section (mirrors the Ahrefs/health pairing above it).
GBP_CLICKS_HISTORY_DAYS = 30
GBP_CLICKS_TREND_LOOKBACK_DAYS = 7

# Same window again, for the GSC clicks trend block above the Search Console
# section — sourced from `gsc_daily`, the same per-day dataset the client-page
# chart already reads (see `GscAdapter.collect_datasets`).
GSC_CLICKS_HISTORY_DAYS = 30
GSC_CLICKS_TREND_LOOKBACK_DAYS = 7


def _health_with_trend(
    session: Session,
    *,
    client_id: str,
    health: HealthResult,
    today: date,
) -> ClientHealthDTO:
    """Persist today's composite score, then read the trend back out.

    The composite is derived on every dashboard read and was never stored, so
    there was no series to chart and no basis for "up or down since last week".
    Recording it here — where it has just been computed — is what builds that
    history over time; the write is an idempotent upsert keyed on
    `(client, day)`, so repeated dashboard loads refresh today's point rather
    than appending duplicates.

    A `None` score is never recorded: a day with no contributing service has no
    health, and persisting `0` would later render as a crash to zero rather
    than as the absence of a signal (PLAN §12).

    Known limitation: the series only gains a point on days the dashboard is
    actually opened (or collected). A worker-side daily tick would make it
    unconditional — see this task's follow-ups.
    """
    repo = HealthHistoryRepository(session)

    if health.value is not None:
        repo.record(
            client_id,
            today,
            health.value,
            coverage_contributing=health.contributing,
            coverage_configured=health.configured,
        )

    points = repo.history(client_id, since=today - timedelta(days=HEALTH_HISTORY_DAYS), until=today)
    previous_row = repo.value_on_or_before(
        client_id, today - timedelta(days=HEALTH_TREND_LOOKBACK_DAYS)
    )

    # Relative change, so the copy reads "-5% over the week" rather than
    # "-4 points". No basis (never collected before, or a zero baseline) means
    # no trend at all — `None`, never a fabricated 0%.
    previous_value = previous_row.value if previous_row is not None else None
    delta_pct: Decimal | None = None
    if health.value is not None and previous_value is not None and previous_value != 0:
        delta_pct = ((health.value - previous_value) / previous_value * 100).quantize(
            Decimal("0.1")
        )

    return ClientHealthDTO(
        value=health.value,
        coverage_contributing=health.contributing,
        coverage_configured=health.configured,
        value_previous=previous_value,
        delta_pct=delta_pct,
        history=[HealthPointDTO(day=p.day, value=p.value) for p in points],
    )


def _gbp_clicks_trend(session: Session, *, client_id: str, today: date) -> MetricTrendDTO:
    """Week-over-week arrow + daily sparkline for GBP website clicks.

    Unlike the composite health score, this needs no dedicated history table:
    the Reporting Ninja adapter already persists one `gbp_daily` dataset row
    per day per client (client-wide total across locations, see
    `reporting_ninja.py`'s `_GBP_PERFORMANCE_METRICS`), so
    `latest_rows_by_dimension` — newest row per day across every collection —
    is already the exact daily series this block needs, just as `for_period`
    already serves the client page's own GBP chart.

    A day never collected is simply absent from `history`, never a fabricated
    `0` (PLAN §12, same rule `_health_with_trend` follows above).
    """
    rows = AnalyticsDatasetRepository(session).latest_rows_by_dimension(
        client_id, Provider.reporting_ninja, "gbp_daily"
    )
    since = today - timedelta(days=GBP_CLICKS_HISTORY_DAYS)
    points: list[HealthPointDTO] = []
    for dimension_value, row in rows.items():
        try:
            day = date.fromisoformat(dimension_value)
        except ValueError:
            continue
        if day < since or day > today:
            continue
        raw = row.metrics_json.get("website_clicks")
        if raw is None:
            continue
        points.append(HealthPointDTO(day=day, value=Decimal(str(raw))))
    points.sort(key=lambda point: point.day)

    value = points[-1].value if points else None
    previous_value: Decimal | None = None
    if points:
        lookback_day = points[-1].day - timedelta(days=GBP_CLICKS_TREND_LOOKBACK_DAYS)
        candidates = [point.value for point in points if point.day <= lookback_day]
        if candidates:
            previous_value = candidates[-1]

    delta_pct: Decimal | None = None
    if value is not None and previous_value is not None and previous_value != 0:
        delta_pct = ((value - previous_value) / previous_value * 100).quantize(Decimal("0.1"))

    return MetricTrendDTO(
        value=value, value_previous=previous_value, delta_pct=delta_pct, history=points
    )


def _gsc_clicks_trend(session: Session, *, client_id: str, today: date) -> MetricTrendDTO:
    """Week-over-week arrow + daily sparkline for GSC clicks.

    Mirrors `_gbp_clicks_trend` exactly, reading `gsc_daily` (one row per day
    per client, `clicks` in `metrics_json`) instead of `gbp_daily` — the same
    dataset `GscAdapter.collect_datasets` already persists for the
    client-page chart, so this needs no new collection path.
    """
    rows = AnalyticsDatasetRepository(session).latest_rows_by_dimension(
        client_id, Provider.gsc, "gsc_daily"
    )
    since = today - timedelta(days=GSC_CLICKS_HISTORY_DAYS)
    points: list[HealthPointDTO] = []
    for dimension_value, row in rows.items():
        try:
            day = date.fromisoformat(dimension_value)
        except ValueError:
            continue
        if day < since or day > today:
            continue
        raw = row.metrics_json.get("clicks")
        if raw is None:
            continue
        points.append(HealthPointDTO(day=day, value=Decimal(str(raw))))
    points.sort(key=lambda point: point.day)

    value = points[-1].value if points else None
    previous_value: Decimal | None = None
    if points:
        lookback_day = points[-1].day - timedelta(days=GSC_CLICKS_TREND_LOOKBACK_DAYS)
        candidates = [point.value for point in points if point.day <= lookback_day]
        if candidates:
            previous_value = candidates[-1]

    delta_pct: Decimal | None = None
    if value is not None and previous_value is not None and previous_value != 0:
        delta_pct = ((value - previous_value) / previous_value * 100).quantize(Decimal("0.1"))

    return MetricTrendDTO(
        value=value, value_previous=previous_value, delta_pct=delta_pct, history=points
    )


def backfill_health_history(session: Session, *, days: int, now: datetime) -> int:
    """Reconstruct the daily composite-health series from snapshots already in
    the database, so the card's trend works on day one instead of only after a
    month of dashboard visits.

    For each past day this rebuilds the card as it *would have looked then* —
    `as_of` hides snapshots collected later — and records the resulting score.
    That is a real measurement, not interpolation: between two collections the
    best-known health genuinely did not change, so the line correctly sits flat
    and steps on collection days.

    Days whose score is `None` (nothing contributing yet) are skipped, never
    written as `0`. Idempotent — re-running rewrites the same days.
    """
    clients = ClientRepository(session).list_active()
    repo = HealthHistoryRepository(session)
    written = 0

    for client in clients:
        for offset in range(days, -1, -1):
            day = now.date() - timedelta(days=offset)
            as_of = datetime.combine(day, time(23, 59), tzinfo=UTC)
            periods = resolve_periods(
                PeriodKind.week, as_of, tz=client.timezone, mode=ComparisonMode.closed
            )
            summaries = [
                _build_provider_summary(
                    session,
                    client_id=client.id,
                    provider=provider,
                    periods=periods,
                    period_kind=PeriodKind.week,
                    now=as_of,
                    as_of=as_of,
                )
                for provider in METRIC_PROVIDERS
            ]
            health = compute_health(summaries, CATALOG)
            if health.value is None:
                continue
            repo.record(
                client.id,
                day,
                health.value,
                coverage_contributing=health.contributing,
                coverage_configured=health.configured,
            )
            written += 1

    return written


def _build_client_card(
    session: Session,
    client: Client,
    *,
    providers: Sequence[Provider],
    period_kind: PeriodKind,
    anchor_dt: datetime,
    now: datetime,
    tech_audit: TechAuditCardDTO,
) -> ClientCardDTO:
    periods = resolve_periods(
        period_kind, anchor_dt, tz=client.timezone, mode=ComparisonMode.closed
    )
    summaries = [
        _build_provider_summary(
            session,
            client_id=client.id,
            provider=provider,
            periods=periods,
            period_kind=period_kind,
            now=now,
        )
        for provider in providers
    ]

    configured = [s.state for s in summaries if s.state != "not_configured"]
    if not configured:
        overall_status: Literal["ok", "attention", "stale", "failed_all"] = "attention"
    elif all(s in ("failed", "auth_required") for s in configured):
        overall_status = "failed_all"
    elif all(s == "complete" for s in configured):
        overall_status = "ok"
    elif all(s in ("complete", "stale") for s in configured):
        overall_status = "stale"
    else:
        overall_status = "attention"

    successes = [s.last_success_at for s in summaries if s.last_success_at is not None]
    health = compute_health(summaries, CATALOG)
    health_dto = _health_with_trend(session, client_id=client.id, health=health, today=now.date())
    gbp_clicks_trend = _gbp_clicks_trend(session, client_id=client.id, today=now.date())
    gsc_clicks_trend = _gsc_clicks_trend(session, client_id=client.id, today=now.date())

    return ClientCardDTO(
        client_id=client.id,
        name=client.name,
        slug=client.slug,
        domain=client.domain,
        logo_path=f"/api/v1/clients/{client.id}/logo" if client.logo_path else None,
        market=client.market,
        timezone=client.timezone,
        overall_status=overall_status,
        providers=summaries,
        last_synced_at=max(successes) if successes else None,
        health=health_dto,
        gbp_clicks_trend=gbp_clicks_trend,
        gsc_clicks_trend=gsc_clicks_trend,
        tech_audit=tech_audit,
    )


def _tech_audit_cards(
    session: Session, client_ids: Sequence[str], *, now: datetime
) -> dict[str, TechAuditCardDTO]:
    repo = TechAuditReadRepository(session)
    crawls = repo.latest_crawls(client_ids)
    findings = repo.findings_for_crawls(crawls)
    rechecks = repo.rechecks_for_crawls(crawls)
    attempts = repo.latest_crawl_attempts(client_ids)
    out: dict[str, TechAuditCardDTO] = {}

    for client_id in client_ids:
        crawl = crawls.get(client_id)
        attempt = attempts.get(client_id)
        cell, job = attempt if attempt else (None, None)
        active = cell is not None and cell.status in {
            CellStatus.queued,
            CellStatus.running,
            CellStatus.awaiting_auth,
            CellStatus.cancel_requested,
        }
        latest_failed = cell is not None and cell.status in {
            CellStatus.failed,
            CellStatus.failed_interrupted,
        }

        if crawl is None:
            state: TechAuditCardState = (
                "running"
                if active and cell is not None and cell.status != CellStatus.queued
                else "queued"
                if active
                else "failed"
                if latest_failed
                else "no_run"
            )
            out[client_id] = TechAuditCardDTO(
                state=state,
                active_job_id=(job.batch_id or job.id) if active and job else None,
            )
            continue

        prov = audit_provenance(crawl)
        counts = count_findings(findings.get(client_id, []), rechecks.get(client_id, {}))
        quality = crawl.quality
        if active and cell is not None:
            state = "queued" if cell.status == CellStatus.queued else "running"
        elif (
            latest_failed
            and cell is not None
            and cell.finished_at
            and cell.finished_at > crawl.observed_at
        ):
            state = "failed"
        elif now - crawl.observed_at > _TECH_AUDIT_STALE_AFTER:
            state = "stale"
        elif quality in (Quality.partial, Quality.truncated):
            state = "partial" if quality == Quality.partial else "truncated"
        else:
            state = "complete"

        def _optional_int(key: str, values: dict[str, Any] = prov) -> int | None:
            value = values.get(key)
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        out[client_id] = TechAuditCardDTO(
            state=state,
            run_id=crawl.id,
            observed_at=crawl.observed_at,
            pages_crawled=_optional_int("pages_crawled"),
            pages_discovered=_optional_int("pages_discovered"),
            max_pages=_optional_int("max_pages"),
            open_errors=counts.open_errors,
            error_urls=counts.error_urls,
            open_warnings=counts.open_warnings,
            open_notices=counts.open_notices,
            fixed_findings=counts.fixed_findings,
            unreachable_findings=counts.unreachable_findings,
            total_findings=counts.total_findings,
            quality=quality,
            active_job_id=(job.batch_id or job.id) if active and job else None,
        )
    return out


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(
    session: SessionDep,
    period: PeriodKind = PeriodKind.week,
    anchor: date | None = None,
    status: Literal["ok", "attention", "stale", "failed_all"] | None = None,
    provider: Provider | None = None,
    q: str | None = None,
    sort: DashboardSortMode = "quality",
) -> DashboardResponse:
    now = datetime.now(UTC)
    anchor_dt = _anchor_datetime(anchor)
    providers_wanted: Sequence[Provider] = (
        [provider] if provider is not None else METRIC_PROVIDERS
    )

    def _run(s: Session) -> list[ClientCardDTO]:
        clients = ClientRepository(s).list_active()
        if q:
            needle = q.strip().lower()
            clients = [
                c
                for c in clients
                if needle in c.name.lower()
                or needle in c.domain.lower()
                or needle in c.slug.lower()
            ]
        audit_cards = _tech_audit_cards(s, [str(client.id) for client in clients], now=now)
        cards = [
            _build_client_card(
                s,
                c,
                providers=providers_wanted,
                period_kind=period,
                anchor_dt=anchor_dt,
                now=now,
                tech_audit=audit_cards.get(str(c.id), TechAuditCardDTO()),
            )
            for c in clients
        ]
        if status is not None:
            cards = [c for c in cards if c.overall_status == status]
        return sort_clients(cards, sort)

    cards = await session.run_sync(_run)
    header_periods = resolve_periods(period, anchor_dt, tz=_HEADER_REFERENCE_TZ)

    return DashboardResponse(
        period_kind=period,
        comparison_mode=ComparisonMode.closed,
        current_range=header_periods.current,
        previous_range=header_periods.previous,
        clients=cards,
        generated_at=now,
    )


@router.get(
    "/clients/{client_id}/metrics",
    response_model=ClientMetricsHistoryDTO,
    responses={404: {"model": ErrorDTO}},
)
async def get_client_metrics_history(
    client_id: str,
    session: SessionDep,
    period: PeriodKind = PeriodKind.week,
    history: Annotated[int, Query(ge=1, le=104)] = 12,
) -> ClientMetricsHistoryDTO:
    def _run(s: Session) -> list[MetricHistorySeriesDTO] | None:
        client = ClientRepository(s).get(client_id)
        if client is None:
            return None
        metrics_repo = MetricRepository(s)
        series: list[MetricHistorySeriesDTO] = []
        for spec in CATALOG.values():
            rows = _history_for_period(
                metrics_repo.history(
                    client_id,
                    spec.key,
                    n=min(history * 20, 2080),
                ),
                period=period,
                comparison_kind=spec.comparison_kind,
                limit=history,
            )
            series.append(
                MetricHistorySeriesDTO(
                    metric_key=spec.key,
                    label=spec.label,
                    provider=spec.provider,
                    unit=spec.unit,
                    comparison_kind=spec.comparison_kind,
                    points=[
                        MetricHistoryPointDTO(
                            period_start=row.period_start,
                            period_end=row.period_end,
                            observed_at=row.created_at,
                            value_numeric=row.value_numeric,
                            quality=row.quality,
                        )
                        for row in rows
                    ],
                )
            )
        return series

    series = await session.run_sync(_run)
    if series is None:
        raise HTTPException(
            status_code=404,
            detail=ErrorDTO(error_code="client_not_found", message="Client not found").model_dump(),
        )
    return ClientMetricsHistoryDTO(
        client_id=client_id, period_kind=period, series=series, generated_at=datetime.now(UTC)
    )


register_router(router)
