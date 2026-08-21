"""DTOs for the Monthly Performance table (task-54), prefix
`/api/v1/clients/{client_id}/monthly-table`.

Same conventions as `app/schemas/reports.py`: `Literal[...]` aliases instead
of `enum.Enum` (matches the DB layer's plain-`String` columns), write DTOs
get `ConfigDict(extra="forbid")`, read DTOs don't.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

Attribution = Literal["first_user", "session"]
Ga4State = Literal["ok", "no_data", "failed"]
GscState = Literal["ok", "no_data", "not_configured", "failed"]
Direction = Literal["up", "down", "flat"]


class MonthlyRowDTO(BaseModel):
    month: str
    label: str
    days_in_month: int
    organic_users: int | None
    organic_conversions: int | None
    key_event_count: int | None
    gsc_clicks: int | None
    gsc_impressions: int | None
    gsc_days_with_data: int | None
    conversion_rate: float | None
    ctr: float | None
    is_partial_period: bool
    ga4_state: Ga4State
    gsc_state: GscState
    truncated: bool
    directions: dict[str, Direction | None]


class MonthlyTableSettingsDTO(BaseModel):
    key_event_names: list[str]
    key_event_label: str | None
    attribution_users: Attribution
    attribution_events: Attribution
    history_months: int | None


class MonthlyTableCoverageDTO(BaseModel):
    """What the table can and cannot show for this client — surfaced so the
    UI can explain absent columns instead of rendering silent `—` (PLAN §12,
    frontend/AGENTS.md §6).
    """

    has_gsc: bool
    has_key_event_column: bool
    requested_months: int
    available_months: int


class MonthlyTableDTO(BaseModel):
    client_id: str
    rows: list[MonthlyRowDTO]
    settings: MonthlyTableSettingsDTO
    coverage: MonthlyTableCoverageDTO
    collected_at: str | None


class MonthlyTableRefreshResponseDTO(BaseModel):
    table: MonthlyTableDTO
    requests_used: int
    months_collected: list[str]


class KeyEventCandidateDTO(BaseModel):
    event_name: str
    key_events: int
    months_seen: int


class KeyEventCandidatesDTO(BaseModel):
    candidates: list[KeyEventCandidateDTO]


class MonthlyTableSettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key_event_names: list[str] | None = None
    key_event_label: str | None = None
    attribution_users: Attribution | None = None
    attribution_events: Attribution | None = None
    history_months: int | None = None


class AttributionChangedDetailDTO(BaseModel):
    """Body of the `409 attribution_changed` warning (task-54 §2.2/§2.5) —
    informational, not blocking: the settings ARE saved, this just lists
    which already-frozen months were collected under the old model so the
    operator can decide whether to force-recollect them.
    """

    affected_months: list[str]
