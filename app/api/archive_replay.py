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

from html import escape
from typing import Literal, NamedTuple

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
from app.webarchive.urls import looks_like_binary_content_type, normalize_url

router = APIRouter(tags=["web-archive-replay"])

# The ONE external-origin exception in this whole policy: `frame-src
# https:` (task-60 §2), gated on `Settings.archive_live_iframes` — the
# user's own call, live-data-backed (task-60 plan §0.6/§0.7): archiving an
# iframe (maps/video/audio widgets, ~85 across the portfolio) is slower
# than the page itself AND trips antibot, so its `src` is left pointing at
# the real, live host instead of being archived. Every OTHER directive
# stays `'self'`-only. `app.webarchive.rewrite`'s analytics-host denylist
# still rewrites (and thus blocks, at replay) the one iframe class this
# does NOT apply to — GTM/GA/Meta-pixel beacons — because loading one for
# real on every archived-snapshot view would send a live hit into the
# CLIENT's own GA/Meta account. `data:`/`blob:` cover inline images/fonts a
# rewritten stylesheet may still embed that way; every actual HTTP(S)
# subresource besides a live iframe must resolve through our own
# `/archive/r/...` path or it does not load at all.
def _replay_csp(*, live_iframes: bool) -> str:
    frame_src = "https:" if live_iframes else "'self'"
    return (
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        f"frame-src {frame_src}; "
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
# Deliberately NOT `immutable`/a year-long `max-age` despite a given
# `(ts, url)` pair's bytes genuinely never changing once correctly
# classified: live incident (task-59, 2026-09-04) — a page/asset
# misclassification bug (see `app.webarchive.urls`'s module note) got
# served once, and Cloudflare's edge (`cf-cache-status: HIT`, `age: 2022`)
# cached that WRONG response under this same header for what would have
# been a year, surviving the origin-side fix entirely. One hour matches
# `/archive/static/shim.js`'s own cache lifetime below and still meaningfully
# offloads repeat views within a session, while keeping any future replay
# bug's blast radius to "self-heals within the hour" instead of "needs a
# manual Cloudflare purge we have no API token configured for".
def _replay_headers(*, live_iframes: bool) -> dict[str, str]:
    return {
        "Content-Security-Policy": _replay_csp(live_iframes=live_iframes),
        "X-Frame-Options": "SAMEORIGIN",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "public, max-age=3600",
        **_ACCESS_CONTROL_ALLOW_ORIGIN,
    }


def _miss_headers(*, live_iframes: bool) -> dict[str, str]:
    return {
        "Content-Security-Policy": _replay_csp(live_iframes=live_iframes),
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
  var SNAPSHOT = self_script ? self_script.getAttribute("data-snapshot") : "";
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
  // HTMLIFrameElement is deliberately NOT here (task-60 §2): an iframe's
  // `src` is left pointing at the real, live host instead of being
  // archived — see the `setAttribute` patch below for the matching
  // exception (a lazy-load plugin promoting `data-lazy-src` to `src` at
  // runtime goes through THAT path, not this one).
  var PATCHED = [
    [window.HTMLImageElement, "src"],
    [window.HTMLScriptElement, "src"],
    [window.HTMLMediaElement, "src"],
    [window.HTMLSourceElement, "src"],
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
  // above miss (frameworks that go through the DOM API directly). IFRAME is
  // excluded from the `src`/`href` rewrite for the same reason it is absent
  // from PATCHED above (task-60 §2) — this is also the path a lazy-load
  // plugin's `iframe.src = iframe.dataset.lazySrc` promotion goes through
  // via `setAttribute`, and it must resolve against the real host, not ours.
  var REWRITTEN_ATTRS = { src: true, href: true, action: true };
  var originalSetAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    var isIframe = this.tagName === "IFRAME";
    if (REWRITTEN_ATTRS[String(name).toLowerCase()] && !isIframe) {
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
  //
  // `snapshot` is what lets the parent re-point its OWN address bar and
  // banner (date, prev/next, Meta, Compare) at the version the frame is
  // actually showing after a click on an archived link: the frame lands on
  // `/archive/r/<ts>/<url>`, whose nearest-version resolution happens
  // server-side, so the id it settled on can only come from here.
  function notifyParent(url) {
    try {
      window.parent.postMessage(
        { __archiveNav: true, url: url, title: document.title, snapshot: SNAPSHOT }, "*"
      );
    } catch (e) { /* no parent, or postMessage unavailable — nothing to do */ }
  }
  notifyParent(ORIGIN);
})();
"""


def _inject_shim(html: str, *, ts: str, origin: str, snapshot_id: str | None) -> str:
    tag = (
        '<script src="/archive/static/shim.js"'
        f' data-ts="{escape(ts)}"'
        f' data-origin="{escape(origin)}"'
        f' data-snapshot="{escape(snapshot_id or "")}"></script>'
    )
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


class ReplayTarget(NamedTuple):
    kind: Literal["page", "asset", "miss"]
    raw: bytes | None
    content_type: str | None
    base_url: str | None
    # Only set for a page: the id of the version actually resolved, handed
    # to the viewer chrome through the shim so it can re-point its own
    # address bar and banner after an in-frame link click.
    snapshot_id: str | None = None


_MISS = ReplayTarget("miss", None, None, None)


def _resolve_replay(original_url: str, ts_value: str) -> ReplayTarget:
    settings = get_settings()
    ts = parse_ts(ts_value)
    if ts is None:
        return _MISS
    key = normalize_url(original_url)
    with worker_session_scope() as session:
        repo = WebArchiveRepository(session)
        archive_url = repo.find_url_any_client(key)
        if archive_url is not None:
            snapshot, _is_exact = repo.resolve_snapshot(archive_url.id, ts)
            if snapshot is not None and snapshot.body_blob_sha256:
                raw = storage.read_blob(settings.artifacts_dir, snapshot.body_blob_sha256)
                base_url = snapshot.final_url or archive_url.url
                # Defensive, not just belt-and-suspenders: a "page" row
                # whose actual bytes are an image/font/media file (task-59,
                # 2026-09-04 — a gallery lightbox link queued as a page
                # before the capture-time guard existed, see
                # `app.webarchive.urls`'s module note) must never be run
                # through the HTML rewrite+shim pipeline below — self-heals
                # any already-stored bad snapshot immediately, without
                # waiting for the next capture pass to delete the row.
                if snapshot.content_type and looks_like_binary_content_type(
                    snapshot.content_type
                ):
                    return ReplayTarget("asset", raw, snapshot.content_type, base_url)
                return ReplayTarget("page", raw, snapshot.content_type, base_url, snapshot.id)
        asset = repo.get_asset_by_key(key)
        if asset is not None:
            version, _is_exact = repo.resolve_asset_version(asset.id, ts)
            if version is not None and version.blob_sha256:
                raw = storage.read_blob(settings.artifacts_dir, version.blob_sha256)
                return ReplayTarget("asset", raw, version.content_type, asset.url)
        repo.record_replay_miss(original_url, key)
        session.commit()
        return _MISS


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
    settings = get_settings()
    live_iframes = settings.archive_live_iframes
    analytics_hosts = frozenset(settings.archive_iframe_analytics_hosts)
    parsed: URL = request.url
    original_url = _full_requested_url(ts, url, parsed.query)
    target = await run_in_threadpool(_resolve_replay, original_url, ts)
    kind, raw = target.kind, target.raw
    content_type, base_url = target.content_type, target.base_url

    if kind == "miss" or raw is None:
        return Response(
            content="Not archived at this point in time.",
            status_code=404,
            media_type="text/plain",
            headers=_miss_headers(live_iframes=live_iframes),
        )

    if kind == "page":
        text = decode_body(raw, content_type=content_type)
        rewritten = rewritemod.rewrite_html(
            text, base_url=base_url or original_url, ts=ts,
            live_iframes=live_iframes, analytics_hosts=analytics_hosts,
        )
        rewritten = _inject_shim(
            rewritten,
            ts=ts,
            origin=base_url or original_url,
            snapshot_id=target.snapshot_id,
        )
        return Response(
            content=rewritten, media_type="text/html; charset=utf-8",
            headers=_replay_headers(live_iframes=live_iframes),
        )

    # asset
    media_type = content_type or "application/octet-stream"
    if "css" in media_type.lower():
        text = raw.decode("utf-8", errors="replace")
        rewritten_css = rewritemod.rewrite_css(text, base_url=base_url or original_url, ts=ts)
        return Response(
            content=rewritten_css, media_type="text/css; charset=utf-8",
            headers=_replay_headers(live_iframes=live_iframes),
        )
    return Response(
        content=raw, media_type=media_type, headers=_replay_headers(live_iframes=live_iframes)
    )


@router.get("/archive/static/shim.js")
async def replay_shim() -> Response:
    settings = get_settings()
    return Response(
        content=_SHIM_JS,
        media_type="application/javascript; charset=utf-8",
        headers=_replay_headers(live_iframes=settings.archive_live_iframes),
    )


register_router(router)
