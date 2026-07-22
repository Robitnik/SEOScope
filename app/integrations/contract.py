"""Provider adapter contract (PLAN §7) — the seam every provider adapter
(task-09/10/11) implements and the worker (task-05) resolves through the
registry. No implementations live here, only the `Protocol`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from app.schemas.adapter import CollectionContext, CollectionResult, PreflightResult, ProgressEvent
from app.schemas.enums import Provider


@runtime_checkable
class ProviderAdapter(Protocol):
    """Contract every provider adapter implements.

    Invariants:
    - `collect()` never raises on partial data: it returns a
      `CollectionResult` with `quality in {partial, truncated, unavailable}`
      and populated `warnings` instead of throwing. Only a truly unexpected
      failure may propagate, and even then the worker converts it into a
      `failed` cell rather than crashing (PLAN §9.2).
    - `emit` is best-effort; the adapter must not depend on its return value
      or on delivery succeeding.
    - `is_cancel_requested()` is polled only at safe stage boundaries;
      cancellation is cooperative and never kills an in-flight write.
    - Secrets never appear in `ProgressEvent`, `warnings`, `error_code` or
      `provenance` — all of these are persisted and/or shown to the UI.
    """

    provider: Provider

    def preflight(self, context: CollectionContext) -> PreflightResult: ...

    def collect(
        self,
        context: CollectionContext,
        emit: Callable[[ProgressEvent], None],
        is_cancel_requested: Callable[[], bool],
    ) -> CollectionResult: ...
