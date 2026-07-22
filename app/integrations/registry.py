"""Provider adapter registry: `Provider -> ProviderAdapter` (PLAN §7).

Populated by provider tasks (09-11) at import time; resolved by the worker
(task-05) via `get_adapter`. Scoped to a single process — this project runs
exactly one worker process (PLAN §5.1), so a plain module-level dict is
sufficient; do not share this across multiple processes.
"""

from __future__ import annotations

from app.integrations.contract import ProviderAdapter
from app.schemas.enums import Provider

_registry: dict[Provider, ProviderAdapter] = {}


def register_adapter(adapter: ProviderAdapter) -> None:
    """Register `adapter` for its `provider`. Raises on duplicate registration."""
    if adapter.provider in _registry:
        raise ValueError(f"adapter already registered for provider={adapter.provider!r}")
    _registry[adapter.provider] = adapter


def get_adapter(provider: Provider) -> ProviderAdapter:
    """Resolve the adapter registered for `provider`. Raises if none is registered."""
    try:
        return _registry[provider]
    except KeyError:
        raise LookupError(f"no adapter registered for provider={provider!r}") from None


def available_providers() -> list[Provider]:
    return list(_registry.keys())
