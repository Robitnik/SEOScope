"""DNS-over-HTTPS client (task-35 §1.6/§3.3): no `dnspython` in the image, so
plain `urllib.request` against two independent JSON-DoH resolvers stands in
for a real resolver library. Two resolvers also means "what the world sees",
not just what this one container's resolver returns.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote

from app.tools.http_safety import USER_AGENT

DOH_RESOLVERS: tuple[str, ...] = ("dns.google", "cloudflare-dns.com")
RECORD_TYPES: tuple[str, ...] = ("A", "AAAA", "CNAME", "NS", "MX", "TXT", "CAA")

_RESOLVER_ENDPOINT = {
    "dns.google": "https://dns.google/resolve",
    "cloudflare-dns.com": "https://cloudflare-dns.com/dns-query",
}
_RECORD_TYPE_NUM = {"A": 1, "NS": 2, "CNAME": 5, "MX": 15, "TXT": 16, "AAAA": 28, "CAA": 257}


@dataclass(frozen=True)
class DnsQueryResult:
    record_type: str
    values: tuple[str, ...]
    ttl: int | None
    resolver: str | None  # None only when every configured resolver failed
    available: bool


def _clean_value(record_type: str, raw: str) -> str:
    value = raw.strip()
    if record_type == "TXT" and len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if record_type in {"CNAME", "NS", "MX"} and value.endswith("."):
        value = value[:-1]
    return value


def _query_resolver(
    resolver_host: str, name: str, record_type: str, *, timeout: float
) -> tuple[list[str], int | None] | None:
    endpoint = _RESOLVER_ENDPOINT[resolver_host]
    url = f"{endpoint}?name={quote(name)}&type={record_type}"
    request = urllib.request.Request(
        url, headers={"Accept": "application/dns-json", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed DoH hosts, not user input
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None

    if payload.get("Status") not in (
        0,
        3,
    ):  # 0 = NOERROR, 3 = NXDOMAIN (a real, valid answer: "no records")
        return None

    wanted = _RECORD_TYPE_NUM[record_type]
    answers = [a for a in payload.get("Answer", []) if a.get("type") == wanted]
    values = sorted({_clean_value(record_type, a["data"]) for a in answers if a.get("data")})
    ttl = min((int(a["TTL"]) for a in answers if "TTL" in a), default=None)
    return values, ttl


def resolve(
    name: str,
    record_type: str,
    *,
    resolvers: tuple[str, ...] = DOH_RESOLVERS,
    timeout: float = 10.0,
) -> DnsQueryResult:
    """First resolver to answer wins; only `available=False` when every
    configured resolver failed — that is `dns_unavailable` (`warning`), never
    silently treated as "the records are gone" (PLAN §12).
    """
    for resolver_host in resolvers:
        result = _query_resolver(resolver_host, name, record_type, timeout=timeout)
        if result is not None:
            values, ttl = result
            return DnsQueryResult(
                record_type=record_type,
                values=tuple(values),
                ttl=ttl,
                resolver=resolver_host,
                available=True,
            )
    return DnsQueryResult(
        record_type=record_type, values=(), ttl=None, resolver=None, available=False
    )


def extract_google_site_verification_tokens(txt_values: tuple[str, ...]) -> list[str]:
    prefix = "google-site-verification="
    return sorted(v[len(prefix) :] for v in txt_values if v.startswith(prefix))
