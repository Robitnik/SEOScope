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

import json
import logging
from html import escape
from typing import Literal, NamedTuple
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.exc import OperationalError
from starlette.datastructures import URL

from app.api import register_router
from app.core.config import get_settings
from app.db.repositories.web_archive import WebArchiveRepository
from app.db.session import worker_session_scope
from app.monitor.page import decode_body
from app.webarchive import rewrite as rewritemod
from app.webarchive import storage
from app.webarchive.replay import parse_ts
from app.webarchive.urls import is_asset_like_url, looks_like_binary_content_type, normalize_url

router = APIRouter(tags=["web-archive-replay"])
logger = logging.getLogger(__name__)

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


def _miss_page_html(*, original_url: str, ts: str) -> str:
    """A page-shaped miss hit FROM INSIDE the replay iframe (task-70) —
    replaces a bare `text/plain` "Not archived" with something the
    operator can actually act on, WITHOUT escaping the sandbox: no
    navigation happens here, this whole response IS the 404. Its own
    button posts `__archiveMiss` to the parent window — `archive-viewer.js`
    resolves whether the url even belongs to one of our clients and shows
    the SAME shared "not in the archive" panel the hub and the direct-URL
    404 already use (`archive-not-archived.js`), so all three surfaces
    agree.

    Inline `<style>`/`<script>` only, no external asset of any kind: this
    response carries the SAME CSP every other replay response does
    (`style-src`/`script-src 'unsafe-inline'`, no external origin listed at
    all), so nothing else could load here even if it tried. `original_url`
    is untrusted (an arbitrary path segment) — `json.dumps` for the two
    values embedded in the inline script (never raw string interpolation
    into JS) and `html.escape` for the one shown as visible text.
    """
    safe_url = escape(original_url)
    js_url = json.dumps(original_url)
    js_ts = json.dumps(ts)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Not archived</title>
<style>
  body {{ margin: 0; padding: 15vh 24px 24px; font-family: system-ui, -apple-system, sans-serif;
    background: #0b0d10; color: #e8e8e8; }}
  .archive-miss {{ max-width: 480px; margin: 0 auto; text-align: center; }}
  .archive-miss code {{ display: block; margin: 12px 0; padding: 8px 10px; background: #1a1d21;
    border-radius: 4px; word-break: break-all; font-size: 12px; color: #9aa0a6; }}
  .archive-miss button {{ margin-top: 16px; padding: 10px 18px; border: 0; border-radius: 6px;
    background: #ff5a1f; color: #fff; font-weight: 700; font-size: 14px; cursor: pointer; }}
  .archive-miss button:disabled {{ opacity: .6; cursor: default; }}
</style></head>
<body>
  <div class="archive-miss">
    <p><strong>Not archived at this point in time.</strong></p>
    <code>{safe_url}</code>
    <button type="button" id="archive-miss-capture">Archive this page now</button>
  </div>
  <script>
    document.getElementById("archive-miss-capture").addEventListener("click", function (event) {{
      event.target.disabled = true;
      try {{
        window.parent.postMessage({{ __archiveMiss: true, url: {js_url}, ts: {js_ts} }}, "*");
      }} catch (err) {{ /* no parent, or postMessage unavailable */ }}
    }});
  </script>
</body></html>"""

_SHIM_JS = r"""(function () {
  "use strict";
  var self_script = document.currentScript;
  var TS = self_script ? self_script.getAttribute("data-ts") : "";
  var ORIGIN = self_script ? self_script.getAttribute("data-origin") : "";
  var SNAPSHOT = self_script ? self_script.getAttribute("data-snapshot") : "";
  var SELF_ORIGIN = self_script ? self_script.getAttribute("data-self-origin") : "";
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
    // A page's OWN JS (a lazy-load bootstrap, any lib that resolves its own
    // asset root off `location.href`) sees THIS app's origin inside the
    // sandboxed iframe, not the archived site's — `location.href` is
    // `https://<this app>/archive/r/<ts>/<real url>`, and code that only
    // looks at the origin (not the path) builds
    // "https://<this app>/wp-content/..." (task-63 §3.1, live-confirmed:
    // #1 entry in `archive_replay_misses` — a real WP Rocket LazyLoad
    // bootstrap doing exactly this on a real client site). That is never a
    // legitimate reference to THIS app (everything of ours lives under
    // `/archive/`) — re-resolve it against the archived page's real origin
    // instead of leaving it pointed at a host the archive can never serve.
    if (SELF_ORIGIN && absolute.indexOf(SELF_ORIGIN) === 0) {
      var rest = absolute.slice(SELF_ORIGIN.length);
      if (rest.indexOf("/archive/") !== 0) {
        try {
          absolute = new URL(rest, ORIGIN).href;
        } catch (err) { /* leave absolute as-is */ }
      }
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


def _inject_shim(
    html: str, *, ts: str, origin: str, snapshot_id: str | None, self_origin: str
) -> str:
    tag = (
        '<script src="/archive/static/shim.js"'
        f' data-ts="{escape(ts)}"'
        f' data-origin="{escape(origin)}"'
        f' data-snapshot="{escape(snapshot_id or "")}"'
        f' data-self-origin="{escape(self_origin)}"></script>'
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
        if asset is None and "?" in key:
            # task-63 §3.2: a page's own JS appends a fresh cache-buster
            # query (`?v=<timestamp>`) that the archived asset was never
            # captured under. Two independent fallbacks, both scoped to
            # ASSETS ONLY — `LAXTON`'s `?taxonomy=...&term=123` PAGES are
            # real, distinct pages (`app.webarchive.urls.normalize_url`'s
            # own docstring) and must never be collapsed this way.
            no_query_key = normalize_url(original_url, keep_query=False)
            asset = repo.get_asset_by_key(no_query_key)
            if asset is None:
                prefix = f"{key.split('?', 1)[0]}?"
                asset = repo.find_asset_by_key_prefix(prefix)
        if asset is not None:
            version, _is_exact = repo.resolve_asset_version(asset.id, ts)
            if version is not None and version.blob_sha256:
                raw = storage.read_blob(settings.artifacts_dir, version.blob_sha256)
                return ReplayTarget("asset", raw, version.content_type, asset.url)
        return _MISS


def _record_replay_miss(original_url: str, key: str) -> None:
    """Scheduled via `BackgroundTasks` (task-63 follow-up, live-reported
    2026-09-05) — runs AFTER the response is already sent, on its OWN
    session, never on the request path that decides hit vs. miss.

    Live-measured root cause of a ~30s hang on this exact response: a
    concurrent writer (a capture in progress) held SQLite's write lock, and
    this write's own `session.commit()` sat waiting out the FULL
    `busy_timeout` (30s, `app/db/session.py`) before the read path could
    even answer — the operator's own "27 seconds" almost exactly matches
    it. This is pure telemetry (an honest list of rewriter gaps), never
    archive data, so it is fine for it to finish late, and fine to
    occasionally lose one row if the process exits before it runs — neither
    is fine for the operator's own response to wait on.
    """
    with worker_session_scope() as session:
        repo = WebArchiveRepository(session)
        try:
            repo.record_replay_miss(original_url, key)
            session.commit()
        except OperationalError:
            # Telemetry, not archive data — a busy SQLite writer (task-62's
            # own `ArchiveWriteController` only serializes ITS OWN process;
            # this background task has no controller to queue behind)
            # must never raise past this best-effort log line (task-63
            # §3.4, live-confirmed: `database is locked` surfaced as a bare
            # 500 with none of this module's own CSP/CORS headers attached,
            # back when this write still sat on the request path itself).
            session.rollback()
            logger.warning("archive_replay_miss_unlogged url=%s", key)


def _looks_like_top_level_navigation(request: Request) -> bool:
    """`Sec-Fetch-Dest: document` means the WHOLE browser tab is navigating
    HERE directly — a pasted/typed address, or (live-reported 2026-09-05,
    the actual case) an operator opening a rewritten in-page link in a new
    tab, which bypasses the SPA viewer entirely and lands on this raw
    backend route with no chrome at all. The viewer's OWN `<iframe>` sends
    `Sec-Fetch-Dest: iframe` for the identical URL and must NEVER redirect
    here — that would silently escape the sandbox and navigate the WHOLE
    app inside a replay meant to be inert. A missing header (an older
    browser, a script, `curl`) is treated leniently as "assume top-level" —
    the conservative default a human clicking a link would actually get.
    """
    dest = request.headers.get("sec-fetch-dest")
    return dest is None or dest == "document"


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
async def replay(
    ts: str, url: str, request: Request, background_tasks: BackgroundTasks
) -> Response:
    settings = get_settings()
    live_iframes = settings.archive_live_iframes
    analytics_hosts = frozenset(settings.archive_iframe_analytics_hosts)
    parsed: URL = request.url
    original_url = _full_requested_url(ts, url, parsed.query)
    target = await run_in_threadpool(_resolve_replay, original_url, ts)
    kind, raw = target.kind, target.raw
    content_type, base_url = target.content_type, target.base_url

    if kind == "miss" or raw is None:
        background_tasks.add_task(
            _record_replay_miss, original_url, normalize_url(original_url)
        )
        # A direct top-level hit on a PAGE-shaped miss (task-63 follow-up,
        # live-reported 2026-09-05: opening a rewritten in-page link in a
        # new tab, landing on a bare "Not archived" with nothing to do
        # about it) sends the operator to the hub's own lookup instead —
        # same "Not archived (yet)" / "tracked but no version yet" panels,
        # same "Archive this page now" button and live progress, this task
        # already built there. An asset-shaped miss (an image/css/js the
        # rewriter genuinely lost) stays a plain 404 — "archive this page"
        # makes no sense for a sub-resource, and this route is what the
        # viewer's OWN iframe fetches thousands of times per page, where a
        # redirect would silently escape the replay sandbox.
        asset_like = is_asset_like_url(original_url)
        if _looks_like_top_level_navigation(request) and not asset_like:
            return RedirectResponse(
                url=f"/#/archive?url={quote(original_url, safe='')}", status_code=302
            )
        # A PAGE-shaped miss hit from INSIDE the iframe (task-70) gets the
        # actionable styled response (`_miss_page_html`) — an asset-shaped
        # miss (an image/css/js the rewriter genuinely lost) stays plain
        # `text/plain`: "archive this page" makes no sense for a
        # sub-resource, and this route is what the viewer's OWN iframe
        # fetches thousands of times per page, where anything heavier is
        # pure waste.
        if asset_like:
            return Response(
                content="Not archived at this point in time.",
                status_code=404,
                media_type="text/plain",
                headers=_miss_headers(live_iframes=live_iframes),
            )
        return Response(
            content=_miss_page_html(original_url=original_url, ts=ts),
            status_code=404,
            media_type="text/html; charset=utf-8",
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
            self_origin=f"{parsed.scheme}://{parsed.netloc}",
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
