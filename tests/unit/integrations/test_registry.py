import pytest

from app.integrations import registry
from app.schemas.enums import Provider


class _FakeAdapter:
    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    def preflight(self, context):
        raise NotImplementedError

    def collect(self, context, emit, is_cancel_requested):
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})


def test_register_and_get_adapter():
    adapter = _FakeAdapter(Provider.ahrefs)

    registry.register_adapter(adapter)

    assert registry.get_adapter(Provider.ahrefs) is adapter
    assert registry.available_providers() == [Provider.ahrefs]


def test_register_duplicate_raises():
    registry.register_adapter(_FakeAdapter(Provider.gsc))

    with pytest.raises(ValueError):
        registry.register_adapter(_FakeAdapter(Provider.gsc))


def test_get_missing_adapter_raises():
    with pytest.raises(LookupError):
        registry.get_adapter(Provider.semrush)


def test_available_providers_empty_by_default():
    assert registry.available_providers() == []
