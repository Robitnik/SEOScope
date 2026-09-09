"""Кого саме питати в Google для цього клієнта (task-88 §2.1).

Нічого не мапиться й не вгадується: `client_integrations.config_json`
["account_id"] **уже** лежить у форматі, який приймає Google API — це
зафіксовано вимірюванням task-83 §0.3 по всіх 15 клієнтах портфеля:

    gsc              account_id='sc-domain:hearwellservices.com' -> GSC siteUrl
    reporting_ninja  account_id='properties/395442288'           -> GA4 property

Транспорт вибирається тим самим `resolve_transport`, що в task-83/84, тож
клієнт без явного `config_json["transport"]` лишається на збережених
RN-даних і сторінка для нього не змінюється взагалі (§2.1, гейт G8).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ClientIntegration
from app.integrations.google_api import GOOGLE_API, Transport, resolve_transport
from app.schemas.enums import Provider


@dataclass(frozen=True)
class GoogleTarget:
    transport: Transport
    gsc_site_url: str | None
    ga4_property_id: str | None
    # Чому джерела немає — окремо для кожного боку, бо «GSC не підключений»
    # і «GA4 не підключений» це різні стани, і жоден із них не має гасити
    # інший (§2.5).
    gsc_detail: str | None
    ga4_detail: str | None

    @property
    def uses_google_api(self) -> bool:
        return self.transport == GOOGLE_API

    @property
    def has_any(self) -> bool:
        return bool(self.gsc_site_url or self.ga4_property_id)


def resolve_google_target(session: Session, *, client_id: str) -> GoogleTarget:
    rows = session.scalars(
        select(ClientIntegration).where(
            ClientIntegration.client_id == client_id,
            ClientIntegration.provider.in_([Provider.gsc, Provider.reporting_ninja]),
        )
    ).all()
    by_provider = {row.provider: row for row in rows}

    gsc = by_provider.get(Provider.gsc)
    ninja = by_provider.get(Provider.reporting_ninja)

    gsc_site, gsc_detail = _account_id(gsc, "Search Console")
    ga4_property, ga4_detail = _account_id(ninja, "GA4")

    # Транспорт беремо з GA4-інтеграції, а якщо її немає — з GSC: обидві
    # їдуть одним ключем `transport`, і мати сторінку, де половина панелей
    # із Google, а половина зі сховища, було б гірше за будь-який із двох
    # чистих варіантів.
    config = (ninja.config_json if ninja else None) or (gsc.config_json if gsc else None)
    return GoogleTarget(
        transport=resolve_transport(config),
        gsc_site_url=gsc_site,
        ga4_property_id=ga4_property,
        gsc_detail=gsc_detail,
        ga4_detail=ga4_detail,
    )


def _account_id(
    integration: ClientIntegration | None, label: str
) -> tuple[str | None, str | None]:
    if integration is None:
        return None, f"{label} isn't connected for this client."
    if not integration.enabled:
        return None, f"{label} is connected but disabled for this client."
    account_id = str((integration.config_json or {}).get("account_id") or "").strip()
    if not account_id:
        return None, f"{label} has no account id configured."
    return account_id, None
