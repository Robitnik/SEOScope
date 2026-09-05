"""URL -> client resolution (task-61 §0.6/§2.2).

`Client.domain` is UNIQUE, but the operator's URL frequently carries a
subdomain the row doesn't (`blog.nysapphire.com`, `shop.alphastyle.store`
are both real, live GSC/monitor hosts on this portfolio) — so an exact
`domain` match is tried first, then "the URL's host ends with
`.<domain>`". A client list of ~15 rows makes doing this in Python cheaper
to reason about than a `LIKE '%.' || domain` SQL clause, and avoids a
SQL-injection-shaped pattern for no real gain at this size.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.db.models import Client

ClientMatch = str  # "exact" | "subdomain" | "unknown_domain"

# Same detector `app.tools.redirects.input` uses: matches ANY explicit
# scheme prefix (`mailto:`, `javascript:`, `tel:`, ...), not just `http(s)://`
# — `javascript:alert(1)` has no `://`, so a naive "does it contain '://'"
# check would prepend `https://` and hide the real scheme entirely.
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


class UrlValidationError(ValueError):
    """Raised for input that isn't a checkable http(s) page URL at all.

    Same shape as `app.tools.redirects.input.UrlInputError` — a plain
    `ValueError` subclass carrying a stable `code`, not a frozen dataclass:
    an exception needs `Exception.__init__` to run so `str(exc)` and
    pickling behave normally.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code  # "empty" | "invalid_url" | "unsupported_scheme"


def parse_input_url(raw: str) -> str:
    """Validates and trims `raw` to something safe to carry through the rest
    of the pipeline. Raises `UrlValidationError` for anything that cannot be
    a page URL — never returns a partially-valid string."""
    candidate = (raw or "").strip()
    if not candidate:
        raise UrlValidationError("empty", "Paste a page URL to inspect.")
    if len(candidate) > 2048:
        raise UrlValidationError("invalid_url", "That URL is too long.")
    parts = urlsplit(candidate if _SCHEME.match(candidate) else f"https://{candidate}")
    if parts.scheme not in ("http", "https"):
        raise UrlValidationError(
            "unsupported_scheme", "Only http:// and https:// page URLs are supported."
        )
    if not parts.hostname:
        raise UrlValidationError("invalid_url", "That doesn't look like a URL with a host.")
    return parts.geturl()


@dataclass(frozen=True)
class ResolvedClient:
    client: Client | None
    match: ClientMatch


def resolve_client_for_url(url: str, clients: list[Client]) -> ResolvedClient:
    """`clients` is the small, already-loaded portfolio list (task-61 keeps
    this a pure function so tests never touch a session)."""
    host = (urlsplit(url).hostname or "").lower()
    bare = host[4:] if host.startswith("www.") else host

    for client in clients:
        domain = (client.domain or "").lower()
        if domain and (bare == domain or f"www.{bare}" == domain):
            return ResolvedClient(client, "exact")

    for client in clients:
        domain = (client.domain or "").lower()
        if domain and bare.endswith(f".{domain}"):
            return ResolvedClient(client, "subdomain")

    return ResolvedClient(None, "unknown_domain")
