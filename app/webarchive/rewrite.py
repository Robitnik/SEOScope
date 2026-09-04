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
"""

from __future__ import annotations

import re

from app.webarchive.urls import resolve

_PLACEHOLDER = "\x00WEBARCHIVE-{index}\x00"
_PLACEHOLDER_RE = re.compile(r"\x00WEBARCHIVE-(\d+)\x00")

_SCRIPT_OR_STYLE_RE = re.compile(
    r"(<(script|style)\b[^>]*>)(.*?)(</\2\s*>)", re.IGNORECASE | re.DOTALL
)

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


def rewrite_html(html: str, *, base_url: str, ts: str) -> str:
    """Rewrite an HTML document for replay at `ts` (14-digit
    `app.webarchive.replay.format_ts` form), resolving every relative
    reference against `base_url` (the page's own original absolute URL).
    """
    placeholders: list[str] = []

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

    stashed = _SCRIPT_OR_STYLE_RE.sub(_stash, html)
    rewritten = _rewrite_tag_attrs(stashed, base_url=base_url, ts=ts)

    def _restore(match: re.Match[str]) -> str:
        return placeholders[int(match.group(1))]

    return _PLACEHOLDER_RE.sub(_restore, rewritten)
