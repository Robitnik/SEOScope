"""GA4 Admin API client — property metadata and key events (task-89 §4.2).

Two GET calls per client, cached for a day by the caller (property
configuration does not change hour to hour):

- `GET /v1beta/{property}` — `displayName`, `timeZone`, `currencyCode`,
  `industryCategory`. Used to warn when the property's own `timeZone`
  disagrees with `clients.timezone` (task-89 §0.14/§8.4) — day buckets in
  GA4 Data API responses are cut in the PROPERTY's timezone, not ours, so a
  mismatch silently shifts every daily row by the difference.
- `GET /v1beta/{property}/keyEvents` — the client's own declared conversion
  events (`eventName`, `createTime`, `custom`), so `ga4_key_events` (task-89
  §4.1) can label a breakdown with what the CLIENT configured as a
  conversion, rather than guessing from "any event with a non-zero
  `keyEvents` count this period" (a signal that changes if the client edits
  their GA4 config mid-period).

Read-only, additive to `app/integrations/google_api/` — no existing caller
touched.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.integrations.google_api.auth import GoogleApiSession

_GA4_ADMIN_BASE = "https://analyticsadmin.googleapis.com/v1beta"


@dataclass(frozen=True)
class Ga4PropertyInfo:
    """Property-level configuration, not day-range data."""

    property_id: str  # "properties/<id>", echoes the input
    display_name: str
    time_zone: str
    currency_code: str | None
    industry_category: str | None


@dataclass(frozen=True)
class Ga4KeyEvent:
    """One declared key event (GA4's name for "conversion event")."""

    event_name: str
    create_time: str | None
    custom: bool


class Ga4AdminClient:
    def __init__(self, session: GoogleApiSession) -> None:
        self._session = session

    def property_info(self, property_id: str) -> Ga4PropertyInfo:
        """`GET /v1beta/{property}` — raises `GoogleApiPermissionError` /
        `GoogleApiError` same as the Data API client on denial/failure; the
        caller decides how to fold that into a warning, never crashes."""
        payload = self._session.get_json(f"{_GA4_ADMIN_BASE}/{property_id}")
        return Ga4PropertyInfo(
            property_id=str(payload.get("name") or property_id),
            display_name=str(payload.get("displayName") or ""),
            time_zone=str(payload.get("timeZone") or ""),
            currency_code=payload.get("currencyCode"),
            industry_category=payload.get("industryCategory"),
        )

    def key_events(self, property_id: str) -> list[Ga4KeyEvent]:
        """`GET /v1beta/{property}/keyEvents` — the property's own declared
        conversion events. An empty list is a valid answer (property has no
        key events configured), never treated as an error."""
        payload = self._session.get_json(f"{_GA4_ADMIN_BASE}/{property_id}/keyEvents")
        return [
            Ga4KeyEvent(
                event_name=str(item.get("eventName") or ""),
                create_time=item.get("createTime"),
                custom=bool(item.get("custom", False)),
            )
            for item in payload.get("keyEvents") or []
            if item.get("eventName")
        ]
