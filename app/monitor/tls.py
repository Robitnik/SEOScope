"""TLS certificate expiry check (task-35 §3.3 item 8): stdlib `ssl` only."""

from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from app.tools.http_safety import UnsafeUrlError, assert_public_http_url

_CERT_DATE_FORMAT = "%b %d %H:%M:%S %Y %Z"


@dataclass(frozen=True)
class TlsCertInfo:
    not_after: datetime | None
    issuer: str | None
    subject_alt_names: tuple[str, ...]
    error: str | None

    @property
    def available(self) -> bool:
        return self.error is None


def check_certificate(host: str, *, port: int = 443, timeout: float = 10.0) -> TlsCertInfo:
    try:
        assert_public_http_url(f"https://{host}")
    except UnsafeUrlError as exc:
        return TlsCertInfo(not_after=None, issuer=None, subject_alt_names=(), error=str(exc))

    context = ssl.create_default_context()
    try:
        with (
            socket.create_connection((host, port), timeout=timeout) as sock,
            context.wrap_socket(sock, server_hostname=host) as tls_sock,
        ):
            cert = cast("dict[str, Any] | None", tls_sock.getpeercert())
    except (ssl.SSLError, OSError, TimeoutError) as exc:
        return TlsCertInfo(not_after=None, issuer=None, subject_alt_names=(), error=str(exc))

    if not cert:
        return TlsCertInfo(
            not_after=None, issuer=None, subject_alt_names=(), error="no certificate returned"
        )

    not_after = None
    not_after_raw = cert.get("notAfter")
    if not_after_raw:
        try:
            not_after = datetime.strptime(not_after_raw, _CERT_DATE_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            pass

    issuer_parts = dict(part[0] for part in cert.get("issuer", ()))
    issuer = issuer_parts.get("organizationName") or issuer_parts.get("commonName")
    sans = tuple(value for key, value in cert.get("subjectAltName", ()) if key == "DNS")

    return TlsCertInfo(not_after=not_after, issuer=issuer, subject_alt_names=sans, error=None)


def days_until_expiry(not_after: datetime | None, *, now: datetime | None = None) -> int | None:
    if not_after is None:
        return None
    now = now or datetime.now(UTC)
    return (not_after - now).days
