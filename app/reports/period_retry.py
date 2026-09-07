"""Manual single-period retry (task-47 follow-up).

Automatic retries (`ninja_browser`'s own retry loop on the PDF path,
`runner._fetch_period_metrics_with_retry` on the API path) are bounded — once
they're exhausted, this lets the operator ask for exactly ONE failed period
to be re-collected. Re-running the whole multi-period report would throw
away every period that already succeeded: a `monthly` PDF report costs
~3-5 minutes end to end, and discarding 5 good columns to fix 1 bad one is
not an acceptable trade for a report that already has 5/6 of its data.

This is a single-shot action, not another bounded-retry loop: the operator
already waited through the automatic budget, and a manual retry that itself
silently retried 3 more times would just re-create the same multi-minute
wait it was meant to shortcut.

Periods are reconstructed from the run's OWN stored `periods_json`
(`date_from`/`date_to`/`label`/`is_partial`), never recomputed via
`build_periods(kind, params)` — a `monthly` run's "This Month" is relative to
"today", and a retry days or weeks after the original run must refetch the
SAME calendar range the run actually collected, not whatever "This Month"
means now.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from app.core.config import Settings
from app.db.repositories.clients import ClientRepository
from app.db.repositories.reports import ReportRepository
from app.integrations.google_api import GoogleApiConfigError, open_google_api_session
from app.reports import google_source, ninja_browser
from app.reports.api_source import discover_ga4_fields, discover_gsc_fields, fetch_period_metrics
from app.reports.history import HistoryMonth, ReportHistory
from app.reports.models import PeriodMetrics
from app.reports.ninja_api import ReportingNinjaClient
from app.reports.periods import ReportPeriod
from app.reports.runner import (
    Emit,
    NotConfiguredError,
    ReportNameMissingError,
    aggregate_run_status,
    make_emitter,
    parse_and_check,
    sha256_of,
    write_run_client_pdf,
    write_run_xlsx,
)
from app.reports.table import MetricRowResult, ReportTable, changes_for, safe_pct
from app.schemas.enums import Provider

_TERMINAL_RUN_STATUSES = ("succeeded", "partial", "failed", "cancelled")
# A relogin mid-attempt consumes one attempt slot (`ninja_browser`'s own
# `continue` back into the loop) — 1 would leave no room for that and hit
# the loop's `AssertionError("unreachable")` fallback. 2 is "try once, and
# survive one incidental relogin", not a second real retry.
_PDF_SINGLE_SHOT_SLOTS = 2


def _periods_from_json(periods_json: list[dict[str, Any]]) -> list[ReportPeriod]:
    return [
        ReportPeriod(
            label=row["label"],
            date_from=date.fromisoformat(row["date_from"]),
            date_to=date.fromisoformat(row["date_to"]),
            is_partial=bool(row.get("is_partial", False)),
        )
        for row in periods_json
    ]


def _history_from_dict(data: dict[str, Any] | None) -> ReportHistory | None:
    if not data:
        return None
    months = [
        HistoryMonth(
            month=m["month"], label=m["label"], clicks=m.get("clicks"),
            impressions=m.get("impressions"), ctr=m.get("ctr"),
            organic_conversions=m.get("organic_conversions"),
        )
        for m in data.get("months", [])
    ]
    return ReportHistory(
        months=months, has_gsc=data.get("has_gsc", True), has_ga4=data.get("has_ga4", True)
    )


def _table_from_result(
    kind: str, periods: list[ReportPeriod], result: dict[str, Any]
) -> ReportTable:
    """Rebuild the `ReportTable` object `write_run_xlsx` needs from the
    plain dict `result_json` stores — the only place raw `PeriodMetrics` per
    period lived was inside the ORIGINAL run's local variables, long gone by
    the time a retry happens, so the already-patched dict is the source of
    truth here, not a re-fetch of every period.
    """
    change_labels = list(result.get("change_labels", []))
    pairs_by_label = {triple[0]: (triple[1], triple[2]) for triple in changes_for(kind, periods)}
    change_pairs = [
        pairs_by_label[change_label] for change_label in change_labels
        if change_label in pairs_by_label
    ]
    rows = [
        MetricRowResult(
            key=row["key"], label=row["label"],
            values=list(row["values"]), changes=list(row["changes"]),
        )
        for row in result.get("rows", [])
    ]
    return ReportTable(
        periods=periods,
        change_labels=change_labels,
        change_pairs=change_pairs,
        rows=rows,
        sources=dict(result.get("sources", {})),
        history=_history_from_dict(result.get("history")),
    )


def _refetch_api(
    period: ReportPeriod, rn_integration: Any, gsc_integration: Any, settings: Settings
) -> PeriodMetrics:
    if rn_integration is None or not rn_integration.config_json.get("connection_key"):
        raise NotConfiguredError("Reporting Ninja is not configured for this client")
    api_key = os.environ.get("REPORTING_NINJA_API_KEY", "")
    if not api_key:
        raise NotConfiguredError("REPORTING_NINJA_API_KEY is not set")

    gsc_ref: tuple[str, str] | None = None
    if gsc_integration is not None and gsc_integration.config_json.get("connection_key"):
        gsc_ref = (
            gsc_integration.config_json["connection_key"],
            gsc_integration.config_json["account_id"],
        )

    timeout = settings.http_request_timeout_seconds
    with ReportingNinjaClient(api_key, timeout_seconds=timeout) as ninja:
        ga4_fields = discover_ga4_fields(ninja)
        gsc_fields = discover_gsc_fields(ninja) if gsc_ref else None
        return fetch_period_metrics(
            ninja,
            ga4_connection_key=rn_integration.config_json["connection_key"],
            ga4_account_id=rn_integration.config_json["account_id"],
            ga4_fields=ga4_fields, gsc_ref=gsc_ref, gsc_fields=gsc_fields,
            date_from=period.date_from, date_to=period.date_to,
        )


def _refetch_google(
    period: ReportPeriod, rn_integration: Any, gsc_integration: Any, settings: Settings
) -> PeriodMetrics:
    """The direct-Google-API mirror of `_refetch_api` (task-84 §3.1). The GA4
    property id comes from the `reporting_ninja` integration's `account_id`
    (already in `properties/<id>` form); `connection_key` is not needed.
    """
    if rn_integration is None or not rn_integration.config_json.get("account_id"):
        raise NotConfiguredError("No GA4 property id for this client")
    ga4_property_id = rn_integration.config_json["account_id"]

    gsc_site_url: str | None = None
    if gsc_integration is not None and gsc_integration.config_json.get("account_id"):
        gsc_site_url = gsc_integration.config_json["account_id"]

    try:
        google = open_google_api_session(settings)
    except GoogleApiConfigError as exc:
        raise NotConfiguredError(
            "Google API is not configured (no refresh token in the environment)",
            error_code="not_configured",
        ) from exc
    with google:
        return google_source.fetch_period_metrics(
            google,
            ga4_property_id=ga4_property_id,
            gsc_site_url=gsc_site_url,
            date_from=period.date_from,
            date_to=period.date_to,
        )


def _refetch_pdf(  # noqa: PLR0913 - one call site, every argument is data it needs
    session_scope: Any,
    settings: Settings,
    run_id: str,
    client: Any,
    period: ReportPeriod,
    rn_integration: Any,
    emit: Emit,
) -> PeriodMetrics:
    if rn_integration is None:
        raise NotConfiguredError("Reporting Ninja is not configured for this client")
    report_name = rn_integration.config_json.get("ninja_report_name")
    if not report_name:
        raise ReportNameMissingError()
    email = os.environ.get("REPORTING_NINJA_EMAIL", "")
    password = os.environ.get("REPORTING_NINJA_PASSWORD", "")
    if not email or not password:
        raise NotConfiguredError("REPORTING_NINJA_EMAIL/PASSWORD are not set")

    artifacts_root = settings.artifacts_dir / "reports" / run_id
    downloaded = ninja_browser.download_periods(
        report_name, [period], artifacts_root,
        emit=emit, is_cancelled=lambda: False,
        email=email, password=password, headless=settings.reports_headless,
        retries=_PDF_SINGLE_SHOT_SLOTS,
        screenshots_dir=artifacts_root / "screenshots",
    )
    if not downloaded:
        raise RuntimeError(f"{period.label} still did not download")
    pdf_path = downloaded[0].pdf_path
    metrics = parse_and_check(pdf_path, period.label, emit)
    if metrics is None:
        raise RuntimeError(f"{period.label} PDF parsed 0/8 metrics")

    with session_scope() as session:
        ReportRepository(session).create_file(
            client.id, run_id=run_id, kind="pdf",
            path=str(pdf_path.relative_to(settings.artifacts_dir)),
            sha256=sha256_of(pdf_path), size_bytes=pdf_path.stat().st_size,
            mime="application/pdf",
            period_label=period.label, date_from=period.date_from, date_to=period.date_to,
            is_partial_period=period.is_partial, source_report_name=report_name,
        )
    return metrics


def _patch_result_cells(
    result: dict[str, Any], kind: str, periods: list[ReportPeriod], label: str,
    metrics: PeriodMetrics,
) -> None:
    """Set this period's column in every metric row and recompute only the
    change columns that actually reference it — the other periods' values
    (raw `PeriodMetrics` for them no longer exists after the run finished)
    are left exactly as they were.
    """
    period_index = next(i for i, p in enumerate(periods) if p.label == label)
    for row in result.get("rows", []):
        row["values"][period_index] = getattr(metrics, row["key"], None)

    change_labels: list[str] = result.get("change_labels", [])
    for change_label, newer_label, older_label in changes_for(kind, periods):
        if label not in (newer_label, older_label) or change_label not in change_labels:
            continue
        change_index = change_labels.index(change_label)
        for row in result.get("rows", []):
            by_label = dict(zip((p.label for p in periods), row["values"], strict=True))
            row["changes"][change_index] = safe_pct(
                by_label.get(newer_label), by_label.get(older_label)
            )


def retry_period(session_scope: Any, settings: Settings, run_id: str, label: str) -> str | None:
    """Re-collect exactly one FAILED period of an already-finished run and
    patch the stored table/XLSX/client-PDF in place.

    Returns an error code (`run_not_found`, `period_not_found`,
    `period_not_retryable`, `run_not_finished`) or `None`. `None` covers BOTH
    a successful retry AND a retry that fails again — a second failure is
    not a request error, the period simply stays `failed` with a fresh
    `error` message, same as any other failed period the operator can retry
    again.
    """
    with session_scope() as session:
        repo = ReportRepository(session)
        run = repo.get(run_id)
        if run is None:
            return "run_not_found"
        if run.status not in _TERMINAL_RUN_STATUSES:
            return "run_not_finished"
        client = ClientRepository(session).get(run.client_id)
        if client is None:
            return "run_not_found"
        kind, source = run.kind, run.source
        periods_json = list(run.periods_json or [])
        result = dict(run.result_json or {})
        client_repo = ClientRepository(session)
        rn_integration = client_repo.get_integration(client.id, Provider.reporting_ninja)
        gsc_integration = client_repo.get_integration(client.id, Provider.gsc)

    period_row = next((row for row in periods_json if row.get("label") == label), None)
    if period_row is None:
        return "period_not_found"
    # task-49 §2.5: a `cached`/`done` period is now retryable too — this is
    # what makes "Rebuild" possible at the period level, not just "Retry a
    # failure". `_refetch_pdf` below already re-downloads unconditionally
    # regardless of the period's prior state.
    if period_row.get("state") not in ("failed", "cached", "done"):
        return "period_not_retryable"

    periods = _periods_from_json(periods_json)
    period = next((p for p in periods if p.label == label), None)
    if period is None:  # pragma: no cover - periods_json/period_row always agree
        return "period_not_found"

    emit = make_emitter(session_scope, run_id)
    emit("info", "retry", f"Manual retry requested for {label}", {"period": label})
    # `attempt=1` here resets `started_at` (`update_period`'s own rule: only
    # `attempt <= 1` does) — a manual retry is a fresh, independent action
    # taken possibly hours after the original failure, and the eventual
    # `duration_seconds` should say how long THIS attempt took, not "since
    # the original attempt started" (found live: a period retried 38 minutes
    # after it first failed showed a "38m" duration for a ~1 minute retry).
    with session_scope() as session:
        ReportRepository(session).update_period(
            run_id, label, state="running", attempt=1, max_attempts=1,
        )

    try:
        if source == "api":
            metrics = _refetch_api(period, rn_integration, gsc_integration, settings)
        elif source == "google_api":
            metrics = _refetch_google(period, rn_integration, gsc_integration, settings)
        else:
            metrics = _refetch_pdf(
                session_scope, settings, run_id, client, period, rn_integration, emit
            )
    except Exception as exc:  # noqa: BLE001 - isolated: this period stays failed, run untouched
        with session_scope() as session:
            ReportRepository(session).update_period(
                run_id, label, state="failed", error=str(exc), attempt=1, max_attempts=1,
            )
        emit("error", "retry", f"{label} retry failed: {exc}", {"period": label})
        return None

    _patch_result_cells(result, kind, periods, label, metrics)

    with session_scope() as session:
        repo = ReportRepository(session)
        repo.update_period(run_id, label, state="done", error=None, attempt=1, max_attempts=1)
        fresh = repo.get(run_id)
        rows_now = fresh.periods_json or [] if fresh else []
        done = sum(1 for row in rows_now if row.get("state") in ("done", "cached"))
        failed = sum(1 for row in rows_now if row.get("state") == "failed")
        repo.update_progress(run_id, periods_done=done, periods_failed=failed)
        new_status = aggregate_run_status(len(periods), failed, cancelled=False)
        repo.finish(run_id, status=new_status, result=result)

    emit("info", "retry", f"{label} succeeded on manual retry", {"period": label})

    # Best-effort, exactly like the main run's own tail (task-45/47): the
    # table is already saved even if regenerating either deliverable fails.
    write_run_xlsx(
        session_scope, settings, run_id, client,
        _table_from_result(kind, periods, result), None, emit,
    )
    if kind == "monthly":
        write_run_client_pdf(session_scope, settings, run_id, client, periods, emit)

    return None
