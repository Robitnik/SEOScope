"""JSON-schema snapshot test for the shared contract (task-02 DoD).

Any change to a model in `app.schemas` or the enums it's built from shows up
as a diff against `data/contract_snapshot.json`. That makes contract changes
visible in code review instead of silently rippling into every consumer.

To intentionally update the snapshot after a reviewed contract change:
    docker compose run --rm api poetry run python -m tests.unit.schemas.test_contract_snapshot
"""

from __future__ import annotations

import json
from pathlib import Path

from app.schemas import (
    BatchProgressDTO,
    CellProgressDTO,
    ClientCardDTO,
    ClientCreateRequest,
    ClientDTO,
    ClientStatusRequest,
    ClientUpdateRequest,
    CollectionContext,
    CollectionResult,
    CoverageDTO,
    DashboardResponse,
    ErrorDTO,
    IntegrationDTO,
    IntegrationUpsertRequest,
    JobDTO,
    MetricCardValue,
    MetricSpec,
    MetricValueDTO,
    PreflightResult,
    ProgressEvent,
    ProvidersStatusDTO,
    ProviderStatusCellDTO,
    ProviderSummaryDTO,
    SyncJobAcceptedDTO,
    SyncJobRequest,
)
from app.schemas.enums import (
    STAGE_ORDER,
    BetterDirection,
    CellStatus,
    ClientStatus,
    ComparisonMode,
    JobStatus,
    MetricUnit,
    PeriodKind,
    PeriodRole,
    Provider,
    Quality,
    SourceMode,
    StageStatus,
)

SNAPSHOT_PATH = Path(__file__).parent / "data" / "contract_snapshot.json"

_MODELS = {
    "ProgressEvent": ProgressEvent,
    "CollectionContext": CollectionContext,
    "CollectionResult": CollectionResult,
    "PreflightResult": PreflightResult,
    "MetricSpec": MetricSpec,
    "MetricValueDTO": MetricValueDTO,
    "MetricCardValue": MetricCardValue,
    "ProviderSummaryDTO": ProviderSummaryDTO,
    "ClientCardDTO": ClientCardDTO,
    "DashboardResponse": DashboardResponse,
    "ClientDTO": ClientDTO,
    "ClientCreateRequest": ClientCreateRequest,
    "ClientUpdateRequest": ClientUpdateRequest,
    "ClientStatusRequest": ClientStatusRequest,
    "IntegrationDTO": IntegrationDTO,
    "IntegrationUpsertRequest": IntegrationUpsertRequest,
    "SyncJobRequest": SyncJobRequest,
    "SyncJobAcceptedDTO": SyncJobAcceptedDTO,
    "CellProgressDTO": CellProgressDTO,
    "BatchProgressDTO": BatchProgressDTO,
    "JobDTO": JobDTO,
    "ProviderStatusCellDTO": ProviderStatusCellDTO,
    "ProvidersStatusDTO": ProvidersStatusDTO,
    "ErrorDTO": ErrorDTO,
    "CoverageDTO": CoverageDTO,
}

_ENUMS = {
    "Provider": Provider,
    "PeriodKind": PeriodKind,
    "ComparisonMode": ComparisonMode,
    "PeriodRole": PeriodRole,
    "Quality": Quality,
    "BetterDirection": BetterDirection,
    "SourceMode": SourceMode,
    "JobStatus": JobStatus,
    "StageStatus": StageStatus,
    "CellStatus": CellStatus,
    "ClientStatus": ClientStatus,
    "MetricUnit": MetricUnit,
}


def build_snapshot() -> dict:
    return {
        "models": {name: model.model_json_schema() for name, model in sorted(_MODELS.items())},
        "enums": {name: sorted(m.value for m in enum) for name, enum in sorted(_ENUMS.items())},
        "stage_order": list(STAGE_ORDER),
    }


def test_contract_snapshot_matches_recorded_schema():
    current = build_snapshot()
    recorded = json.loads(SNAPSHOT_PATH.read_text())

    assert current == recorded, (
        "app.schemas contract changed — this is a contract change every "
        "consumer must see (CLAUDE.md). If reviewed and intentional, "
        "regenerate data/contract_snapshot.json (see module docstring)."
    )


if __name__ == "__main__":
    SNAPSHOT_PATH.write_text(json.dumps(build_snapshot(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {SNAPSHOT_PATH}")
