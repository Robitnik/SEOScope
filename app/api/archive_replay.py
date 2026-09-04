"""Web Archive replay (task-59 §1.5/§1.6/§4): `GET /archive/r/<ts>/<url>`.

Deliberately OUTSIDE `/api/v1` — this is what actually renders inside the
viewer's `<iframe sandbox>` (`#/archive/view`), and a short, readable path
matters there the same way it does on the real Wayback Machine.

The single most important property of this module: an unrewritten reference
must NEVER silently fall through to the live internet. Every response here
carries its OWN Content-Security-Policy with no external origin listed at
all (`app.core.security.SecurityHeadersMiddleware` only `setdefault`s its
own headers, so a header this module sets explicitly always wins) — a
rewriter gap means a blocked request and a row in `archive_replay_misses`,
never a live fetch of today's real page under yesterday's byline.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from starlette.datastructures import URL

from app.api import register_router
from app.core.config import get_settings
from app.db.repositories.web_archive import WebArchiveRepository
from app.db.session import worker_session_scope
from app.monitor.page import decode_body
from app.webarchive import rewrite as rewritemod
from app.webarchive import storage
from app.webarchive.replay import parse_ts
from app.webarchive.urls import normalize_url

router = APIRouter(tags=["web-archive-replay"])

# No external origin anywhere in this list — see module docstring. `data:`/
# `blob:` cover inline images/fonts a rewritten stylesheet may still embed
# that way; every actual HTTP(S) subresource must resolve through our own
# `/archive/r/...` path or it does not load at all.
_REPLAY_CSP = (
    "default-src 'self'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' data: blob:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-src 'self'; "
    "form-action 'none'; "
    "frame-ancestors 'self'; "
    "base-uri 'none'"
)
# The replay iframe has an OPAQUE origin (`Origin: null`, no
# `allow-same-origin`) — live-measured (task-59, 2026-09-04): without
# `Access-Control-Allow-Origin`, a real captured page's own `@font-face`
# (fonts are ALWAYS CORS-checked cross-origin, regardless of `crossorigin`
# attributes) and a `crossorigin` `<script>` (Cloudflare's own analytics
# beacon) both failed to load from that opaque origin, even though the
# target is this SAME server. `*` is safe here: every response the origin
# can request is already scoped to `/archive/r/...`/`/archive/static/...`
# by our own routing, carries no credentials (the sandbox has none to
# send), and is the same archived, already-public page content either way.
_ACCESS_CONTROL_ALLOW_ORIGIN = {"Access-Control-Allow-Origin": "*"}
_REPLAY_HEADERS = {
    "Content-Security-Policy": _REPLAY_CSP,
    "X-Frame-Options": "SAMEORIGIN",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "public, max-age=31536000, immutable",
    **_ACCESS_CONTROL_ALLOW_ORIGIN,
}
_MISS_HEADERS = {
    "Content-Security-Policy": _REPLAY_CSP,
    "X-Frame-Options": "SAMEORIGIN",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    **_ACCESS_CONTROL_ALLOW_ORIGIN,
}

_SHIM_JS = r"""(function () {
  "use strict";
  var self_script = document.currentScript;
  var TS = self_script ? self_script.getAttribute("data-ts") : "";
  var ORIGIN = self_script ? self_script.getAttribute("data-origin") : "";
  if (!TS || !ORIGIN) return;

  function isReplayUrl(value) {
    return typeof value === "string" && value.indexOf("/archive/r/") === 0;
  }
  function toReplayUrl(raw) {
    if (raw === null || raw === undefined) return raw;
    var value = String(raw);
    if (value === "" || isReplayUrl(value)) return value;
    if (/^(data:|blob:|javascript:|mailto:|tel:|#|about:)/i.test(value)) return value;
    var absolute;
    try {
      absolute = new URL(value, ORIGIN).href;
    } catch (err) {
      return value;
    }
    if (!/^https?:\/\//i.test(absolute)) return value;
    return "/archive/r/" + TS + "/" + absolute;
  }

  // fetch()
  if (window.fetch) {
    var originalFetch = window.fetch;
    window.fetch = function (input, init) {
      try {
        if (typeof input === "string") {
          input = toReplayUrl(input);
        } else if (input && input.url) {
          input = new Request(toReplayUrl(input.url), input);
        }
      } catch (err) { /* fall through with the original input */ }
      return originalFetch.call(this, input, init);
    };
  }

  // XMLHttpRequest
  if (window.XMLHttpRequest) {
    var originalOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url) {
      var args = Array.prototype.slice.call(arguments);
      try {
        args[1] = toReplayUrl(url);
      } catch (err) { /* leave args[1] as-is */ }
      return originalOpen.apply(this, args);
    };
  }

  // src/href setters on the common elements a script might assign to
  // directly (`img.src = "..."`, `new Image().src = "..."`, etc).
  var PATCHED = [
    [window.HTMLImageElement, "src"],
    [window.HTMLScriptElement, "src"],
    [window.HTMLMediaElement, "src"],
    [window.HTMLSourceElement, "src"],
    [window.HTMLIFrameElement, "src"],
    [window.HTMLLinkElement, "href"],
    [window.HTMLAnchorElement, "href"],
  ];
  PATCHED.forEach(function (pair) {
    var ctor = pair[0], attr = pair[1];
    if (!ctor || !ctor.prototype) return;
    var descriptor = Object.getOwnPropertyDescriptor(ctor.prototype, attr);
    if (!descriptor || !descriptor.set) return;
    Object.defineProperty(ctor.prototype, attr, {
      configurable: true,
      enumerable: descriptor.enumerable,
      get: descriptor.get,
      set: function (value) {
        descriptor.set.call(this, toReplayUrl(value));
      },
    });
  });

  // setAttribute("src"/"href", ...) covers everything the property setters
  // above miss (frameworks that go through the DOM API directly).
  var REWRITTEN_ATTRS = { src: true, href: true, action: true };
  var originalSetAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    if (REWRITTEN_ATTRS[String(name).toLowerCase()]) {
      value = toReplayUrl(value);
    }
    return originalSetAttribute.call(this, name, value);
  };

  // history.pushState/replaceState: keep client-side routing inside the
  // replay space instead of drifting the iframe's own address bar to a
  // live-site path our server has no route for.
  ["pushState", "replaceState"].forEach(function (method) {
    var original = history[method];
    if (!original) return;
    history[method] = function (state, title, url) {
      var rewritten = url ? toReplayUrl(url) : url;
      if (url) notifyParent(new URL(url, ORIGIN).href);
      return original.call(this, state, title, rewritten);
    };
  });

  // Tells the parent chrome (archive-viewer.js) which real URL is
  // currently showing, so its address-style input can track navigation —
  // the parent cannot read `iframe.contentWindow.location` itself (opaque
  // sandbox origin), so this is the only way it learns. `"*"` targetOrigin
  // is safe: nothing sensitive is sent, only the archived page's own
  // already-public URL and title.
  function notifyParent(url) {
    try {
      window.parent.postMessage({ __archiveNav: true, url: url, title: document.title }, "*");
    } catch (e) { /* no parent, or postMessage unavailable — nothing to do */ }
  }
  notifyParent(ORIGIN);
})();
"""


def _inject_shim(html: str, *, ts: str, origin: str) -> str:
    tag = f'<script src="/archive/static/shim.js" data-ts="{ts}" data-origin="{origin}"></script>'
    lowered = html.lower()
    head_index = lowered.find("<head")
    if head_index != -1:
        close = html.find(">", head_index)
        if close != -1:
            return html[: close + 1] + tag + html[close + 1 :]
    html_index = lowered.find("<html")
    if html_index != -1:
        close = html.find(">", html_index)
        if close != -1:
            return html[: close + 1] + tag + html[close + 1 :]
    return tag + html


def _full_requested_url(ts: str, url_path: str, raw_query: str) -> str:
    """Reassemble the ORIGINAL absolute URL from the route's `{url:path}`
    capture plus the request's own raw query string — `?` in the incoming
    request always belongs to the ORIGINAL page's query, not to our own
    route (`/archive/r/<ts>/<url>` never takes query parameters itself).
    """
    return f"{url_path}?{raw_query}" if raw_query else url_path


def _resolve_replay(
    original_url: str, ts_value: str
) -> tuple[Literal["page", "asset", "miss"], bytes | None, str | None, str | None]:
    """`(kind, raw_bytes, content_type, base_url_for_rewriting)`."""
    settings = get_settings()
    ts = parse_ts(ts_value)
    if ts is None:
        return "miss", None, None, None
    key = normalize_url(original_url)
    with worker_session_scope() as session:
        repo = WebArchiveRepository(session)
        archive_url = repo.find_url_any_client(key)
        if archive_url is not None:
            snapshot, _is_exact = repo.resolve_snapshot(archive_url.id, ts)
            if snapshot is not None and snapshot.body_blob_sha256:
                raw = storage.read_blob(settings.artifacts_dir, snapshot.body_blob_sha256)
                return "page", raw, snapshot.content_type, snapshot.final_url or archive_url.url
        asset = repo.get_asset_by_key(key)
        if asset is not None:
            version, _is_exact = repo.resolve_asset_version(asset.id, ts)
            if version is not None and version.blob_sha256:
                raw = storage.read_blob(settings.artifacts_dir, version.blob_sha256)
                return "asset", raw, version.content_type, asset.url
        repo.record_replay_miss(original_url, key)
        session.commit()
        return "miss", None, None, None


_PREFLIGHT_HEADERS = {
    **_ACCESS_CONTROL_ALLOW_ORIGIN,
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Max-Age": "86400",
}


@router.options("/archive/r/{ts}/{url:path}")
async def replay_preflight(ts: str, url: str) -> Response:
    """Answers a bare `OPTIONS` probe with this route's own CORS headers.

    Does NOT cover a genuine multi-header CORS preflight (`OPTIONS` carrying
    `Access-Control-Request-Method`) — `app.core.security.configure_security`'s
    global `CORSMiddleware` intercepts and answers those itself, before
    routing ever reaches here, using the app-wide `cors_allow_origins`
    (empty by default). Live-measured (task-59, 2026-09-04): a real
    captured page's own third-party script (Cloudflare's RUM analytics
    beacon) triggers exactly that kind of preflight against another
    same-origin path it references, and it fails — a DELIBERATELY accepted,
    harmless gap: the beacon is fire-and-forget third-party telemetry with
    zero effect on the archived page's own content, and it failing means it
    never reaches Cloudflare's live infrastructure either, which is the
    correct outcome for an archive. Fixing it properly would mean loosening
    the app-WIDE `cors_allow_origins` for every other route too, which is a
    worse trade than living with one third-party beacon's console noise.
    """
    return Response(status_code=204, headers=_PREFLIGHT_HEADERS)


@router.get("/archive/r/{ts}/{url:path}")
async def replay(ts: str, url: str, request: Request) -> Response:
    parsed: URL = request.url
    original_url = _full_requested_url(ts, url, parsed.query)
    kind, raw, content_type, base_url = await run_in_threadpool(_resolve_replay, original_url, ts)

    if kind == "miss" or raw is None:
        return Response(
            content="Not archived at this point in time.",
            status_code=404,
            media_type="text/plain",
            headers=_MISS_HEADERS,
        )

    if kind == "page":
        text = decode_body(raw, content_type=content_type)
        rewritten = rewritemod.rewrite_html(text, base_url=base_url or original_url, ts=ts)
        rewritten = _inject_shim(rewritten, ts=ts, origin=base_url or original_url)
        return Response(
            content=rewritten, media_type="text/html; charset=utf-8", headers=_REPLAY_HEADERS
        )

    # asset
    media_type = content_type or "application/octet-stream"
    if "css" in media_type.lower():
        text = raw.decode("utf-8", errors="replace")
        rewritten_css = rewritemod.rewrite_css(text, base_url=base_url or original_url, ts=ts)
        return Response(
            content=rewritten_css, media_type="text/css; charset=utf-8", headers=_REPLAY_HEADERS
        )
    return Response(content=raw, media_type=media_type, headers=_REPLAY_HEADERS)


@router.get("/archive/static/shim.js")
async def replay_shim() -> Response:
    return Response(
        content=_SHIM_JS,
        media_type="application/javascript; charset=utf-8",
        headers={**_REPLAY_HEADERS, "Cache-Control": "public, max-age=3600"},
    )


register_router(router)
