"""Discover non-secret provider references exposed by AISeoTools.

Secrets and authenticated browser state remain in AISeoTools.  This module
only stores the client-specific identifiers needed by the dashboard worker:
domains, Reporting Ninja connection/account IDs, and browser profile names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.db.repositories.clients import ClientRepository
from app.integrations.discovery import (
    GA4_ACCOUNT_ALIASES,
    GA4_INTEGRATION_ID,
    GSC_INTEGRATION_ID,
    accounts_by_domain,
    accounts_by_name,
    best_ga4_account,
)
from app.schemas.enums import Provider, SourceMode


@dataclass
class IntegrationSyncReport:
    configured: int = 0
    warnings: list[str] = field(default_factory=list)


def sync_ai_seo_tools_integrations(
    *, session_factory: sessionmaker[Session], allowed_domains: set[str]
) -> IntegrationSyncReport:
    report = IntegrationSyncReport()
    gsc_accounts: dict[str, tuple[str, str]] = {}
    ga4_accounts: dict[str, tuple[str, str]] = {}
    ga4_connections: list[dict[str, Any]] = []

    try:
        from seo_audit.clientreport.api_client import ReportingNinjaClient

        with ReportingNinjaClient() as client:
            gsc_accounts = accounts_by_domain(client.list_connections(GSC_INTEGRATION_ID))
            ga4_connections = list(client.list_connections(GA4_INTEGRATION_ID))
            ga4_accounts = accounts_by_name(ga4_connections)
    except Exception as exc:  # optional discovery must not block history/Ahrefs/Semrush
        report.warnings.append(f"Reporting Ninja discovery unavailable: {type(exc).__name__}")

    session = session_factory()
    try:
        clients = ClientRepository(session)
        for client in clients.list_active():
            if client.domain not in allowed_domains:
                continue

            market = (client.market or "us").lower()
            clients.upsert_integration(
                client.id,
                Provider.ahrefs,
                config={
                    "domain": client.domain,
                    "market": client.market or "",
                    "ahrefs_client_label": client.name,
                },
            )
            report.configured += 1
            clients.upsert_integration(
                client.id,
                Provider.semrush,
                config={
                    "domain": client.domain,
                    "database": market,
                    "profile": "semrush",
                },
            )
            report.configured += 1

            if gsc_ref := gsc_accounts.get(client.domain):
                connection_key, account_id = gsc_ref
                clients.upsert_integration(
                    client.id,
                    Provider.gsc,
                    source_mode=SourceMode.api,
                    config={
                        "connection_key": connection_key,
                        "account_id": account_id,
                        "property": account_id,
                        "profile": "gsc",
                    },
                )
                report.configured += 1

            # Аліас — точний override; якщо його немає (а для кожного нового
            # клієнта його немає), падаємо на той самий матчер за назвою, що й
            # автопідключення з UI. Саме через відсутність fallback'у раніше
            # ALPHA/LAXTON/GOREPORT лишалися без GA4 (task-33 §1.3).
            ga4_name = GA4_ACCOUNT_ALIASES.get(client.domain)
            ga4_ref = ga4_accounts.get(ga4_name.casefold()) if ga4_name else None
            if ga4_ref is None and ga4_connections:
                ga4_ref = best_ga4_account(
                    ga4_connections, domain=client.domain, client_name=client.name
                )
            if ga4_ref:
                connection_key, account_id = ga4_ref
                clients.upsert_integration(
                    client.id,
                    Provider.reporting_ninja,
                    config={"connection_key": connection_key, "account_id": account_id},
                )
                report.configured += 1

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return report
