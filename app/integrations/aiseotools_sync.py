"""Discover non-secret provider references exposed by AISeoTools.

Secrets and authenticated browser state remain in AISeoTools.  This module
only stores the client-specific identifiers needed by the dashboard worker:
domains, Reporting Ninja connection/account IDs, and browser profile names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.db.repositories.clients import ClientRepository
from app.importers.client_mapping import normalize_domain
from app.schemas.enums import Provider

_GA4_ACCOUNT_NAMES: dict[str, str] = {
    "19cleveland.com": "19 Cleveland - GA4",
    "exxxoticaexpo.com": "EXXXOTICA Expo - Homepage - GA4",
    "effyscafe.com": "Effys Cafe",
    "grillpointnyc.com": "Grill Point NYC",
    "hearwellservices.com": "Hear Well Services",
    "nychearing.com": "NYC Hearing Associate",
    "nysapphire.com": "http://www.nysapphire.com - GA4",
    "pjclarkes.com": "PJ Clarke's - GA4",
    "shawarma-mia.com": "Shawarma-Mia",
    "shorelinedentalstudio.com": "Shoreline Dental Studio GA4",
    "smashhouseburgers.com": "Smash House Burgers",
}


@dataclass
class IntegrationSyncReport:
    configured: int = 0
    warnings: list[str] = field(default_factory=list)


def _accounts_by_domain(connections: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for connection in connections:
        connection_key = str(connection.get("connection_key") or "")
        for account in connection.get("accounts") or []:
            account_id = str(account.get("account_id") or "")
            domain = normalize_domain(account_id)
            if connection_key and account_id and domain:
                found.setdefault(domain, (connection_key, account_id))
    return found


def _accounts_by_name(connections: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for connection in connections:
        connection_key = str(connection.get("connection_key") or "")
        for account in connection.get("accounts") or []:
            account_id = str(account.get("account_id") or "")
            account_name = str(account.get("account_name") or "").strip().casefold()
            if connection_key and account_id and account_name:
                found.setdefault(account_name, (connection_key, account_id))
                # Reporting Ninja currently appends the numeric property id
                # to the human account name, e.g. ``Client GA4 (123456)``.
                # Match the stable name while still retaining the exact ID.
                stable_name = re.sub(r"\s+\(\d+\)$", "", account_name)
                found.setdefault(stable_name, (connection_key, account_id))
    return found


def sync_ai_seo_tools_integrations(
    *, session_factory: sessionmaker[Session], allowed_domains: set[str]
) -> IntegrationSyncReport:
    report = IntegrationSyncReport()
    gsc_accounts: dict[str, tuple[str, str]] = {}
    ga4_accounts: dict[str, tuple[str, str]] = {}

    try:
        from seo_audit.clientreport.api_client import ReportingNinjaClient

        with ReportingNinjaClient() as client:
            gsc_accounts = _accounts_by_domain(client.list_connections("google_search_console"))
            ga4_accounts = _accounts_by_name(client.list_connections("ga4"))
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
                    "profile_store": "semrush",
                },
            )
            report.configured += 1

            if gsc_ref := gsc_accounts.get(client.domain):
                connection_key, account_id = gsc_ref
                clients.upsert_integration(
                    client.id,
                    Provider.gsc,
                    config={
                        "connection_key": connection_key,
                        "account_id": account_id,
                        "property": account_id,
                        "profile": "gsc",
                        "profile_store": "semrush",
                    },
                )
                report.configured += 1

            ga4_name = _GA4_ACCOUNT_NAMES.get(client.domain)
            ga4_ref = ga4_accounts.get(ga4_name.casefold()) if ga4_name else None
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
