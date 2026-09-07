"""Content fingerprint (task-59 §0.2/§0.3): decides whether a page's
ARCHIVED state actually changed, independent of the raw bytes a given
request happened to return.

Live-measured 2026-09-04 against real client sites: two back-to-back
fetches of the SAME page returned different raw bytes on 5 of 7 sites —
a Cloudflare Zaraz `nonce`, a `cf-ray`-derived value, a rating widget
minting a fresh `id="asrg5-<rand>"` per render, a WP Fastest Cache
build-timestamp comment. Naive `sha256(raw_body)` dedup would write a full
new copy on 5/7 sites on EVERY capture (~8x the storage this design
actually needs). This fingerprint was verified stable 7/7 on that same
sample, while still changing for every one of 5 tested real edits: a
visible-text word, `<title>`, an `<img src>`, a newly added `<a href>`, and
a `<meta content>` value.

Known, deliberate blind spot: content living INSIDE an inline `<script>`
template body (e.g. an Elementor loop template rendered client-side) never
enters the fingerprint — that is exactly where the volatile per-render
noise above lives, and there is no way to tell "real edit inside a script
string" from "random per-render state inside a script string" without
executing the script. A change that only ever manifests through JS is
outside what a static-HTML archive can detect as a content change (the
captured BYTES themselves are still stored and replayable unchanged either
way — this only affects whether the archive writes a NEW snapshot row for
an otherwise-identical capture).
"""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser

# Tags whose body text never enters the fingerprint — only their own `src`
# attribute, since swapping which script/stylesheet is loaded IS a real
# change. This is where the volatile per-render noise above lives.
_SKIP_BODY_TAGS = frozenset({"script", "style"})

# Allowlist, not a denylist: an attribute has to earn its way in. `id` is
# deliberately excluded (see module docstring); so are `nonce`, `integrity`,
# `crossorigin`, `data-*` tracking/analytics hooks, and event handlers —
# none of them are "the page" in the sense an operator means by "it changed".
_KEPT_ATTRS = frozenset(
    {
        "href", "src", "srcset", "alt", "title", "rel", "type", "name", "content",
        "class", "action", "value", "media", "sizes", "lang", "role",
        "data-src", "data-lazy-src", "data-bg", "poster",
    }
)

# Hex-ish tokens (nonces, ray ids, per-render element ids) and long numeric
# ids get collapsed to a single placeholder rather than dropped outright —
# collapsing (not dropping) still lets an attribute whose VALUE genuinely
# changed shape (e.g. a real new numeric id scheme) show up as a token-count
# difference elsewhere, while erasing exactly the per-request noise measured
# live.
_HEXISH_RE = re.compile(
    r"(?i)\b(?:[0-9a-f]{8,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b"
)
# task-82 §3: a base64-encoded per-render token (Gravity Forms' own
# `gform_currency` hidden-field value is exactly this — an encrypted
# payload, freshly minted on every render) uses the FULL base64 alphabet
# (`+`, `/`, mixed case), which `_HEXISH_RE` above cannot see at all —
# `[0-9a-f]{8,}` only matches hex. Applied BEFORE `_HEXISH_RE` so a
# shorter, pure-hex nonce elsewhere in the same value still gets collapsed
# by that regex afterward instead of surviving as a leftover fragment.
_B64ISH_RE = re.compile(r"\b[A-Za-z0-9+/]{24,}={0,2}(?![A-Za-z0-9+/=])")
_LONGNUM_RE = re.compile(r"\b\d{9,}\b")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_value(value: str) -> str:
    value = _B64ISH_RE.sub("#", value)
    value = _HEXISH_RE.sub("#", value)
    value = _LONGNUM_RE.sub("#", value)
    return _WHITESPACE_RE.sub(" ", value).strip()


class _FingerprintParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_BODY_TAGS:
            self._skip_depth += 1
            src = dict(attrs).get("src")
            self.tokens.append(f"<{tag} src={_normalize_value(src or '')}>")
            return
        attrs_dict = dict(attrs)
        is_hidden_input = tag == "input" and (attrs_dict.get("type") or "").lower() == "hidden"
        kept = sorted(
            (name, _normalize_value(value or ""))
            for name, value in attrs
            if name in _KEPT_ATTRS
            # A hidden input's `value` is FORM STATE (a CSRF token, an
            # encrypted per-render payload like Gravity Forms' own
            # `gform_currency`) rather than content — task-82 §0.3,
            # live-measured as the one noise source present in ALL 5
            # consecutive same-content snapshot pairs of one client's
            # homepage. A VISIBLE input's `value` (a pre-filled field, a
            # button label) still enters the fingerprint — it's something
            # a visitor actually sees.
            and not (is_hidden_input and name == "value")
        )
        self.tokens.append("<" + tag + "".join(f" {name}={value}" for name, value in kept) + ">")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_BODY_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        self.tokens.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = _WHITESPACE_RE.sub(" ", data).strip()
        if text:
            self.tokens.append(_normalize_value(text))


def fingerprint_tokens(html: str) -> list[str]:
    """The normalized token stream itself — exposed mainly for tests that
    want to assert WHICH token differs, not just that the hash does.
    """
    parser = _FingerprintParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup: keep whatever parsed so far
        pass
    return parser.tokens


def content_fingerprint(html: str) -> str:
    tokens = fingerprint_tokens(html)
    return hashlib.sha256("\n".join(tokens).encode("utf-8")).hexdigest()
