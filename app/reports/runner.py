"""Execute one `ReportRun` end to end (task-42 §2.1-§2.5, §3.4-§3.7).

Network/browser work happens OUTSIDE any DB transaction (PLAN §5.1/§9.1);
progress is persisted in short, separate `session_scope()` blocks after each
meaningful step, mirroring `app/jobs/events.py::make_emitter` and
`app/monitor/runner.py`. One period's failure never aborts the run (§3.7.5)
— it is recorded and the run continues; only "not configured" (no
integration/report name/credentials) and an exhausted PDF re-login
(§3.7.1) abort early, since every remaining period would fail identically.

task-45 adds three things here:

- **Progress is data** (§2.1). `report_runs.periods_json` is written in full
  (every period `waiting`, dates known) before the first period is touched,
  then patched `running` -> `done`/`cached`/`failed` on BOTH paths. The UI no
  longer reconstructs state from log message text, so rewording a log line
  cannot break the progress display.
- **One XLSX path** (§2.3). `build_xlsx` moved out of `_run_pdf` into the
  shared tail, so an API run also produces a downloadable workbook.
- **The monthly history block** (§2.4), two extra Reporting Ninja queries,
  best-effort: it never fails a run that already has a table.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.db.repositories.clients import ClientRepository
from app.db.repositories.reports import ReportRepository
from app.integrations.google_api import (
    GoogleApiConfigError,
    GoogleApiPermissionError,
    open_google_api_session,
)
from app.reports import google_source, ninja_browser
from app.reports.api_source import (
    discover_ga4_fields,
    discover_gsc_fields,
    fetch_period_metrics,
)
from app.reports.client_pdf import client_pdf_filename, render_client_pdf
from app.reports.history import ReportHistory, fetch_history
from app.reports.models import PeriodMetrics
from app.reports.ninja_api import ReportingNinjaClient
from app.reports.pdf_parse import looks_like_a_report_pdf, metric_coverage, parse_pdf
from app.reports.periods import (
    ReportPeriod,
    build_comparator_periods,
    build_custom_months_periods,
    build_monthly_periods,
    build_yearly_periods,
)
from app.reports.table import ReportTable, build_table
from app.reports.verify import CrossCheckResult, cross_check
from app.reports.xlsx import build_xlsx
from app.schemas.enums import Provider

Emit = Any  # Callable[[str, str, str, dict[str, Any] | None], None] — see events.py's own alias


class NotConfiguredError(RuntimeError):
    def __init__(self, message: str, *, error_code: str = "not_configured") -> None:
        self.error_code = error_code
        super().__init__(message)


class ReportNameMissingError(NotConfiguredError):
    def __init__(self, client_name: str | None = None) -> None:
        # task-49 §1.3/§2.3: the old message named a REST endpoint the
        # operator has no way to call. This one names the client and the
        # two actual recovery actions available from the wizard.
        who = f" for {client_name}" if client_name else ""
        super().__init__(
            f"Reporting Ninja report not set{who}. Open the report wizard "
            "and use Connect next to this client, or exclude it from the batch.",
            error_code="report_name_missing",
        )


def build_periods(
    kind: str,
    params: dict[str, Any],
    *,
    today: date | None = None,
    max_months: int = 24,
    max_comparator_years: int = 4,
) -> list[ReportPeriod]:
    if kind == "monthly":
        return build_monthly_periods(today=today)
    if kind == "yearly":
        return build_yearly_periods(params["years"], params.get("span", "full"), today=today)
    if kind == "custom_months":
        return build_custom_months_periods(params["months"], today=today, max_months=max_months)
    if kind == "comparator":
        return build_comparator_periods(
            params["months"], today=today, max_years=max_comparator_years
        )
    raise ValueError(f"Unknown report kind: {kind!r}")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_emitter(session_scope: Any, run_id: str) -> Emit:
    def _emit(level: str, stage: str, message: str, details: dict[str, Any] | None = None) -> None:
        try:
            with session_scope() as session:
                ReportRepository(session).append_event(
                    run_id, level, stage=stage, message=message, details=details or {}
                )
        except Exception:  # noqa: BLE001 - a broken event pipe must never break the run
            pass

    return _emit


def period_rows(periods: list[ReportPeriod]) -> list[dict[str, Any]]:
    """The initial `periods_json` payload: every period `waiting`, with the
    dates it will actually be collected for (task-45 §2.1). Also used by
    `app/api/reports.py` at enqueue time, so a `queued` run's period list is
    complete and labelled before the worker has claimed it.
    """
    return [
        {
            "label": period.label,
            "date_from": period.date_from.isoformat(),
            "date_to": period.date_to.isoformat(),
            "is_partial": period.is_partial,
            "state": "waiting",
            "started_at": None,
            "finished_at": None,
            "duration_seconds": None,
            "from_cache": False,
            "error": None,
            # Real values land on the first "running" transition (task-47) —
            # a `waiting` period hasn't attempted anything yet, so `1/1` here
            # is a neutral placeholder, not a claim about the retry budget.
            "attempt": 1,
            "max_attempts": 1,
        }
        for period in periods
    ]


def _make_period_marker(session_scope: Any, run_id: str) -> Any:
    """`mark(label, state, ...)` — one short transaction per transition, the
    same shape `update_progress` already uses (PLAN §9.2). Bookkeeping must
    never break a run, so a failed write is swallowed exactly like a failed
    event append.
    """

    def _mark(
        label: str,
        state: str,
        *,
        from_cache: bool | None = None,
        error: str | None = None,
        attempt: int | None = None,
        max_attempts: int | None = None,
    ) -> None:
        try:
            with session_scope() as session:
                ReportRepository(session).update_period(
                    run_id, label, state=state, from_cache=from_cache, error=error,
                    attempt=attempt, max_attempts=max_attempts,
                )
        except Exception:  # noqa: BLE001 - progress bookkeeping is never fatal
            pass

    return _mark


def _skip_pending_periods(session_scope: Any, run_id: str) -> None:
    """A cancelled or aborted run leaves its untouched periods `skipped`, not
    forever `Waiting` — the difference between "we stopped" and "still going".
    """
    try:
        with session_scope() as session:
            ReportRepository(session).mark_pending_periods(run_id, state="skipped")
    except Exception:  # noqa: BLE001 - see `_make_period_marker`
        pass


def _make_is_cancelled(session_scope: Any, run_id: str) -> Any:
    def _is_cancelled() -> bool:
        with session_scope() as session:
            run = ReportRepository(session).get(run_id)
            return bool(run and run.cancel_requested)

    return _is_cancelled


def aggregate_run_status(period_count: int, periods_failed: int, *, cancelled: bool) -> str:
    if cancelled:
        return "cancelled"
    if periods_failed == 0:
        return "succeeded"
    if period_count > 0 and periods_failed >= period_count:
        return "failed"
    return "partial"


def parse_and_check(path: Path, label: str, emit: Emit) -> PeriodMetrics | None:
    metrics = parse_pdf(path)
    found, missing = metric_coverage(metrics)
    if not found:
        # Live regression (2026-08-19): "0 found" used to always mean the
        # download was garbage/mismatched and fail the period outright — but
        # it's also exactly what a genuinely empty period looks like once
        # `parse_pdf` nulls every metric (task-48 §2.3's no-data
        # normalization). Real case: EFFYS's Month YOY/Quarterly YOY (before
        # the GA4 property existed) were being discarded as parse failures,
        # even though the PDF was a perfectly good, complete report saying
        # "nothing happened here" — `looks_like_a_report_pdf` tells the two
        # apart by checking the report's own page chrome, independent of
        # which metrics parsed.
        if looks_like_a_report_pdf(path):
            emit(
                "info", "parse", f"{label}: no data in this period (0/8 metrics, valid report)",
                {"period": label},
            )
            return metrics
        emit(
            "error", "parse", f"{label}: parsed 0/8 metrics — treating as failed",
            {"period": label},
        )
        return None
    if missing:
        emit(
            "warning", "parse",
            f"{label}: parsed {len(found)}/8 metrics — missing: {', '.join(missing)}",
            {"period": label, "missing": missing},
        )
    return metrics


def run(session_scope: Any, settings: Settings, run_id: str) -> None:
    """Load and execute the run identified by `run_id`. Always leaves the run
    in a terminal status — never raises past this function, so the caller
    (the scheduler) can rely on every claimed run eventually finishing.
    """
    with session_scope() as session:
        report_run = ReportRepository(session).get(run_id)
        if report_run is None:
            return
        client = ClientRepository(session).get(report_run.client_id)
        kind, source, params = report_run.kind, report_run.source, dict(report_run.params_json)

    emit = make_emitter(session_scope, run_id)
    is_cancelled = _make_is_cancelled(session_scope, run_id)

    periods: list[ReportPeriod] = []
    try:
        if client is None:
            raise NotConfiguredError("Client not found", error_code="client_not_found")
        periods = build_periods(
            kind, params,
            max_months=settings.reports_max_months,
            max_comparator_years=settings.reports_max_comparator_years,
        )
        # Before any network call: the full, date-labelled period list. The UI
        # shows every period as `Waiting` from the first second instead of
        # waiting for the first event to invent a row (task-45 §1.1).
        with session_scope() as session:
            ReportRepository(session).set_periods(run_id, period_rows(periods))
        mark_period = _make_period_marker(session_scope, run_id)

        cc_result: CrossCheckResult | None = None
        if source == "api":
            table = _run_api(
                session_scope, settings, run_id, client, kind, periods, emit, is_cancelled,
                mark_period,
            )
        elif source == "google_api":
            # An explicit branch, NOT an extension of the `pdf` fallthrough
            # (task-84 §0.2): a `pdf` `else` would have sent every unknown
            # source silently down the browser path.
            table = _run_google(
                session_scope, settings, run_id, client, kind, periods, emit, is_cancelled,
                mark_period,
            )
        elif source == "pdf":
            table, cc_result = _run_pdf(
                session_scope, settings, run_id, client, kind, periods, emit, is_cancelled,
                mark_period, force_refresh=bool(params.get("force_refresh")),
            )
        else:
            raise ValueError(f"unknown report source: {source!r}")
        cross_check_status = cc_result.status if cc_result is not None else None
    except NotConfiguredError as exc:
        _skip_pending_periods(session_scope, run_id)
        _finish(
            session_scope, run_id, status="failed",
            error_code=exc.error_code, error_message=str(exc),
        )
        return
    except ninja_browser.AuthenticationFailedError as exc:
        _skip_pending_periods(session_scope, run_id)
        _finish(
            session_scope, run_id, status="failed",
            error_code="auth_failed", error_message=str(exc),
        )
        return
    except Exception as exc:  # noqa: BLE001 - a run must always reach a terminal status
        emit("error", "run", f"Unexpected failure: {exc}", {})
        _skip_pending_periods(session_scope, run_id)
        _finish(
            session_scope, run_id, status="failed",
            error_code="internal_error", error_message=str(exc)[:900],
        )
        return

    _skip_pending_periods(session_scope, run_id)
    with session_scope() as session:
        fresh = ReportRepository(session).get(run_id)
        periods_done = fresh.periods_done if fresh else 0
        periods_failed = fresh.periods_failed if fresh else len(periods)
        fresh_periods_json = fresh.periods_json if fresh else None
    status = aggregate_run_status(len(periods), periods_failed, cancelled=is_cancelled())
    result = table.to_dict()
    result["periods_done"] = periods_done
    if cc_result is not None:
        result["cross_check"] = cc_result.to_dict()
    # task-49 §2.6: which periods this run actually reused from cache, and
    # the oldest of their download dates — the incident this closes had two
    # "successful" clients whose whole 6/6 came from yesterday's PDFs with
    # nothing in the matrix to tell an operator that.
    cached_rows = [
        row for row in (fresh_periods_json or [])
        if row.get("state") == "cached" and row.get("cached_at")
    ]
    if cached_rows:
        result["cached_periods"] = [row["label"] for row in cached_rows]
        result["oldest_cached_at"] = min(row["cached_at"] for row in cached_rows)
    if periods_done:
        # The workbook is built from the in-memory `table`, so it does not
        # care whether the result has been stored yet.
        write_run_xlsx(session_scope, settings, run_id, client, table, cc_result, emit)
    _finish(
        session_scope, run_id, status=status,
        result=result, cross_check_status=cross_check_status,
    )
    # Client PDF (task-47 §5): automatic on every successful/partial
    # `monthly` run — the type the tile/YOY layout is built for (§1.5).
    # A `yearly`/`custom_months`/`comparator` run has no "This Month"
    # column to key it to, so it is skipped here; the operator can still
    # request one explicitly via `POST .../client-pdf` later.
    #
    # STRICTLY AFTER `_finish` (live 2026-08-19): `render_client_pdf` drives
    # Chromium against `GET /runs/{id}/document`, and that endpoint answers
    # `422 run_not_finished` until `_finish` has stored `result_json` — so
    # rendering here before it could only ever fail. It did, silently, on
    # every automatic run: each of a 9-client batch's children logged
    # "Failed to find element matching selector .report-doc__page" as a
    # best-effort warning, no `client_pdf` row was ever written, and the
    # batch's "Download all PDFs" archive came out empty. The manual
    # `POST .../client-pdf` always worked precisely because it runs after
    # the run is finished.
    if periods_done and kind == "monthly":
        write_run_client_pdf(session_scope, settings, run_id, client, periods, emit)


def write_run_xlsx(  # noqa: PLR0913 - one call site, every argument is data it needs
    session_scope: Any,
    settings: Settings,
    run_id: str,
    client: Any,
    table: ReportTable,
    cross_check_result: CrossCheckResult | None,
    emit: Emit,
) -> None:
    """The workbook, for BOTH sources (task-45 §2.3). task-42 built it only
    inside `_run_pdf`, which is why every API run finished with an empty Files
    section. A failure here never fails the run — the table is already stored
    and the operator can still read it in the UI.
    """
    try:
        artifacts_root = settings.artifacts_dir / "reports" / run_id
        xlsx_path = build_xlsx(
            client.name, table, artifacts_root, cross_check=cross_check_result
        )
        with session_scope() as session:
            ReportRepository(session).create_file(
                client.id, run_id=run_id, kind="xlsx",
                path=str(xlsx_path.relative_to(settings.artifacts_dir)),
                sha256=sha256_of(xlsx_path), size_bytes=xlsx_path.stat().st_size,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
    except Exception as exc:  # noqa: BLE001 - the workbook is an output, not the result
        emit("warning", "xlsx", f"XLSX could not be written: {exc}", {})


def write_run_client_pdf(
    session_scope: Any,
    settings: Settings,
    run_id: str,
    client: Any,
    periods: list[ReportPeriod],
    emit: Emit,
) -> None:
    """The client-facing PDF (task-47 §2.1/§3.3): Chromium renders
    `GET /runs/{run_id}/document` — the same markup the operator's iframe
    preview shows — to a vector PDF. Best-effort, exactly like `write_run_xlsx`:
    a run that already has a table and a workbook is never failed by a PDF
    render problem (a Chromium crash, a slow document load).
    """
    this_month = next((p for p in periods if p.label == "This Month"), None)
    month_stamp = (this_month.date_from if this_month else date.today()).strftime("%Y-%m")
    try:
        artifacts_root = settings.artifacts_dir / "reports" / run_id
        filename = client_pdf_filename(client.name, month_stamp)
        pdf_path = artifacts_root / filename
        render_client_pdf(
            settings=settings, run_id=run_id, out_path=pdf_path, headless=settings.reports_headless
        )
        with session_scope() as session:
            ReportRepository(session).create_file(
                client.id, run_id=run_id, kind="client_pdf",
                path=str(pdf_path.relative_to(settings.artifacts_dir)),
                sha256=sha256_of(pdf_path), size_bytes=pdf_path.stat().st_size,
                mime="application/pdf",
            )
        emit("info", "client_pdf", f"Client PDF written: {filename}", {})
    except Exception as exc:  # noqa: BLE001 - the client PDF is an output, not the result
        emit("warning", "client_pdf", f"Client PDF could not be rendered: {exc}", {})


def _finish(
    session_scope: Any,
    run_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    cross_check_status: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    with session_scope() as session:
        ReportRepository(session).finish(
            run_id, status=status, result=result, cross_check_status=cross_check_status,
            error_code=error_code, error_message=error_message,
        )


def _fetch_period_metrics_with_retry(  # noqa: PLR0913 - bounded retry, see docstring
    ninja: ReportingNinjaClient,
    *,
    ga4_connection_key: str,
    ga4_account_id: str,
    ga4_fields: dict[str, str],
    gsc_ref: tuple[str, str] | None,
    gsc_fields: dict[str, str] | None,
    date_from: date,
    date_to: date,
    label: str,
    retries: int,
    mark_period: Any,
    emit: Emit,
    is_cancelled: Any,
    sleep: Any = time.sleep,
) -> PeriodMetrics:
    """Up to `retries` attempts (task-47) — the API path used to fail a
    period on its very first exception, unlike the PDF path's own retry loop
    (`ninja_browser._fetch_period_with_retry`), which this mirrors: each
    attempt re-enters `mark_period(..., "running", attempt=N)` so the UI
    shows "retry 2/3" live, and a mid-run cooperative cancel check between
    attempts (§3.7.6 on the PDF side).
    """
    for attempt in range(1, retries + 1):
        if is_cancelled():
            raise RuntimeError(f"Cancelled while fetching {label!r}")
        mark_period(label, "running", attempt=attempt, max_attempts=retries)
        try:
            return fetch_period_metrics(
                ninja,
                ga4_connection_key=ga4_connection_key,
                ga4_account_id=ga4_account_id,
                ga4_fields=ga4_fields,
                gsc_ref=gsc_ref,
                gsc_fields=gsc_fields,
                date_from=date_from,
                date_to=date_to,
            )
        except Exception as exc:  # noqa: BLE001 - bounded retry, caller isolates the final failure
            if attempt == retries:
                raise
            emit(
                "warning", "fetch",
                f"{label}: attempt {attempt}/{retries} failed ({exc}) — retrying",
                {"period": label, "attempt": attempt, "max_attempts": retries},
            )
            sleep(3.0 * attempt)
    raise AssertionError("unreachable")  # pragma: no cover - loop always returns or raises


def _run_api(  # noqa: PLR0913 - orchestrator, see module docstring
    session_scope: Any,
    settings: Settings,
    run_id: str,
    client: Any,
    kind: str,
    periods: list[ReportPeriod],
    emit: Emit,
    is_cancelled: Any,
    mark_period: Any,
) -> ReportTable:
    with session_scope() as session:
        client_repo = ClientRepository(session)
        rn_integration = client_repo.get_integration(client.id, Provider.reporting_ninja)
        gsc_integration = client_repo.get_integration(client.id, Provider.gsc)

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
    else:
        emit("warning", "preflight", "GSC not configured — clicks/impressions unavailable", {})

    metrics_by_label: dict[str, PeriodMetrics] = {}
    periods_done = periods_failed = 0
    timeout = settings.http_request_timeout_seconds
    with ReportingNinjaClient(api_key, timeout_seconds=timeout) as ninja:
        ga4_fields = discover_ga4_fields(ninja)
        gsc_fields = discover_gsc_fields(ninja) if gsc_ref else None

        for period in periods:
            if is_cancelled():
                emit("info", "run", "Cancelled", {})
                break
            # BEFORE the work, not after it (task-45 §1.1): the API path used
            # to emit only "<label> done", so a period in flight was
            # indistinguishable from one not started. `mark_period` itself is
            # now called once per attempt inside the retry helper below
            # (task-47), including the first one.
            try:
                metrics = _fetch_period_metrics_with_retry(
                    ninja,
                    ga4_connection_key=rn_integration.config_json["connection_key"],
                    ga4_account_id=rn_integration.config_json["account_id"],
                    ga4_fields=ga4_fields,
                    gsc_ref=gsc_ref,
                    gsc_fields=gsc_fields,
                    date_from=period.date_from,
                    date_to=period.date_to,
                    label=period.label,
                    retries=settings.reports_api_period_retries,
                    mark_period=mark_period,
                    emit=emit,
                    is_cancelled=is_cancelled,
                )
                metrics_by_label[period.label] = metrics
                periods_done += 1
                # `attempt`/`max_attempts` are left as-is: the last "running"
                # transition inside `_fetch_period_metrics_with_retry` already
                # stamped the attempt that actually succeeded — no need (and
                # no easy way, without threading a return value) to repeat it
                # here, and `update_period` only overwrites when passed.
                mark_period(period.label, "done")
                emit("info", "fetch", f"{period.label} done", {"period": period.label})
            except Exception as exc:  # noqa: BLE001 - isolate this period, keep going
                periods_failed += 1
                mark_period(period.label, "failed", error=str(exc))
                emit("error", "fetch", f"{period.label} failed: {exc}", {"period": period.label})
            with session_scope() as session:
                ReportRepository(session).update_progress(
                    run_id, periods_done=periods_done, periods_failed=periods_failed
                )

        history = (
            None
            if is_cancelled()
            else _history_with_client(
                ninja, settings, rn_integration, ga4_fields, gsc_ref, gsc_fields, emit
            )
        )
        emit("info", "run", f"{ninja.request_count} Reporting Ninja requests", {})

    return replace(build_table(kind, periods, metrics_by_label), history=history)


# GSC Search Analytics API returns at most ~16 months of history (task-84
# §0.7). A period older than this cannot be filled by the direct transport at
# all — the column becomes `—`, and the operator is told why rather than left
# to guess (task-84 §4.3).
_GSC_HISTORY_LIMIT_DAYS = 16 * 31


def _fetch_google_period_with_retry(  # noqa: PLR0913 - bounded retry, mirrors `_fetch_period_metrics_with_retry`
    session: Any,
    *,
    ga4_property_id: str,
    gsc_site_url: str | None,
    date_from: date,
    date_to: date,
    label: str,
    retries: int,
    mark_period: Any,
    emit: Emit,
    is_cancelled: Any,
    sleep: Any = time.sleep,
) -> PeriodMetrics:
    """Up to `retries` attempts, the same shape as the API path's own retry
    helper. A `403 PERMISSION_DENIED` / missing-credentials failure is NOT
    retried — it will fail identically every time — and is re-raised for the
    caller to turn into a single `not_configured` run (task-84 §3.4).
    """
    for attempt in range(1, retries + 1):
        if is_cancelled():
            raise RuntimeError(f"Cancelled while fetching {label!r}")
        mark_period(label, "running", attempt=attempt, max_attempts=retries)
        try:
            return google_source.fetch_period_metrics(
                session,
                ga4_property_id=ga4_property_id,
                gsc_site_url=gsc_site_url,
                date_from=date_from,
                date_to=date_to,
            )
        except (GoogleApiPermissionError, GoogleApiConfigError):
            raise
        except Exception as exc:  # noqa: BLE001 - bounded retry, caller isolates the final failure
            if attempt == retries:
                raise
            emit(
                "warning", "fetch",
                f"{label}: attempt {attempt}/{retries} failed ({exc}) — retrying",
                {"period": label, "attempt": attempt, "max_attempts": retries},
            )
            sleep(3.0 * attempt)
    raise AssertionError("unreachable")  # pragma: no cover - loop always returns or raises


def _run_google(  # noqa: PLR0913 - orchestrator, mirrors `_run_api`
    session_scope: Any,
    settings: Settings,
    run_id: str,
    client: Any,
    kind: str,
    periods: list[ReportPeriod],
    emit: Emit,
    is_cancelled: Any,
    mark_period: Any,
) -> ReportTable:
    """The direct-Google-API source (task-84): the same per-period isolation,
    `mark_period` transitions and `emit` cadence as `_run_api`, only the data
    module differs — Reporting Ninja is not in the chain at all.
    """
    if kind == "comparator":
        # task-84 §1: a comparator column is `PDF | API | Diff %` — it compares
        # the two Reporting Ninja transports, which this source replaces rather
        # than joins. Fail loudly instead of producing a half-built table.
        raise NotConfiguredError(
            "The Google API source does not produce comparator reports — that "
            "report compares the PDF and API transports column by column. Use "
            "API or PDF for a comparator.",
            error_code="source_unsupported",
        )

    with session_scope() as session:
        client_repo = ClientRepository(session)
        rn_integration = client_repo.get_integration(client.id, Provider.reporting_ninja)
        gsc_integration = client_repo.get_integration(client.id, Provider.gsc)

    # The GA4 property id lives on the `reporting_ninja` integration's
    # `config_json["account_id"]`, already in Google's own `properties/<id>`
    # form (task-84 §2 / task-83 §0.3) — no `connection_key` needed here.
    if rn_integration is None or not rn_integration.config_json.get("account_id"):
        raise NotConfiguredError(
            "No GA4 property id for this client (the Reporting Ninja integration "
            "holds it) — connect it before generating a Google API report"
        )
    ga4_property_id = rn_integration.config_json["account_id"]

    gsc_site_url: str | None = None
    if gsc_integration is not None and gsc_integration.config_json.get("account_id"):
        gsc_site_url = gsc_integration.config_json["account_id"]
    else:
        emit("warning", "preflight", "GSC not configured — clicks/impressions unavailable", {})

    try:
        google = open_google_api_session(settings)
    except GoogleApiConfigError as exc:
        # No refresh token in the environment. `not_configured`, never a silent
        # fallback to Reporting Ninja (task-84 §4.3 / task-83 §6.1.3).
        raise NotConfiguredError(
            "Google API is not configured (no refresh token in the environment) "
            "— pick the API or PDF source, or set it up",
            error_code="not_configured",
        ) from exc

    metrics_by_label: dict[str, PeriodMetrics] = {}
    periods_done = periods_failed = 0
    with google:
        for period in periods:
            if is_cancelled():
                emit("info", "run", "Cancelled", {})
                break
            try:
                metrics = _fetch_google_period_with_retry(
                    google,
                    ga4_property_id=ga4_property_id,
                    gsc_site_url=gsc_site_url,
                    date_from=period.date_from,
                    date_to=period.date_to,
                    label=period.label,
                    retries=settings.reports_api_period_retries,
                    mark_period=mark_period,
                    emit=emit,
                    is_cancelled=is_cancelled,
                )
            except GoogleApiPermissionError as exc:
                # task-84 §3.4 / PLAN §9.2: no access to the property is "not
                # configured", not a red run — every remaining period is the
                # same property and would fail identically.
                raise NotConfiguredError(
                    "Google API: the agency account has no access to this "
                    f"client's GA4/GSC property ({exc})",
                    error_code="not_configured",
                ) from exc
            except Exception as exc:  # noqa: BLE001 - isolate this period, keep going
                periods_failed += 1
                mark_period(period.label, "failed", error=str(exc))
                emit("error", "fetch", f"{period.label} failed: {exc}", {"period": period.label})
            else:
                metrics_by_label[period.label] = metrics
                periods_done += 1
                mark_period(period.label, "done")
                emit("info", "fetch", f"{period.label} done", {"period": period.label})
                if (
                    gsc_site_url
                    and metrics.clicks is None
                    and (date.today() - period.date_from).days > _GSC_HISTORY_LIMIT_DAYS
                ):
                    emit(
                        "warning", "fetch",
                        f"{period.label}: GSC Search Analytics API only serves ~16 months "
                        "of history — Clicks/Impressions for this period cannot be read "
                        "directly and stay blank",
                        {"period": period.label},
                    )
            with session_scope() as session:
                ReportRepository(session).update_progress(
                    run_id, periods_done=periods_done, periods_failed=periods_failed
                )

        history = (
            None
            if is_cancelled()
            else _google_history(google, settings, ga4_property_id, gsc_site_url, emit)
        )

    return replace(build_table(kind, periods, metrics_by_label), history=history)


def _google_history(
    session: Any,
    settings: Settings,
    ga4_property_id: str,
    gsc_site_url: str | None,
    emit: Emit,
) -> ReportHistory | None:
    """The monthly Historical Data block through the direct transport —
    best-effort, exactly like `_history_with_client`: a run with a valid table
    is never failed by its history block.
    """
    try:
        history = google_source.fetch_history(
            session,
            ga4_property_id=ga4_property_id,
            gsc_site_url=gsc_site_url,
            months=settings.reports_history_months,
        )
        emit(
            "info", "history",
            f"Historical Data: {len(history.months)} complete month(s)",
            {},
        )
        return history
    except Exception as exc:  # noqa: BLE001 - see docstring
        emit("warning", "history", f"Historical Data could not be collected: {exc}", {})
        return None


def _run_pdf(  # noqa: PLR0913 - orchestrator, see module docstring
    session_scope: Any,
    settings: Settings,
    run_id: str,
    client: Any,
    kind: str,
    periods: list[ReportPeriod],
    emit: Emit,
    is_cancelled: Any,
    mark_period: Any,
    *,
    force_refresh: bool,
) -> tuple[ReportTable, CrossCheckResult | None]:
    with session_scope() as session:
        client_repo = ClientRepository(session)
        rn_integration = client_repo.get_integration(client.id, Provider.reporting_ninja)
        gsc_integration = client_repo.get_integration(client.id, Provider.gsc)

    if rn_integration is None:
        raise NotConfiguredError("Reporting Ninja is not configured for this client")
    report_name = rn_integration.config_json.get("ninja_report_name")
    if not report_name:
        raise ReportNameMissingError(client.name)

    email = os.environ.get("REPORTING_NINJA_EMAIL", "")
    password = os.environ.get("REPORTING_NINJA_PASSWORD", "")
    if not email or not password:
        raise NotConfiguredError("REPORTING_NINJA_EMAIL/PASSWORD are not set")

    emit("info", "preflight", f"Reporting Ninja report: {report_name}", {})

    to_download, cached_by_label = _split_cached(
        session_scope, settings, run_id, client.id, report_name, periods, emit,
        mark_period, force_refresh=force_refresh,
    )

    artifacts_root = settings.artifacts_dir / "reports" / run_id
    downloaded = []
    if to_download:
        downloaded = ninja_browser.download_periods(
            report_name, to_download, artifacts_root,
            emit=emit, is_cancelled=is_cancelled,
            email=email, password=password,
            headless=settings.reports_headless,
            retries=settings.reports_pdf_retries,
            screenshots_dir=artifacts_root / "screenshots",
            # The browser loop is the only place that knows when a download
            # actually starts (task-45 §2.1) — without this the period would
            # sit `waiting` for its whole ~55s download.
            on_period_state=mark_period,
        )
    downloaded_by_label = {d.period.label: d.pdf_path for d in downloaded}

    pdf_metrics: dict[str, PeriodMetrics] = {}
    sources: dict[str, str] = {}
    periods_done = periods_failed = 0

    for label, cached_path in cached_by_label.items():
        parsed = parse_and_check(cached_path, label, emit)
        if parsed is None:
            periods_failed += 1
            mark_period(label, "failed", error="Cached PDF could not be parsed")
            continue
        pdf_metrics[label] = parsed
        sources[label] = cached_path.name
        periods_done += 1
    if cached_by_label:
        with session_scope() as session:
            ReportRepository(session).update_progress(
                run_id, periods_done=periods_done, periods_failed=periods_failed
            )

    for period in to_download:
        pdf_path = downloaded_by_label.get(period.label)
        if pdf_path is None:
            periods_failed += 1  # ninja_browser already emitted the error event
            continue
        parsed = parse_and_check(pdf_path, period.label, emit)
        if parsed is None:
            periods_failed += 1
            mark_period(period.label, "failed", error="PDF parsed 0/8 metrics")
            continue
        pdf_metrics[period.label] = parsed
        sources[period.label] = pdf_path.name
        periods_done += 1
        with session_scope() as session:
            ReportRepository(session).create_file(
                client.id, run_id=run_id, kind="pdf",
                path=str(pdf_path.relative_to(settings.artifacts_dir)),
                sha256=sha256_of(pdf_path), size_bytes=pdf_path.stat().st_size,
                mime="application/pdf",
                period_label=period.label, date_from=period.date_from, date_to=period.date_to,
                is_partial_period=period.is_partial, source_report_name=report_name,
            )
        with session_scope() as session:
            ReportRepository(session).update_progress(
                run_id, periods_done=periods_done, periods_failed=periods_failed
            )

    history = (
        None
        if is_cancelled()
        else _try_history(settings, rn_integration, gsc_integration, emit)
    )
    table = replace(
        build_table(kind, periods, pdf_metrics, sources=sources), history=history
    )
    cross_check_result = _try_cross_check(
        settings, rn_integration, gsc_integration, periods, pdf_metrics, emit
    )

    # The workbook itself is written by the shared tail in `run()` (task-45
    # §2.3) — both sources go through exactly one code path now.
    return table, cross_check_result


def _split_cached(  # noqa: PLR0913 - see `_run_pdf`
    session_scope: Any,
    settings: Settings,
    run_id: str,
    client_id: str,
    report_name: str,
    periods: list[ReportPeriod],
    emit: Emit,
    mark_period: Any,
    *,
    force_refresh: bool,
) -> tuple[list[ReportPeriod], dict[str, Path]]:
    """Which periods can reuse a cached closed-period PDF (task-42 §3.5)."""
    if force_refresh or not settings.reports_pdf_cache_enabled:
        return list(periods), {}

    to_download: list[ReportPeriod] = []
    cached_by_label: dict[str, Path] = {}
    cached_at_by_label: dict[str, str] = {}
    with session_scope() as session:
        repo = ReportRepository(session)
        for period in periods:
            if period.is_partial:
                to_download.append(period)
                continue
            cached = repo.find_cached_file(client_id, report_name, period.date_from, period.date_to)
            if cached is None:
                to_download.append(period)
            else:
                cached_by_label[period.label] = settings.artifacts_dir / cached.path
                cached_at_by_label[period.label] = cached.created_at.date().isoformat()
                mark_period(period.label, "cached", from_cache=True)
                emit(
                    "info", "download",
                    f"Reused cached PDF for {period.label} "
                    f"(downloaded {cached.created_at.date().isoformat()})",
                    {"period": period.label},
                )
        # task-49 §2.6: the original download date, stamped directly onto the
        # `periods_json` row so the run/document footer can show it without
        # re-deriving it from event log text (task-45 §1.1 already rejected
        # that approach once). No repository setter for this — `update_period`
        # is owned by a file this task does not touch — so the whole list is
        # re-read and reassigned here, the same convention `set_periods` uses.
        if cached_at_by_label:
            run = repo.get(run_id)
            if run is not None and run.periods_json:
                run.periods_json = [
                    {**row, "cached_at": cached_at_by_label[row["label"]]}
                    if row.get("label") in cached_at_by_label
                    else row
                    for row in run.periods_json
                ]
                session.flush()
    return to_download, cached_by_label


def _history_with_client(  # noqa: PLR0913 - one call site, plain data arguments
    ninja: ReportingNinjaClient,
    settings: Settings,
    rn_integration: Any,
    ga4_fields: dict[str, str],
    gsc_ref: tuple[str, str] | None,
    gsc_fields: dict[str, str] | None,
    emit: Emit,
) -> ReportHistory | None:
    """The history block over an ALREADY OPEN client (API path — it has one).
    Best-effort: a run with a valid table is never failed by its history.
    """
    try:
        history = fetch_history(
            ninja,
            ga4_connection_key=rn_integration.config_json["connection_key"],
            ga4_account_id=rn_integration.config_json["account_id"],
            ga4_fields=ga4_fields,
            gsc_ref=gsc_ref,
            gsc_fields=gsc_fields,
            months=settings.reports_history_months,
        )
        emit(
            "info", "history",
            f"Historical Data: {len(history.months)} complete month(s)",
            {},
        )
        return history
    except Exception as exc:  # noqa: BLE001 - see docstring
        emit("warning", "history", f"Historical Data could not be collected: {exc}", {})
        return None


def _try_history(
    settings: Settings,
    rn_integration: Any,
    gsc_integration: Any,
    emit: Emit,
) -> ReportHistory | None:
    """Same block for the PDF path, which has no open API client of its own —
    two queries against the REST API even though the report itself came from
    the browser (task-45 §2.4). Silently skipped when the API key or the
    Reporting Ninja connection is missing.
    """
    api_key = os.environ.get("REPORTING_NINJA_API_KEY", "")
    if not api_key or not rn_integration.config_json.get("connection_key"):
        return None
    try:
        timeout = settings.http_request_timeout_seconds
        with ReportingNinjaClient(api_key, timeout_seconds=timeout) as ninja:
            ga4_fields = discover_ga4_fields(ninja)
            gsc_ref: tuple[str, str] | None = None
            gsc_fields: dict[str, str] | None = None
            if gsc_integration is not None and gsc_integration.config_json.get("connection_key"):
                gsc_ref = (
                    gsc_integration.config_json["connection_key"],
                    gsc_integration.config_json["account_id"],
                )
                gsc_fields = discover_gsc_fields(ninja)
            return _history_with_client(
                ninja, settings, rn_integration, ga4_fields, gsc_ref, gsc_fields, emit
            )
    except Exception as exc:  # noqa: BLE001 - see docstring
        emit("warning", "history", f"Historical Data could not be collected: {exc}", {})
        return None


def _try_cross_check(
    settings: Settings,
    rn_integration: Any,
    gsc_integration: Any,
    periods: list[ReportPeriod],
    pdf_metrics: dict[str, PeriodMetrics],
    emit: Emit,
) -> CrossCheckResult | None:
    """Best-effort API cross-check for every period the PDF path produced
    data for (task-42 §3.6). A cross-check failure never fails the run — the
    PDF-path result already stands on its own.
    """
    if not pdf_metrics:
        return None
    api_key = os.environ.get("REPORTING_NINJA_API_KEY", "")
    if not api_key or not rn_integration.config_json.get("connection_key"):
        return None

    try:
        timeout = settings.http_request_timeout_seconds
        with ReportingNinjaClient(api_key, timeout_seconds=timeout) as ninja:
            ga4_fields = discover_ga4_fields(ninja)
            gsc_ref = None
            gsc_fields = None
            if gsc_integration is not None and gsc_integration.config_json.get("connection_key"):
                gsc_ref = (
                    gsc_integration.config_json["connection_key"],
                    gsc_integration.config_json["account_id"],
                )
                gsc_fields = discover_gsc_fields(ninja)

            api_metrics = {
                period.label: fetch_period_metrics(
                    ninja,
                    ga4_connection_key=rn_integration.config_json["connection_key"],
                    ga4_account_id=rn_integration.config_json["account_id"],
                    ga4_fields=ga4_fields,
                    gsc_ref=gsc_ref,
                    gsc_fields=gsc_fields,
                    date_from=period.date_from,
                    date_to=period.date_to,
                )
                for period in periods
                if period.label in pdf_metrics
            }
        compared_periods = [p for p in periods if p.label in pdf_metrics]
        result = cross_check(
            compared_periods, pdf_metrics, api_metrics,
            tolerance_pct=settings.reports_cross_check_tolerance_pct,
        )
        for mismatch in result.mismatches:
            emit("warning", "cross_check", mismatch.describe(), {})
        return result
    except Exception as exc:  # noqa: BLE001 - cross-check is a bonus, not a gate
        emit("warning", "cross_check", f"Cross-check could not run: {exc}", {})
        return None
