from datetime import date

import pytest
from pydantic import ValidationError

from app.schemas.adapter import DateRange
from app.schemas.api import (
    CellProgressDTO,
    ClientCreateRequest,
    MetricCardValue,
    ProviderSummaryDTO,
    SyncJobRequest,
)
from app.schemas.enums import (
    BetterDirection,
    CellStatus,
    MetricUnit,
    PeriodKind,
    Provider,
    Quality,
    SourceMode,
)


def test_sync_job_request_accepts_all_literal_for_client_ids_and_providers():
    request = SyncJobRequest(client_ids="all", providers="all", period_kind=PeriodKind.week)

    assert request.client_ids == "all"
    assert request.providers == "all"
    assert request.mode == "collect_missing"


def test_sync_job_request_accepts_explicit_lists():
    request = SyncJobRequest(
        client_ids=["client-1", "client-2"],
        providers=[Provider.ahrefs, Provider.semrush],
        period_kind=PeriodKind.month,
        mode="force_refresh",
    )

    assert request.client_ids == ["client-1", "client-2"]
    assert request.providers == [Provider.ahrefs, Provider.semrush]


def test_sync_job_request_accepts_custom_range():
    request = SyncJobRequest(
        client_ids=["client-1"],
        providers=[Provider.gsc],
        period_kind=PeriodKind.custom,
        custom_start=date(2025, 1, 1),
        custom_end=date(2025, 12, 31),
    )

    assert request.custom_start == date(2025, 1, 1)
    assert request.custom_end == date(2025, 12, 31)


@pytest.mark.parametrize(
    "payload",
    [
        {"period_kind": PeriodKind.custom},
        {
            "period_kind": PeriodKind.custom,
            "custom_start": date(2025, 2, 1),
            "custom_end": date(2025, 1, 1),
        },
        {
            "period_kind": PeriodKind.week,
            "custom_start": date(2025, 1, 1),
            "custom_end": date(2025, 1, 7),
        },
    ],
)
def test_sync_job_request_rejects_invalid_custom_range(payload):
    with pytest.raises(ValidationError):
        SyncJobRequest(client_ids=["client-1"], providers=[Provider.gsc], **payload)


def test_sync_job_request_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        SyncJobRequest(
            client_ids="all",
            providers="all",
            period_kind=PeriodKind.week,
            unexpected_flag=True,
        )


def test_client_create_request_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        ClientCreateRequest(
            name="Acme",
            slug="acme",
            domain="acme.com",
            timezone="America/New_York",
            secret_api_key="should-not-exist",
        )


def test_metric_card_value_null_delta_carries_reason_not_infinity():
    card = MetricCardValue(
        metric_key="clicks",
        label="Clicks",
        unit=MetricUnit.count,
        comparison_kind="range",
        current_value=120,
        previous_value=0,
        delta_pct=None,
        delta_reason="previous_zero",
        better_direction=BetterDirection.up,
        quality=Quality.complete,
        source_mode=SourceMode.api,
        current_range=DateRange(start=date(2026, 7, 13), end=date(2026, 7, 19)),
        previous_range=DateRange(start=date(2026, 7, 6), end=date(2026, 7, 12)),
    )

    assert card.delta_pct is None
    assert card.delta_reason == "previous_zero"


def test_provider_summary_dto_supports_not_configured_state_without_quality():
    summary = ProviderSummaryDTO(provider=Provider.reporting_ninja, state="not_configured")

    assert summary.state == "not_configured"
    assert summary.quality is None
    assert summary.metrics == []


def test_provider_summary_dto_rejects_unknown_state():
    with pytest.raises(ValidationError):
        ProviderSummaryDTO(provider=Provider.gsc, state="bogus_state")


def test_cell_progress_dto_round_trip():
    cell = CellProgressDTO(
        client_id="client-1",
        provider=Provider.ahrefs,
        status=CellStatus.not_configured,
        error_code=None,
    )

    assert cell.status is CellStatus.not_configured
    assert cell.progress_percent is None
    assert cell.attempt == 1
