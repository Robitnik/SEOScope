"""Крок 1 конвеєра: sitemap -> реєстр URL (task-100 §2.3).

Джерело переліку — **тільки sitemap** (рішення користувача): сторінка,
якої сайт сам не декларує Google, у чергу не потрапляє. Перелік самих
sitemap-ів береться з GSC (`sitemaps.list`), бо саме його бачить Google,
а не з robots.txt.

Головна пастка, заміряна живо: **GSC пам'ятає sitemap-и, яких сайт більше
не віддає**. У 3XE мертві 4 з 6 (включно з трьома субдоменами), у GRP і
SWM — по одному; разом розрив 871 URL між `submitted` у GSC (3182) і живим
завантаженням (2311). Якщо проковтнути виняток, реєстр клієнта мовчки
зменшиться і частина сайту назавжди випаде з черги. Тому недоступний
sitemap — це `partial` із причиною, і за такого прогону **жоден** URL не
позначається як зниклий: відсутність відповіді не є доказом відсутності
сторінки (PLAN §12).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx

from app.db.base import utcnow
from app.db.repositories.indexing import IndexingRepository
from app.integrations.google_api.auth import GoogleApiSession
from app.integrations.google_api.gsc_client import GscClient
from app.monitor.sitemaps import SitemapEntry, maybe_decompress, parse_sitemap
from app.urlinspector.urlkey import inspector_key

logger = logging.getLogger(__name__)

_USER_AGENT = "TheYovthClientsReport/IndexingPlanner (+https://theyovth.com)"
_FETCH_TIMEOUT = 30.0
# Стеля глибини для sitemap-індексів, що посилаються один на одного.
_MAX_DOCUMENTS = 200


@dataclass
class SyncResult:
    added: int = 0
    gone: int = 0
    seen: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        """Хоч один sitemap не прочитався — реєстру не можна довіряти цілком."""
        return bool(self.errors)


def _fetch(client: httpx.Client, url: str) -> bytes:
    response = client.get(url)
    response.raise_for_status()
    return maybe_decompress(
        response.content, url=url, content_type=response.headers.get("content-type")
    )


def _parse_lastmod(value: str | None) -> date | None:
    if not value:
        return None
    text = value.strip()
    for length in (10, 19):
        try:
            return datetime.fromisoformat(text[:length].replace("Z", "")).date()
        except ValueError:
            continue
    return None


def collect_sitemap_entries(
    session: GoogleApiSession, *, site_url: str, http: httpx.Client | None = None
) -> tuple[list[SitemapEntry], list[dict[str, str]]]:
    """Усі URL із усіх sitemap-ів property + перелік тих, що не прочитались."""
    roots = [entry.path for entry in GscClient(session).sitemaps(site_url) if entry.path]
    entries: list[SitemapEntry] = []
    errors: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    visited_documents: set[str] = set()

    owns_client = http is None
    client = http or httpx.Client(
        timeout=_FETCH_TIMEOUT, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
    )
    try:
        queue = list(roots)
        while queue and len(visited_documents) < _MAX_DOCUMENTS:
            document_url = queue.pop(0)
            if document_url in visited_documents:
                continue
            visited_documents.add(document_url)
            try:
                payload = _fetch(client, document_url)
            except Exception as error:  # noqa: BLE001 — будь-яка мережева біда однаково лікується
                errors.append({"url": document_url, "error": type(error).__name__})
                logger.warning("indexing_sitemap_unreadable url=%s err=%s", document_url, error)
                continue
            document = parse_sitemap(payload)
            if document.kind == "index":
                queue.extend(
                    child.loc for child in document.entries if child.loc not in visited_documents
                )
                continue
            if document.kind == "unknown":
                errors.append({"url": document_url, "error": "unparsable"})
                continue
            for child in document.entries:
                key = inspector_key(child.loc)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                entries.append(child)
    finally:
        if owns_client:
            client.close()
    return entries, errors


def sync_client_sitemap(
    repo: IndexingRepository,
    session: GoogleApiSession,
    *,
    client_id: str,
    site_url: str,
    http: httpx.Client | None = None,
    now: datetime | None = None,
) -> SyncResult:
    moment = now or utcnow()
    entries, errors = collect_sitemap_entries(session, site_url=site_url, http=http)
    result = SyncResult(errors=errors, seen=len(entries))

    known = repo.urls_by_key(client_id)
    seen_keys: set[str] = set()
    for entry in entries:
        key = inspector_key(entry.loc)
        seen_keys.add(key)
        lastmod = _parse_lastmod(entry.lastmod)
        row = known.get(key)
        if row is None:
            repo.add_url(
                client_id=client_id,
                url=entry.loc,
                url_key=key,
                lastmod=lastmod,
                seen_at=moment,
            )
            result.added += 1
            continue
        row.url = entry.loc
        row.last_seen_in_sitemap_at = moment
        if lastmod is not None:
            row.sitemap_lastmod = lastmod
        if row.gone_from_sitemap_at is not None:
            # Сторінка повернулась у sitemap — знімаємо позначку, інакше
            # вона назавжди лишилась би поза чергою.
            row.gone_from_sitemap_at = None

    if result.degraded:
        # Частковий перелік не є доказом зникнення. Мовчазне скорочення
        # реєстру — саме той дефект, через який частина сайту випала б із
        # черги непоміченою.
        logger.info(
            "indexing_sync_partial client=%s unreadable=%d — skipping gone-detection",
            client_id,
            len(errors),
        )
        return result

    for key, row in known.items():
        if key in seen_keys or row.gone_from_sitemap_at is not None:
            continue
        row.gone_from_sitemap_at = moment
        result.gone += 1
    return result
