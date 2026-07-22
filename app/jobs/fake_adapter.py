"""Deterministic `ProviderAdapter` for integration/E2E tests (task-12/13).

Real provider adapters (task-09/10/11) talk to browsers and metered APIs —
neither is available nor deterministic in CI. This adapter implements the
same `ProviderAdapter` contract with fully scripted behavior selected via
`CollectionContext.integration_config`, so tests can exercise the worker's
concurrency, isolation and recovery guarantees without any real network I/O.

`integration_config` keys:
- `scenario`: one of `SCENARIOS` below. Default `"success"`.
- `stage_seconds`: float, time to "spend" per stage (default 0). Used to
  prove lane parallelism/sequencing under a controlled clock.
- `not_configured`: bool: if true, `preflight()` returns `ok=False`.
- `preflight_reason_code`: overrides the `not_configured`/`auth_required`/... code.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from app.schemas.adapter import (
    CollectionContext,
    CollectionResult,
    MetricValueResult,
    PreflightResult,
    ProgressEvent,
)
from app.schemas.enums import MetricUnit, PeriodRole, Provider, Quality

SCENARIOS = (
    "success",
    "partial",
    "unavailable",
    "fail",
    "timeout",
    "not_configured",
    "cancel",
    "slow",
)


class CancelledByAdapter(Exception):
    """Raised when the fake adapter observes cancellation mid-`collect`.

    The real contract says `collect()` should return a result, not raise,
    but a cooperative cancellation genuinely does abandon the in-flight
    attempt — the worker (task-05 guard) treats this the same as any other
    unexpected exception: the cell becomes `failed`/`cancelled`, never a crash.
    """


class FakeAdapter:
    """`ProviderAdapter` driven entirely by `integration_config["scenario"]`."""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    def preflight(self, context: CollectionContext) -> PreflightResult:
        cfg = context.integration_config
        scenario = cfg.get("scenario", "success")
        if scenario == "not_configured" or cfg.get("not_configured"):
            return PreflightResult(
                provider=self.provider,
                ok=False,
                reason_code=cfg.get("preflight_reason_code", "not_configured"),
                message="fake adapter: not configured",
            )
        return PreflightResult(provider=self.provider, ok=True)

    def collect(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
        is_cancel_requested: Callable[[], bool],
    ) -> CollectionResult:
        cfg = context.integration_config
        scenario = cfg.get("scenario", "success")
        stage_seconds = float(cfg.get("stage_seconds", 0))
        stages = ("authenticate", "fetch_current", "fetch_previous", "normalize")

        for stage in stages:
            emit(
                ProgressEvent(
                    provider=self.provider,
                    stage=stage,
                    percent=stages.index(stage) / len(stages) * 100,
                    message=f"fake:{scenario}:{stage}",
                )
            )
            if stage_seconds:
                time.sleep(stage_seconds)
            if scenario == "timeout":
                # Simulate a hung adapter: sleep far longer than any
                # reasonable per-cell timeout, ignoring cancellation.
                time.sleep(3600)
            if scenario == "cancel" and is_cancel_requested():
                raise CancelledByAdapter(f"{self.provider}: cancelled at stage {stage}")
            if scenario == "fail" and stage == "fetch_current":
                raise RuntimeError(f"fake adapter forced failure at {stage}")

        observed_at = datetime.now(UTC)

        if scenario == "unavailable":
            return CollectionResult(
                provider=self.provider,
                quality=Quality.unavailable,
                observed_at=observed_at,
                metrics=[],
                warnings=["fake adapter: no data available"],
            )

        metric_key = f"{self.provider.value}_fake_metric"
        quality = Quality.partial if scenario == "partial" else Quality.complete
        metrics = [
            MetricValueResult(
                metric_key=metric_key,
                period_role=PeriodRole.current,
                period_start=context.current.start,
                period_end=context.current.end,
                value_numeric=Decimal(42),
                unit=MetricUnit.count,
                quality=quality,
            ),
            MetricValueResult(
                metric_key=metric_key,
                period_role=PeriodRole.previous,
                period_start=context.previous.start,
                period_end=context.previous.end,
                value_numeric=Decimal(40),
                unit=MetricUnit.count,
                quality=quality,
            ),
        ]
        return CollectionResult(
            provider=self.provider,
            quality=quality,
            observed_at=observed_at,
            metrics=metrics,
            warnings=["fake adapter: partial data"] if scenario == "partial" else [],
            provenance={"source": "fake_adapter", "scenario": scenario},
        )
