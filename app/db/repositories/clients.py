"""Client and client-integration repository (PLAN §6, `clients`/`client_integrations`)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import Client, ClientIntegration
from app.schemas.enums import ClientStatus, Provider, SourceMode


class ClientRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_active(self) -> list[Client]:
        """Клієнти, видимі на дашборді — фільтр саме за `enabled`.

        «Active» тут історично означає «не прихований», а **не** «не на
        паузі»: паузований клієнт (task-34) навмисно лишається у видачі, бо
        весь сенс статусу — бути помітним. Кому потрібні клієнти для
        реального збору — беріть `list_collectable()`.
        """
        stmt = select(Client).where(Client.enabled.is_(True)).order_by(Client.name)
        return list(self._session.scalars(stmt))

    # --- Added by task-34 (client lifecycle status).

    def list_collectable(self) -> list[Client]:
        """Клієнти, яких можна брати в масовий збір: видимі **і** не на паузі.

        Це список, у який розгортається `client_ids="all"` (sync-джоби,
        batch-аудит, CLI-беkфіл) і на якому будуються портфельні агрегати.
        Явно названий оператором паузований клієнт збирається як звичайно —
        блокування живе тільки на «all», не на явному виборі.
        """
        stmt = (
            select(Client)
            .where(Client.enabled.is_(True), Client.status == ClientStatus.active)
            .order_by(Client.name)
        )
        return list(self._session.scalars(stmt))

    # --- Added by task-06 (REST API): `GET /clients` is a management list
    # (create/edit/disable), so it must also surface disabled clients —
    # `list_active` intentionally excludes them for the dashboard's use.
    # See this task's Completion report, "Contract changes", for the rationale.

    def list_all(self) -> list[Client]:
        stmt = select(Client).order_by(Client.name)
        return list(self._session.scalars(stmt))

    def get(self, client_id: str) -> Client | None:
        return self._session.get(Client, client_id)

    # --- Added by task-07 (importer): backfilling several artifacts for the
    # same domain (e.g. an `_ahrefs` run and a `_gsc` run) must resolve to
    # the same `Client` row rather than creating a duplicate on every
    # artifact, and there is no existing lookup keyed on the unique
    # `domain` column. See this task's Completion report, "Contract
    # changes", for the rationale.

    def get_by_domain(self, domain: str) -> Client | None:
        stmt = select(Client).where(Client.domain == domain)
        return self._session.scalars(stmt).one_or_none()

    def create(
        self,
        *,
        name: str,
        slug: str,
        domain: str,
        market: str | None = None,
        timezone: str = "America/New_York",
        enabled: bool = True,
        status: ClientStatus = ClientStatus.active,
    ) -> Client:
        client = Client(
            name=name,
            slug=slug,
            domain=domain,
            market=market,
            timezone=timezone,
            enabled=enabled,
            # `status_changed_at` лишається NULL: створення — не зміна статусу.
            status=status,
        )
        self._session.add(client)
        self._session.flush()
        return client

    def update(self, client_id: str, **fields: Any) -> Client:
        client = self._session.get(Client, client_id)
        if client is None:
            raise LookupError(f"client not found: {client_id}")
        for key, value in fields.items():
            setattr(client, key, value)
        client.updated_at = utcnow()
        self._session.flush()
        return client

    def set_logo(self, client_id: str, logo_path: str | None) -> Client:
        return self.update(client_id, logo_path=logo_path)

    # --- Added by task-34: єдина точка запису статусу. `update()` навмисно
    # лишається «тупим» сеттером полів, а дату зміни ставить тільки цей
    # метод — інакше `status_changed_at` зсувався б з двох різних місць.

    def set_status(
        self,
        client_id: str,
        status: ClientStatus,
        *,
        note: str | None = None,
        now: datetime | None = None,
    ) -> Client:
        client = self._session.get(Client, client_id)
        if client is None:
            raise LookupError(f"client not found: {client_id}")
        # Дату зсуваємо лише за фактичної зміни: повторне «Paused → Paused»
        # (наприклад, оператор просто виправив нотатку) не має переписувати
        # «paused since», бо клієнт на паузі з першого разу.
        if client.status != status:
            client.status = status
            client.status_changed_at = now or utcnow()
        cleaned = (note or "").strip()
        client.status_note = cleaned or None
        client.updated_at = utcnow()
        self._session.flush()
        return client

    def upsert_integration(
        self,
        client_id: str,
        provider: Provider,
        *,
        enabled: bool = True,
        source_mode: SourceMode = SourceMode.auto,
        config: dict[str, Any] | None = None,
    ) -> ClientIntegration:
        integration = self.get_integration(client_id, provider)
        if integration is None:
            integration = ClientIntegration(
                client_id=client_id,
                provider=provider,
                enabled=enabled,
                source_mode=source_mode,
                config_json=config or {},
            )
            self._session.add(integration)
        else:
            integration.enabled = enabled
            integration.source_mode = source_mode
            integration.config_json = config or {}
            integration.updated_at = utcnow()
        self._session.flush()
        return integration

    def get_integration(self, client_id: str, provider: Provider) -> ClientIntegration | None:
        stmt = select(ClientIntegration).where(
            ClientIntegration.client_id == client_id,
            ClientIntegration.provider == provider,
        )
        return self._session.scalars(stmt).one_or_none()
