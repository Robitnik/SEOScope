"""Панель стану індексації (task-88 §3.3) — URL Inspection + сайтмапи.

Два запити в одній відповіді, бо вони відповідають на одне питання
оператора: «чи Google узагалі бачить цю сторінку». Перший — 6.6 с, другий —
дешевий, тож об'єднання нічого не коштує; жити вони мусять за окремим
ендпоінтом саме через ті 6.6 с (§0.6).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.schemas.url_inspector import (
    UrlIndexStatusDTO,
    UrlRichResultTypeDTO,
    UrlSitemapContentDTO,
    UrlSitemapDTO,
    UrlSourceStateDTO,
)
from app.urlinspector.google.gsc import GscUrlClient
from app.urlinspector.google.index import UrlInspectionClient


def collect_index_status(
    inspector: UrlInspectionClient,
    gsc: GscUrlClient,
    *,
    url: str,
) -> UrlIndexStatusDTO:
    inspection = inspector.inspect(url)
    generated_at = datetime.now(UTC)

    # `verdict is None` тут — не «не в індексі», а «Google не дав вердикту».
    # Різниця важлива: перше — привід щось робити, друге — привід перевірити
    # ще раз (task-88 §3.3).
    known = any(
        (
            inspection.verdict,
            inspection.coverage_state,
            inspection.indexing_state,
            inspection.page_fetch_state,
        )
    )

    try:
        sitemaps = [_sitemap_dto(item) for item in gsc.sitemaps()]
        sitemaps_state = UrlSourceStateDTO(
            source="gsc",
            state="has_data" if sitemaps else "no_data",
            detail=None if sitemaps else "No sitemaps submitted for this property.",
        )
    except Exception as exc:  # noqa: BLE001 — сайтмапи не мають гасити вердикт
        sitemaps = []
        sitemaps_state = UrlSourceStateDTO(
            source="gsc", state="failed", detail=f"{type(exc).__name__}: {exc}"
        )

    return UrlIndexStatusDTO(
        state=UrlSourceStateDTO(
            source="url_inspection",
            state="has_data" if known else "no_data",
            detail=None
            if known
            else "Search Console returned no index status for this URL.",
        ),
        input_url=url,
        verdict=inspection.verdict,
        coverage_state=inspection.coverage_state,
        robots_txt_state=inspection.robots_txt_state,
        indexing_state=inspection.indexing_state,
        page_fetch_state=inspection.page_fetch_state,
        last_crawl_time=inspection.last_crawl_time,
        crawled_as=inspection.crawled_as,
        google_canonical=inspection.google_canonical,
        user_canonical=inspection.user_canonical,
        canonical_mismatch=inspection.canonical_mismatch,
        referring_urls=inspection.referring_urls,
        sitemap_membership=inspection.sitemaps,
        mobile_usability_verdict=inspection.mobile_usability_verdict,
        mobile_usability_issues=inspection.mobile_usability_issues,
        rich_results_verdict=inspection.rich_results_verdict,
        rich_result_types=[
            UrlRichResultTypeDTO(rich_result_type=item.rich_result_type, items=item.items)
            for item in inspection.rich_result_types
        ],
        inspection_result_link=inspection.inspection_result_link,
        sitemaps_state=sitemaps_state,
        sitemaps=sitemaps,
        generated_at=generated_at,
    )


def _sitemap_dto(raw: dict[str, Any]) -> UrlSitemapDTO:
    return UrlSitemapDTO(
        path=str(raw.get("path") or ""),
        last_submitted=_timestamp(raw.get("lastSubmitted")),
        last_downloaded=_timestamp(raw.get("lastDownloaded")),
        is_pending=_bool(raw.get("isPending")),
        is_sitemaps_index=_bool(raw.get("isSitemapsIndex")),
        warnings=_int(raw.get("warnings")),
        errors=_int(raw.get("errors")),
        contents=[
            UrlSitemapContentDTO(
                type=str(item.get("type") or ""),
                submitted=_int(item.get("submitted")),
                # `indexed` свідомо не переноситься: API завжди віддає 0
                # (виміряно на сайті з 248 поданими URL), і показ нуля
                # читався б як «нічого не проіндексовано».
            )
            for item in (raw.get("contents") or [])
            if isinstance(item, dict)
        ],
    )


def _timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _int(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in ("true", "1"):
        return True
    if text in ("false", "0"):
        return False
    return None
