"""Реєстрація fake-адаптерів для наскрізних тестів (task-13).

`app.jobs.fake_adapter.FakeAdapter` (task-05) — єдиний адаптер, яким CI має
право користуватись: справжні провайдери ходять у браузер і в metered API.
Тут лише зручна обгортка над `app.integrations.registry`, який є модульним
синглтоном і тому потребує явного скидання між тестами.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FakeScenario:
    """`integration_config` для однієї клітинки `(client, provider)`.

    Значення потрапляють у `ClientIntegration.config_json`, звідки їх читає
    `FakeAdapter` через `CollectionContext.integration_config`.
    """

    scenario: str = "success"
    stage_seconds: float = 0.0
    profile_store: str | None = None

    def as_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {
            "scenario": self.scenario,
            "stage_seconds": self.stage_seconds,
        }
        if self.profile_store is not None:
            config["profile_store"] = self.profile_store
        return config


def reset_registry(monkeypatch: Any) -> None:
    """Очистити реєстр адаптерів на час одного тесту."""
    from app.integrations import registry

    monkeypatch.setattr(registry, "_registry", {})


def register_fakes(*providers: Any) -> dict[Any, Any]:
    """Зареєструвати `FakeAdapter` для кожного переданого провайдера.

    Без аргументів реєструє всі чотири — саме та конфігурація, у якій
    працює `Dispatcher` за замовчуванням (доріжка на сервіс, PLAN §9.1).
    """
    from app.integrations import registry
    from app.jobs.fake_adapter import FakeAdapter
    from app.schemas.enums import Provider

    targets = providers or tuple(Provider)
    adapters = {}
    for provider in targets:
        adapter = FakeAdapter(provider)
        registry.register_adapter(adapter)
        adapters[provider] = adapter
    return adapters
