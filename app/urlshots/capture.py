"""Multi-viewport screenshot capture for the URL Inspector (task-64 §2).

One browser launch for the whole preset set — measured live (task-64 §0.1,
`hearing wellservices.com`): 19.3s total for desktop+tablet+mobile from one
launch, where Chromium startup dominates and the screenshot itself is
0.1-0.3s. Three separate launches would pay that startup cost three times
for no benefit, the same reasoning `app.monitor.browser_fetch
.BrowserFetchSession` already applies to a monitor run.

Every navigation is SSRF-guarded exactly like `BrowserFetchSession._route`
(task-50 §B1) — a same-origin redirect to a private address is exactly the
input that guard exists to distrust, and a redirect chain is normal on a
client's own site (www canonicalization, HTTP->HTTPS).

Per-preset failure isolation (PLAN §9.1 applied to a 3-item fan-out instead
of a client x provider grid): a timeout on `mobile` becomes that preset's
own `failed` result, never an exception that costs `desktop`/`tablet` too.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from app.reports.stealth_browser import is_timeout_error, new_stealth_context, sync_playwright
from app.tools.http_safety import UnsafeUrlError, assert_public_http_url

_NAV_TIMEOUT_MS = 30_000
# Let client-side rendering/lazy-loaded images settle before the shot —
# `document.load` alone still shows skeleton/placeholder states on many
# sites (measured live, task-64 §0.1).
_SETTLE_MS = 1200
_JPEG_QUALITY = 82
_THUMB_WIDTH = 480
_THUMB_QUALITY = 70
# A full-page capture past this height is clipped, not left to grow
# unbounded — an infinite-scroll page could otherwise produce a
# multi-megabyte image for one screenshot (task-64 §2.2).
_MAX_FULL_HEIGHT_PX = 12_000


@dataclass(frozen=True)
class ShotPreset:
    name: str
    width: int
    height: int
    is_mobile: bool
    device_scale_factor: int


# 1440/390 double as the desktop/mobile widths `frontend/AGENTS.md §11`
# already requires verifying the app's OWN UI at.
PRESETS: tuple[ShotPreset, ...] = (
    ShotPreset("desktop", 1440, 900, is_mobile=False, device_scale_factor=1),
    ShotPreset("tablet", 834, 1112, is_mobile=True, device_scale_factor=2),
    ShotPreset("mobile", 390, 844, is_mobile=True, device_scale_factor=2),
)

PRESET_NAMES: tuple[str, ...] = tuple(p.name for p in PRESETS)
_PRESETS_BY_NAME = {p.name: p for p in PRESETS}


@dataclass(frozen=True)
class CapturedShot:
    preset: str
    state: str  # "ok" | "failed"
    image: bytes | None
    thumb: bytes | None
    width: int | None
    height: int | None
    fold_height: int | None
    byte_size: int | None
    truncated: bool
    http_status: int | None
    final_url: str | None
    error: str | None


def _thumbnail(image_bytes: bytes) -> bytes | None:
    """Best-effort — a missing/broken Pillow install costs only the
    thumbnail, never the full-size screenshot itself."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            rgb = img.convert("RGB")
            ratio = _THUMB_WIDTH / rgb.width
            size = (_THUMB_WIDTH, max(1, round(rgb.height * ratio)))
            resized = rgb.resize(size)
            out = io.BytesIO()
            resized.save(out, format="JPEG", quality=_THUMB_QUALITY)
            return out.getvalue()
    except Exception:  # noqa: BLE001 - thumbnailing is best-effort diagnostics
        return None


def _guarded_route(route: Any) -> None:
    request = route.request
    if request.resource_type == "document":
        try:
            assert_public_http_url(request.url)
        except UnsafeUrlError:
            route.abort()
            return
    route.continue_()


def _failed(preset: ShotPreset, *, error: str) -> CapturedShot:
    return CapturedShot(
        preset=preset.name,
        state="failed",
        image=None,
        thumb=None,
        width=None,
        height=None,
        fold_height=None,
        byte_size=None,
        truncated=False,
        http_status=None,
        final_url=None,
        error=error,
    )


def _capture_one(browser: Any, url: str, preset: ShotPreset) -> CapturedShot:
    ctx = new_stealth_context(
        browser,
        viewport={"width": preset.width, "height": preset.height},
        is_mobile=preset.is_mobile,
        has_touch=preset.is_mobile,
        device_scale_factor=preset.device_scale_factor,
    )
    try:
        ctx.route("**/*", _guarded_route)
        page = ctx.new_page()
        response = page.goto(url, wait_until="load", timeout=_NAV_TIMEOUT_MS)
        page.wait_for_timeout(_SETTLE_MS)

        full_height = int(page.evaluate("document.documentElement.scrollHeight") or preset.height)
        truncated = full_height > _MAX_FULL_HEIGHT_PX
        if truncated:
            image = page.screenshot(
                type="jpeg",
                quality=_JPEG_QUALITY,
                clip={"x": 0, "y": 0, "width": preset.width, "height": _MAX_FULL_HEIGHT_PX},
            )
            height = _MAX_FULL_HEIGHT_PX
        else:
            image = page.screenshot(type="jpeg", quality=_JPEG_QUALITY, full_page=True)
            height = full_height

        return CapturedShot(
            preset=preset.name,
            state="ok",
            image=image,
            thumb=_thumbnail(image),
            width=preset.width,
            height=height,
            fold_height=preset.height,
            byte_size=len(image),
            truncated=truncated,
            http_status=response.status if response else None,
            final_url=page.url,
            error=None,
        )
    except UnsafeUrlError as exc:
        return _failed(preset, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - per-preset isolation, see module docstring
        if is_timeout_error(exc):
            return _failed(preset, error="timeout")
        return _failed(preset, error=f"{type(exc).__name__}: {exc}")
    finally:
        ctx.close()


def capture_screenshots(url: str, *, presets: tuple[str, ...] = PRESET_NAMES) -> list[CapturedShot]:
    """One Chromium launch, one context per requested preset.

    Raises `UnsafeUrlError` immediately for a non-public URL — failing
    before paying for a browser launch, rather than after (defense in depth:
    the per-navigation route guard below also covers a redirect TO a private
    address, which this upfront check cannot see).
    """
    assert_public_http_url(url)
    selected = [_PRESETS_BY_NAME[name] for name in presets]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            return [_capture_one(browser, url, preset) for preset in selected]
        finally:
            browser.close()
