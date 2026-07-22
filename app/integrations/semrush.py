"""`ProviderAdapter` for Semrush — vertical slice (task-11; PLAN §2.2/§3.2/§8.3).

Recommended path is **browser export + `ingest_semrush`** (PLAN §2.2): no metered units, a
persistent logged-in profile does the work. The API (`SemrushClient`) is a metered FALLBACK
only, used under explicit policy — this adapter treats `integration_config["unit_budget"]`
(a positive int) as that explicit policy signal: without it, a missing/stale browser profile
just surfaces as `not_configured`/`auth_required`, never a silent API spend.

`CollectionContext.integration_config` shape for this provider — all non-secret external
identifiers (PLAN §7):

    {
      "domain": "example.com",       # required
      "database": "us",              # optional, default "us" (Semrush regional database)
      "profile": "semrush",          # optional, default "semrush" (persistent browser profile)
      "semrush_client_label": "...", # optional, cosmetic label only (provenance)
      "unit_budget": 500,            # optional — presence is the "explicit policy" opt-in for
                                      # the metered API fallback in `auto`/`api` source modes
    }

`SEMRUSH_API_KEY` itself never lives here or in the DB: `SemrushClient` reads it from
AISeoTools' own `.env` (see AISeoTools `seo_audit/semrush/client.py`).

Each collection attempt gets its OWN, freshly created directory under
`settings.artifacts_dir` (never AISeoTools' shared CLI `out/` tree, and never reused across
attempts) — this is what makes a retry structurally unable to mix an earlier attempt's
downloads into a new one, and what keeps a stale CSV from a previous run out of the snapshot
(PLAN §17 risk; PLAN Stage 6 gate).

`seo_audit` is only imported lazily, inside method bodies — never at module import time — so
merely importing/registering this adapter (which `app/integrations/__init__.py` does
unconditionally) never fails in an environment without the local AISeoTools checkout (e.g. CI;
see compose.yaml/docker/entrypoint.sh). Actually calling `collect()`/`preflight()` without it
present degrades to `not_configured`, never a crash.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.integrations.registry import register_adapter
from app.jobs.fake_adapter import CancelledByAdapter
from app.schemas.adapter import (
    CollectionContext,
    CollectionResult,
    MetricValueResult,
    PreflightResult,
    ProgressEvent,
)
from app.schemas.enums import MetricUnit, PeriodRole, Provider, Quality, SourceMode

SEMRUSH_API_KEY_ENV = "SEMRUSH_API_KEY"

# Two flat-cost calls (domain_ranks + backlinks_overview), one row each — see
# `seo_audit.dashboard.semrush.collect_semrush_api`'s own docstring.
_API_UNITS_ESTIMATE = 50

# `seo_audit.dashboard.models.Quality`'s own declared order — used only to compare two
# `DashboardResult.quality` values when picking the better of browser vs. API-fallback attempts.
_DASH_QUALITY_ORDER = ("complete", "partial", "truncated", "stale", "unavailable")


def _dash_quality_rank(value: str) -> int:
    return _DASH_QUALITY_ORDER.index(value)


def _browser_profile_ready(profile: str) -> bool:
    """Best-effort, cheap readiness check — never launches a browser. A profile directory
    existing does not guarantee a live session (a stale/expired session only surfaces as
    `auth_required` once `collect()` actually opens it), but its ABSENCE is a reliable
    `not_configured` signal pointing at the one-time manual login flow."""
    try:
        from seo_audit.browser.profile import BrowserProfile
    except ImportError:
        return False
    try:
        return bool(BrowserProfile(profile).user_data_dir.is_dir())
    except ValueError:
        return False


def _new_artifacts_dir(client_id: str) -> Path:
    """A fresh directory for exactly ONE collection attempt (see module docstring)."""
    from app.core.config import get_settings

    root = get_settings().artifacts_dir / "semrush_runs" / client_id
    return root / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


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


class SemrushAdapter:
    """`ProviderAdapter` for `Provider.semrush` (PLAN §7)."""

    provider = Provider.semrush

    def preflight(self, context: CollectionContext) -> PreflightResult:
        domain = context.integration_config.get("domain")
        if not domain:
            return PreflightResult(
                provider=self.provider,
                ok=False,
                reason_code="not_configured",
                message="Semrush integration_config is missing 'domain'",
            )

        if context.source_mode == SourceMode.pdf:
            return PreflightResult(
                provider=self.provider,
                ok=False,
                reason_code="not_configured",
                message="Semrush does not support source_mode=pdf",
            )

        profile = str(context.integration_config.get("profile", "semrush"))
        browser_ready = _browser_profile_ready(profile)
        api_key_present = bool(os.environ.get(SEMRUSH_API_KEY_ENV))

        if context.source_mode == SourceMode.browser:
            if not browser_ready:
                return PreflightResult(
                    provider=self.provider,
                    ok=False,
                    reason_code="not_configured",
                    message=f"Semrush browser profile {profile!r} not found — complete the "
                    "one-time manual login flow first (seo-audit browser-login "
                    f"--provider {profile}).",
                )
            return PreflightResult(provider=self.provider, ok=True, units_estimated=0)

        if context.source_mode == SourceMode.api:
            if not api_key_present:
                return PreflightResult(
                    provider=self.provider,
                    ok=False,
                    reason_code="auth_required",
                    message=f"{SEMRUSH_API_KEY_ENV} is not set",
                )
            return PreflightResult(
                provider=self.provider, ok=True, units_estimated=_API_UNITS_ESTIMATE
            )

        # auto: browser is the recommended, unmetered path; the metered API fallback only
        # counts toward `units_estimated` when the explicit `unit_budget` policy is set.
        if browser_ready:
            return PreflightResult(provider=self.provider, ok=True, units_estimated=0)
        if api_key_present and context.integration_config.get("unit_budget"):
            return PreflightResult(
                provider=self.provider, ok=True, units_estimated=_API_UNITS_ESTIMATE
            )
        return PreflightResult(
            provider=self.provider,
            ok=False,
            reason_code="not_configured",
            message="No Semrush browser profile found, and no 'unit_budget' policy configured "
            "to allow the metered API fallback.",
        )

    def collect(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
        is_cancel_requested: Callable[[], bool],
    ) -> CollectionResult:
        try:
            return self._collect(context, emit, is_cancel_requested)
        except CancelledByAdapter:
            raise  # cooperative cancellation — the worker matches on this type (PLAN §9.2)
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
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
        is_cancel_requested: Callable[[], bool],
    ) -> CollectionResult:
        from seo_audit.units_fallback import UnitsExhausted

        if is_cancel_requested():
            raise CancelledByAdapter("semrush: cancelled before collection started")

        domain = str(context.integration_config.get("domain", ""))
        database = str(context.integration_config.get("database", "us"))
        profile = str(context.integration_config.get("profile", "semrush"))
        client_label = str(context.integration_config.get("semrush_client_label", ""))
        unit_budget = context.integration_config.get("unit_budget")
        on_event = _bridge_emit(emit)

        browser_allowed = context.source_mode in (SourceMode.browser, SourceMode.auto)
        # "explicit policy/budget": in `auto` mode the metered API is only tried when a budget
        # was explicitly configured; in explicit `api` mode it is always allowed.
        api_allowed = context.source_mode == SourceMode.api or (
            context.source_mode == SourceMode.auto and bool(unit_budget)
        )

        result = None
        source_used: str | None = None
        units_exhausted_detail: str | None = None
        attempted = False

        if browser_allowed and _browser_profile_ready(profile):
            if is_cancel_requested():
                raise CancelledByAdapter("semrush: cancelled before browser collection")
            from seo_audit.dashboard.semrush import collect_semrush_browser

            attempted = True
            artifacts_dir = _new_artifacts_dir(context.client_id)
            result = collect_semrush_browser(
                domain,
                artifacts_dir=artifacts_dir,
                client=client_label,
                database=database,
                profile=profile,
                virtual_display=not bool(os.environ.get("DISPLAY")),
                no_sandbox=os.geteuid() == 0,
                clear_stale_singletons=True,
                on_event=on_event,
            )
            source_used = "browser"

        needs_fallback = api_allowed and (
            result is None or result.quality.value == "unavailable"
        )
        if needs_fallback and os.environ.get(SEMRUSH_API_KEY_ENV):
            if is_cancel_requested():
                raise CancelledByAdapter("semrush: cancelled before API fallback")
            from seo_audit.dashboard.semrush import collect_semrush_api

            attempted = True
            try:
                api_result = collect_semrush_api(
                    domain, database=database, unit_budget=unit_budget, on_event=on_event,
                )
            except UnitsExhausted as exc:
                units_exhausted_detail = str(exc)
                api_result = None
            if api_result is not None and (
                result is None
                or _dash_quality_rank(api_result.quality.value)
                < _dash_quality_rank(result.quality.value)
            ):
                result = api_result
                source_used = "api" if source_used is None else f"{source_used}+api_fallback"

        if result is None:
            if units_exhausted_detail:
                error_code = "no_units"
            elif not attempted:
                error_code = "not_configured"
            else:
                error_code = "collection_unavailable"
            warnings = (
                [f"Semrush API units exhausted: {units_exhausted_detail}"]
                if units_exhausted_detail
                else ["Semrush: no data source available (no browser profile, no API budget)"]
            )
            return CollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=datetime.now(UTC),
                metrics=[],
                warnings=warnings,
                error_code=error_code,
                provenance={"requested_source_mode": context.source_mode.value},
            )

        collection_result = self._to_collection_result(
            result, context, source_used=source_used or "unknown"
        )
        if units_exhausted_detail and collection_result.error_code is None:
            # The browser attempt already produced an (unavailable) result object, so the
            # `result is None` branch above never ran — the exhausted-budget reason would
            # otherwise be silently lost.
            collection_result = collection_result.model_copy(
                update={
                    "error_code": "no_units",
                    "warnings": [
                        *collection_result.warnings,
                        f"Semrush API units exhausted: {units_exhausted_detail}",
                    ],
                }
            )
        return collection_result

    def _to_collection_result(
        self, dash_result: Any, context: CollectionContext, *, source_used: str
    ) -> CollectionResult:
        aggregate_quality = Quality(dash_result.quality.value)

        metrics: list[MetricValueResult] = []
        for value in dash_result.values:
            quality = Quality.unavailable if value.value_numeric is None else aggregate_quality
            metrics.append(
                MetricValueResult(
                    metric_key=value.metric_key,
                    period_role=PeriodRole.snapshot,
                    value_numeric=value.value_numeric,
                    unit=MetricUnit(value.unit.value),
                    quality=quality,
                )
            )

        provenance = dict(dash_result.provenance)
        provenance["source_used"] = source_used
        provenance["requested_source_mode"] = context.source_mode.value

        return CollectionResult(
            provider=self.provider,
            quality=aggregate_quality,
            observed_at=dash_result.observed_at,
            data_through=dash_result.data_through,
            metrics=metrics,
            warnings=list(dash_result.warnings),
            error_code=dash_result.error_code,
            provenance=provenance,
            units_spent=dash_result.units_spent,
            units_estimated=dash_result.units_estimated,
        )


register_adapter(SemrushAdapter())
