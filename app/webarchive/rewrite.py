"""Replay-time URL rewriting (task-59 §1.5).

Sub-resource references are rewritten ON EVERY RENDER, never at capture
time — the stored bytes stay byte-for-byte identical to what was fetched,
forever. A rewrite-rule fix (a missed attribute, a new markup pattern)
therefore applies to the whole archive retroactively instead of requiring a
re-capture of the portfolio.

Regex-based, not a full HTML re-serialization: this replaces only the URL
INSIDE an attribute value it already recognizes, leaving quoting, attribute
order and everything else byte-identical to what was captured.

`<script>`/`<style>` element BODIES are handled specially and NEVER run
through the plain attribute regex: a naive whole-document `href\\s*=\\s*"..."`
scan would just as happily match inline JS like `link.href = "https://..."`
sitting inside a `<script>` tag, silently corrupting the script. Their
bodies are stashed out before the attribute pass and restored after —
`<style>` bodies get `rewrite_css`, `<script>` bodies are left completely
alone (live network calls FROM a script are handled at runtime by
`archive_replay.py`'s injected shim, not rewritten statically here).

`<iframe>`/`<frame>` tags get the SAME stash treatment, but for the
opposite reason (task-60 §2): the user's own decision, backed by measured
data (task-60 plan §0.6/§0.7), is to leave a real embed (a map/video/audio
widget) pointing at its live host instead of archiving it — archiving one
is slower than the page itself AND trips antibot, and almost all of them
are maps that "just work" against the live site. So these tags are handed
back byte-for-byte, UNREWRITTEN, rather than protected-then-rewritten like
script/style. The one exception is a known analytics/tracking beacon host
(`app.webarchive.urls.is_analytics_iframe_src`) — GTM/GA/Meta-pixel iframes
stay on the normal rewrite path below and 404 at replay, because loading
one for real on every archived-snapshot view would send a live hit into
the CLIENT's own GA/Meta account.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from app.webarchive.urls import is_analytics_iframe_src, resolve

_PLACEHOLDER = "\x00WEBARCHIVE-{index}\x00"
_PLACEHOLDER_RE = re.compile(r"\x00WEBARCHIVE-(\d+)\x00")

_SCRIPT_OR_STYLE_RE = re.compile(
    r"(<(script|style)\b[^>]*>)(.*?)(</\2\s*>)", re.IGNORECASE | re.DOTALL
)
_IFRAME_ELEMENT_RE = re.compile(r"<iframe\b[^>]*>.*?</iframe\s*>", re.IGNORECASE | re.DOTALL)
_FRAME_TAG_RE = re.compile(r"<frame\b[^>]*/?>", re.IGNORECASE)
_IFRAME_SRC_RE = re.compile(r"""\bsrc\s*=\s*(["'])([^"']*)\1""", re.IGNORECASE)

# `\b` in front of the attribute-name alternation is enough to keep e.g.
# `data-src` from being (mis)matched as a bare `src` occurrence: the regex
# engine finds the LEFTMOST position with any match at all, which for
# `data-src="..."` is the `d`, where only the `data-src` alternative can
# match — `src` cannot match starting mid-word.
_ATTR_RE = re.compile(
    r"""\b(?P<attr>href|src|poster|action|data-src|data-lazy-src|data-bg)
        \s*=\s*(?P<quote>["'])(?P<value>[^"']*)(?P=quote)""",
    re.IGNORECASE | re.VERBOSE,
)
_SRCSET_RE = re.compile(
    r"""\bsrcset\s*=\s*(?P<quote>["'])(?P<value>[^"']*)(?P=quote)""", re.IGNORECASE
)
_STYLE_ATTR_RE = re.compile(
    r"""\bstyle\s*=\s*(?P<quote>["'])(?P<value>[^"']*)(?P=quote)""", re.IGNORECASE
)
_META_REFRESH_RE = re.compile(
    r"""(<meta[^>]+http-equiv\s*=\s*["']refresh["'][^>]*content\s*=\s*["'][^"';]*;\s*url=)"""
    r"""([^"';]+)""",
    re.IGNORECASE,
)
_BASE_TAG_RE = re.compile(r"<base\b[^>]*>\s*", re.IGNORECASE)
_INTEGRITY_RE = re.compile(r"""\s+integrity\s*=\s*["'][^"']*["']""", re.IGNORECASE)
_CROSSORIGIN_RE = re.compile(r"""\s+crossorigin(?:\s*=\s*["'][^"']*["'])?""", re.IGNORECASE)
_CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)(?!data:)([^)'"]+)\1\s*\)""", re.IGNORECASE)


def replay_url(absolute_url: str, *, ts: str) -> str:
    """`/archive/r/<ts>/<absolute url>` — readable on purpose, matching
    Wayback's own convention: the original URL survives verbatim in the
    replay path instead of being opaquely encoded.
    """
    return f"/archive/r/{ts}/{absolute_url}"


def _rewrite_reference(raw_value: str, *, base_url: str, ts: str) -> str:
    stripped = raw_value.strip()
    if not stripped:
        return raw_value
    if stripped.startswith("/archive/r/"):
        return raw_value  # already rewritten (defensive; shouldn't normally recur)
    absolute = resolve(base_url, stripped)
    if absolute is None:
        return raw_value
    return replay_url(absolute, ts=ts)


def _rewrite_tag_attrs(text: str, *, base_url: str, ts: str) -> str:
    def _attr_sub(match: re.Match[str]) -> str:
        new_value = _rewrite_reference(match.group("value"), base_url=base_url, ts=ts)
        return f"{match.group('attr')}={match.group('quote')}{new_value}{match.group('quote')}"

    def _srcset_sub(match: re.Match[str]) -> str:
        rewritten_candidates = []
        for candidate in match.group("value").split(","):
            candidate = candidate.strip()
            if not candidate:
                continue
            bits = candidate.split(" ", 1)
            url = _rewrite_reference(bits[0], base_url=base_url, ts=ts)
            descriptor = f" {bits[1]}" if len(bits) > 1 else ""
            rewritten_candidates.append(f"{url}{descriptor}")
        joined = ", ".join(rewritten_candidates)
        return f"srcset={match.group('quote')}{joined}{match.group('quote')}"

    def _style_sub(match: re.Match[str]) -> str:
        rewritten = rewrite_css(match.group("value"), base_url=base_url, ts=ts)
        return f"style={match.group('quote')}{rewritten}{match.group('quote')}"

    def _refresh_sub(match: re.Match[str]) -> str:
        return f"{match.group(1)}{_rewrite_reference(match.group(2), base_url=base_url, ts=ts)}"

    out = _ATTR_RE.sub(_attr_sub, text)
    out = _SRCSET_RE.sub(_srcset_sub, out)
    out = _STYLE_ATTR_RE.sub(_style_sub, out)
    out = _META_REFRESH_RE.sub(_refresh_sub, out)
    out = _BASE_TAG_RE.sub("", out)
    out = _INTEGRITY_RE.sub("", out)
    out = _CROSSORIGIN_RE.sub("", out)
    return out


def _iframe_absolute_src(tag_text: str, *, base_url: str) -> str | None:
    """The absolute `src` an `<iframe>`/`<frame>` tag's raw text resolves
    to, or `None` for anything with no usable `src` at all — including the
    ~1,521-portfolio-wide `src="about:blank"` lazy-load placeholders
    (`resolve` excludes the `about:` scheme), which is exactly right: an
    empty placeholder has nothing live to leave alone or block either way.
    """
    src_match = _IFRAME_SRC_RE.search(tag_text)
    if src_match is None:
        return None
    return resolve(base_url, src_match.group(2))


def rewrite_css(css_text: str, *, base_url: str, ts: str) -> str:
    """Rewrite every `url(...)` in a CSS document or an inline `style="..."`
    value. Safe to run on raw CSS text directly — `url()` syntax is
    unambiguous and does not appear as a false positive inside ordinary CSS.
    """

    def _sub(match: re.Match[str]) -> str:
        quote_char = match.group(1) or ""
        new_value = _rewrite_reference(match.group(2), base_url=base_url, ts=ts)
        return f"url({quote_char}{new_value}{quote_char})"

    return _CSS_URL_RE.sub(_sub, css_text)


def rewrite_html(
    html: str,
    *,
    base_url: str,
    ts: str,
    live_iframes: bool = True,
    analytics_hosts: frozenset[str] | None = None,
) -> str:
    """Rewrite an HTML document for replay at `ts` (14-digit
    `app.webarchive.replay.format_ts` form), resolving every relative
    reference against `base_url` (the page's own original absolute URL).

    `live_iframes` is `Settings.archive_live_iframes` (task-60 §2) — the
    kill-switch for leaving embeds live; `False` reproduces the pre-task-60
    behavior of rewriting (and thus 404ing) every iframe, no exceptions.
    `analytics_hosts` is `Settings.archive_iframe_analytics_hosts`; `None`
    falls back to `is_analytics_iframe_src`'s own built-in default.
    """
    placeholders: list[str] = []

    def _stash_iframe(match: re.Match[str]) -> str:
        whole = match.group(0)
        absolute_src = _iframe_absolute_src(whole, base_url=base_url)
        if absolute_src is not None and is_analytics_iframe_src(
            absolute_src, hosts=analytics_hosts
        ):
            # Analytics beacons fall through to the normal rewrite path
            # below, which 404s them at replay — see module docstring.
            return whole
        # A live embed, or an inert `about:blank` placeholder: handed back
        # byte-for-byte, UNREWRITTEN — see module docstring.
        index = len(placeholders)
        placeholders.append(whole)
        return _PLACEHOLDER.format(index=index)

    def _stash(match: re.Match[str]) -> str:
        open_tag, tag_name, body, close_tag = match.groups()
        # The script/style tag's OWN attributes (e.g. `<script src="...">`,
        # `<link rel=stylesheet>` handled elsewhere) still get rewritten —
        # only the ELEMENT BODY is protected from the attribute pass below.
        open_tag = _rewrite_tag_attrs(open_tag, base_url=base_url, ts=ts)
        if tag_name.lower() == "style":
            body = rewrite_css(body, base_url=base_url, ts=ts)
        # <script> bodies are left untouched: see module docstring.
        index = len(placeholders)
        placeholders.append(f"{open_tag}{body}{close_tag}")
        return _PLACEHOLDER.format(index=index)

    stashed = html
    if live_iframes:
        stashed = _IFRAME_ELEMENT_RE.sub(_stash_iframe, stashed)
        stashed = _FRAME_TAG_RE.sub(_stash_iframe, stashed)
    stashed = _SCRIPT_OR_STYLE_RE.sub(_stash, stashed)
    rewritten = _rewrite_tag_attrs(stashed, base_url=base_url, ts=ts)

    def _restore(match: re.Match[str]) -> str:
        return placeholders[int(match.group(1))]

    return _PLACEHOLDER_RE.sub(_restore, rewritten)


def live_iframe_hosts(
    html: str,
    *,
    base_url: str,
    live_iframes: bool = True,
    analytics_hosts: frozenset[str] | None = None,
) -> list[str]:
    """Distinct hostnames of the iframes a replayed snapshot loads LIVE
    (task-60 §2.3) — everything `rewrite_html`'s iframe stash also leaves
    live, computed independently here (no new DB column: this task adds no
    migration) so the Meta panel can show it without re-rendering the page.
    Sorted, for a stable UI and stable test assertions.
    """
    if not live_iframes:
        return []
    hosts: set[str] = set()
    for pattern in (_IFRAME_ELEMENT_RE, _FRAME_TAG_RE):
        for match in pattern.finditer(html):
            absolute = _iframe_absolute_src(match.group(0), base_url=base_url)
            if absolute is None or is_analytics_iframe_src(absolute, hosts=analytics_hosts):
                continue
            hostname = urlsplit(absolute).hostname
            if hostname:
                hosts.add(hostname)
    return sorted(hosts)
