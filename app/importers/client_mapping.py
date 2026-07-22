"""AISeoTools `clients.json` -> client/domain mapping (task-07).

Builds a `code -> domain` map for conflict/ambiguity detection *before* any
DB write happens (PLAN §11: "показує конфлікти мапінгу... перед записом").
Ahrefs/Semrush have no representation in `clients.json` at all — confirmed
against the AISeoTools source, those pipelines take a bare domain argument
and never consult this file — so this module only ever maps `gsc`/`ga4`
blocks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class ClientEntry:
    code: str
    domain: str | None
    gsc_account_id: str | None
    ga4_account_id: str | None
    connection_key: str | None
    ninja_report_name: str | None


@dataclass
class ClientMap:
    entries: dict[str, ClientEntry] = field(default_factory=dict)  # code -> entry
    # normalized domain -> codes claiming it:
    domain_to_codes: dict[str, list[str]] = field(default_factory=dict)
    conflicts: dict[str, list[str]] = field(default_factory=dict)  # domain -> conflicting codes
    unmappable_codes: list[str] = field(default_factory=list)  # configured, no derivable domain


def normalize_domain(raw: str) -> str | None:
    """`sc-domain:x.com` / `https://x.com/` / `x.com` / `www.x.com` -> `x.com`."""
    value = (raw or "").strip()
    if not value:
        return None
    if value.startswith("sc-domain:"):
        value = value[len("sc-domain:") :]
    elif "://" in value:
        value = urlparse(value).netloc or value
    value = value.strip("/").lower()
    if value.startswith("www."):
        value = value[4:]
    return value or None


def normalize_code(code: str) -> str:
    """Comparable form for a `clients.json` key vs a
    `client_report_<CODE>_...` filename fragment — both collapse
    whitespace/dashes to a single dash (`"SAPPHIRE NEW YORK"` and
    `"SAPPHIRE-NEW-YORK"` must compare equal).
    """
    return re.sub(r"[\s\-]+", "-", code.strip().upper())


def load_client_map(source_root: Path) -> ClientMap:
    path = source_root / "clients.json"
    if not path.is_file():
        return ClientMap()
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

    entries: dict[str, ClientEntry] = {}
    domain_to_codes: dict[str, list[str]] = {}
    unmappable: list[str] = []

    for code, block in raw.items():
        if not isinstance(block, dict):
            continue
        gsc_raw = block.get("gsc")
        gsc: dict[str, Any] = gsc_raw if isinstance(gsc_raw, dict) else {}
        ga4_raw = block.get("ga4")
        ga4: dict[str, Any] = ga4_raw if isinstance(ga4_raw, dict) else {}
        gsc_account_id = gsc.get("account_id")
        domain = normalize_domain(gsc_account_id) if gsc_account_id else None
        connection_key = gsc.get("connection_key") or ga4.get("connection_key")

        entries[code] = ClientEntry(
            code=code,
            domain=domain,
            gsc_account_id=gsc_account_id,
            ga4_account_id=ga4.get("account_id"),
            connection_key=connection_key,
            ninja_report_name=block.get("ninja_report_name"),
        )
        if domain is None:
            if gsc_account_id or ga4.get("account_id"):
                unmappable.append(code)
            continue
        domain_to_codes.setdefault(domain, []).append(code)

    conflicts = {domain: codes for domain, codes in domain_to_codes.items() if len(codes) > 1}
    return ClientMap(
        entries=entries,
        domain_to_codes=domain_to_codes,
        conflicts=conflicts,
        unmappable_codes=unmappable,
    )
