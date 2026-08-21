"""Monthly Performance table repository (task-54): the only place that
touches `client_monthly_metrics`/`client_monthly_table_settings` directly.

Same conventions as `app/db/repositories/monitor.py`: constructed directly
with a plain `Session`, every mutating method `flush()`es and leaves
`commit()` to the caller's transaction boundary.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import ClientMonthlyMetric, ClientMonthlyTableSettings
from app.reports.monthly_table import MonthFetch, is_row_freezable


class ClientMonthlyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def commit(self) -> None:
        self._session.commit()

    # --- settings --------------------------------------------------------

    def get_settings(self, client_id: str) -> ClientMonthlyTableSettings | None:
        stmt = select(ClientMonthlyTableSettings).where(
            ClientMonthlyTableSettings.client_id == client_id
        )
        return self._session.scalars(stmt).first()

    def get_or_create_settings(self, client_id: str) -> ClientMonthlyTableSettings:
        settings = self.get_settings(client_id)
        if settings is not None:
            return settings
        settings = ClientMonthlyTableSettings(client_id=client_id)
        self._session.add(settings)
        self._session.flush()
        return settings

    def update_settings(self, client_id: str, **fields: Any) -> ClientMonthlyTableSettings:
        settings = self.get_or_create_settings(client_id)
        for key, value in fields.items():
            if value is not None:
                setattr(settings, key, value)
        settings.updated_at = utcnow()
        self._session.flush()
        return settings

    def months_with_stale_attribution(
        self, client_id: str, *, attribution_users: str, attribution_events: str
    ) -> list[str]:
        """Months already stored under a DIFFERENT attribution than the one
        about to be saved — the `409 attribution_changed` warning body
        (task-54 §2.2/§2.5). Informational only: it never blocks the save,
        it tells the operator which frozen rows now disagree with the
        client's current setting.
        """
        stmt = (
            select(ClientMonthlyMetric.month)
            .where(
                ClientMonthlyMetric.client_id == client_id,
                (ClientMonthlyMetric.attribution_users != attribution_users)
                | (ClientMonthlyMetric.attribution_events != attribution_events),
            )
            .order_by(ClientMonthlyMetric.month)
        )
        return list(self._session.scalars(stmt).all())

    # --- metrics -----------------------------------------------------------

    def list_window(self, client_id: str, keys: list[str]) -> dict[str, ClientMonthlyMetric]:
        if not keys:
            return {}
        stmt = select(ClientMonthlyMetric).where(
            ClientMonthlyMetric.client_id == client_id, ClientMonthlyMetric.month.in_(keys)
        )
        return {row.month: row for row in self._session.scalars(stmt).all()}

    def months_needing_collection(
        self, client_id: str, keys: list[str], *, force: str | None
    ) -> list[str]:
        """Which of `keys` still need a live RN query. Plain `/refresh`
        (`force=None`) skips every already-frozen month — the whole point of
        freezing (task-54 §2.2): a closed, settled, successfully-collected
        month is NEVER re-queried, so GSC's 16-month window can never make it
        vanish. `force="all"` re-collects everything; `force="<month>"`
        re-collects exactly that one month even if frozen.
        """
        if force == "all":
            return list(keys)
        existing = self.list_window(client_id, keys)
        if force and force != "all":
            return [
                key
                for key in keys
                if key not in existing or not existing[key].is_frozen or key == force
            ]
        return [key for key in keys if key not in existing or not existing[key].is_frozen]

    def upsert_month(
        self,
        client_id: str,
        fetched: MonthFetch,
        *,
        days_in_month: int,
        attribution_users: str,
        attribution_events: str,
        key_event_names: list[str],
        today: date,
    ) -> ClientMonthlyMetric:
        """Insert or overwrite one `(client, month)` row. Never called for a
        month `months_needing_collection` excluded — an already-frozen row
        is simply never passed here, which is what makes freezing durable.
        """
        stmt = select(ClientMonthlyMetric).where(
            ClientMonthlyMetric.client_id == client_id, ClientMonthlyMetric.month == fetched.month
        )
        row = self._session.scalars(stmt).first()
        if row is None:
            row = ClientMonthlyMetric(client_id=client_id, month=fetched.month)
            self._session.add(row)

        row.organic_users = fetched.organic_users
        row.organic_conversions = fetched.organic_conversions
        row.key_event_count = fetched.key_event_count
        row.gsc_clicks = fetched.gsc_clicks
        row.gsc_impressions = fetched.gsc_impressions
        row.gsc_days_with_data = fetched.gsc_days_with_data
        row.days_in_month = days_in_month
        row.attribution_users = attribution_users
        row.attribution_events = attribution_events
        row.key_event_names_json = list(key_event_names)
        row.ga4_state = fetched.ga4_state
        row.gsc_state = fetched.gsc_state
        row.truncated = fetched.truncated
        row.is_frozen = is_row_freezable(
            fetched.month, fetched.ga4_state, fetched.gsc_state, today=today
        )
        row.collected_at = utcnow()
        row.updated_at = utcnow()
        self._session.flush()
        return row
