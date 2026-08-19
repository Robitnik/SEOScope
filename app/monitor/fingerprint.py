"""Three-tier fingerprint (task-35 §1.3 — the key decision this task rests
on): a naive raw-bytes hash is nearly stable but not quite. Two requests to
`goreport.com` 2 seconds apart differ only in a Cloudflare per-request token
(`window.__CF$cv$params={r:'a296eb...'}`), which would otherwise fire a
change alert every single hour on a page nobody touched.

1. `signals_sha256` — canonical JSON of the extracted SEO signals. This is
   what "did it change?" and every severity rule actually means.
2. `text_sha256` — normalized visible text. Signals "the content was rewritten".
3. `raw_sha256` — masked raw bytes. Evidence only, never raises severity above
   `info` on its own.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# Known volatile-per-request noise, found live (task-35 §1.3): a Cloudflare
# per-request challenge token embedded inline in the page. Masked before
# hashing so identical *content* still fingerprints identically.
_CF_CV_PARAMS_RE = re.compile(rb"__CF\$cv\$params\s*=\s*\{[^}]*\}")
_NONCE_ATTR_RE = re.compile(rb'nonce=["\'][^"\']*["\']', re.IGNORECASE)
_CSRF_VALUE_RE = re.compile(
    rb'((?:csrf[-_]?token|_token)["\']?\s*[:=]\s*["\'])[^"\']*(["\'])', re.IGNORECASE
)
_CSRF_META_RE = re.compile(
    rb'(name=["\']csrf-token["\']\s+content=["\'])[^"\']*(["\'])', re.IGNORECASE
)
# Shopify per-request/per-session identifiers (task-50 §0.9 — live 2026-08-19,
# alphastyle.store): `<meta name="shopify-y"/"shopify-s" content="...">`
# carry a fresh UUID on every single request, not just every session. Two
# `urllib` fetches of the same page 2 seconds apart disagreed only here (and
# in the JWT below), which fired `content_changed` on effectively every
# monitor pass for this client regardless of whether anything real changed.
_SHOPIFY_META_RE = re.compile(
    rb'(name=["\']shopify-[ys]["\']\s+content=["\'])[^"\']*(["\'])', re.IGNORECASE
)
# A storefront customer/session JWT embedded inline (observed inside an
# account-link `href`) — three base64url segments, header always starting
# `eyJ` (base64 of `{"`). Anchoring on that prefix keeps this specific to
# actual JWTs rather than any arbitrary dotted token.
_JWT_RE = re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")


def mask_known_noise(raw: bytes) -> bytes:
    masked = _CF_CV_PARAMS_RE.sub(b"__CF$cv$params={MASKED}", raw)
    masked = _NONCE_ATTR_RE.sub(b'nonce="MASKED"', masked)
    masked = _CSRF_VALUE_RE.sub(rb"\1MASKED\2", masked)
    masked = _CSRF_META_RE.sub(rb"\1MASKED\2", masked)
    masked = _SHOPIFY_META_RE.sub(rb"\1MASKED\2", masked)
    masked = _JWT_RE.sub(b"MASKED.JWT.MASKED", masked)
    return masked


def raw_fingerprint(raw: bytes) -> str:
    return hashlib.sha256(mask_known_noise(raw)).hexdigest()


def text_fingerprint(normalized_text: str) -> str:
    return hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()


def _canonical_json_bytes(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def build_signals(
    *,
    status: int | None,
    final_url: str | None,
    meta_robots: str | None,
    x_robots_tag: str | None,
    canonical: str | None,
    title: str | None,
    description: str | None,
    h1: str | None,
    hreflang: list[tuple[str, str]],
    json_ld_types: list[str],
    analytics_ids: list[str],
    google_site_verifications: list[str],
    indexable: bool | None,
) -> dict[str, Any]:
    """Canonical, JSON-serializable signal set — this exact shape is both
    what gets stored as `MonitorPageVersion.signals_json` and what
    `signals_fingerprint` hashes, so the two are always in sync by
    construction.
    """
    return {
        "status": status,
        "final_url": final_url,
        "meta_robots": meta_robots,
        "x_robots_tag": x_robots_tag,
        "canonical": canonical,
        "title": title,
        "description": description,
        "h1": h1,
        "hreflang": sorted(f"{lang}:{href}" for lang, href in hreflang),
        "json_ld_types": sorted(json_ld_types),
        "analytics_ids": sorted(analytics_ids),
        "google_site_verifications": sorted(google_site_verifications),
        "indexable": indexable,
    }


def signals_fingerprint(signals: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(signals)).hexdigest()
