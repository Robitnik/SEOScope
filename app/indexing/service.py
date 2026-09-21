"""Оркестрація конвеєра Indexing Planner (task-100 §2.3).

Ізоляція така сама, як у решті проєкту (PLAN §9.1): виняток на ОДНОМУ
клієнті робить `failed` його клітинку, а не валить портфельний прогін.
Клієнт без GSC-інтеграції — це `not_configured`, а не нуль і не помилка.

Транспорт тут не питається навмисно. `config_json["transport"]` вирішує,
звідки брати ЗВІТНІ метрики (RN чи Google API, task-83/84), а стан
індексації взагалі не має другого джерела: `urlInspection` — єдиний, хто
його знає (план §1.2). Тому тул працює для будь-якого клієнта, у якого є
`gsc.account_id`, незалежно від його транспорту звітів.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.base import utcnow
from app.db.models import IndexingScan
from app.db.repositories.indexing import IndexingRepository
from app.indexing import inspect as inspect_step
from app.indexing import plan as plan_step
from app.indexing import score as score_step
from app.indexing import sync as sync_step
from app.indexing.context import load_client_context
from app.integrations.google_api import (
    GoogleApiConfigError,
    GoogleApiPermissionError,
    open_google_api_session,
)
from app.urlinspector.google.target import resolve_google_target

logger = logging.getLogger(__name__)

# Прогін, від якого немає серцебиття довше за це, вважається мертвим.
SCAN_STALE_AFTER = timedelta(minutes=30)


@dataclass
class ClientScanResult:
    client_id: str
    client_name: str
    state: str = "ok"  # "ok" | "partial" | "failed" | "not_configured"
    detail: str | None = None
    sitemap_added: int = 0
    sitemap_gone: int = 0
    sitemap_errors: list[dict[str, str]] = field(default_factory=list)
    urls_total: int | None = None
    inspected: int = 0
    failed: int = 0
    quota_exhausted: bool = False
    reasons: dict[str, int] = field(default_factory=dict)
    planned: int = 0


def site_url_for(session: Session, client_id: str) -> str | None:
    return resolve_google_target(session, client_id=client_id).gsc_site_url


def scan_client(
    session: Session,
    *,
    client_id: str,
    client_name: str,
    settings: Settings | None = None,
    today: date | None = None,
    full: bool = False,
    quota_budget: int | None = None,
    heartbeat: "callable[[], None] | None" = None,
) -> ClientScanResult:
    """sync -> inspect -> score -> plan для одного клієнта."""
    conf = settings or get_settings()
    anchor = today or utcnow().date()
    repo = IndexingRepository(session)
    result = ClientScanResult(client_id=client_id, client_name=client_name)

    site_url = site_url_for(session, client_id)
    if not site_url:
        result.state = "not_configured"
        result.detail = "Search Console isn't connected for this client."
        return result

    client_settings = repo.get_or_create_settings(client_id)
    mode = "full" if full or client_settings.scan_mode == "full" else "rotating"

    try:
        with open_google_api_session() as google:
            sync_result = sync_step.sync_client_sitemap(
                repo, google, client_id=client_id, site_url=site_url
            )
            repo.flush()
            result.sitemap_added = sync_result.added
            result.sitemap_gone = sync_result.gone
            result.sitemap_errors = sync_result.errors

            targets = inspect_step.select_for_inspection(
                repo, client_id=client_id, mode=mode
            )
            result.urls_total = len(targets)
            inspect_result = inspect_step.inspect_urls(
                repo,
                google,
                site_url=site_url,
                rows=targets,
                workers=conf.indexing_inspect_workers,
                quota_budget=quota_budget,
                heartbeat=heartbeat,
            )
            result.inspected = inspect_result.inspected
            result.failed = inspect_result.failed
            result.quota_exhausted = inspect_result.quota_exhausted
    except GoogleApiConfigError as error:
        result.state = "not_configured"
        result.detail = str(error)
        return result
    except GoogleApiPermissionError as error:
        result.state = "not_configured"
        result.detail = str(error)
        return result
    except Exception as error:  # noqa: BLE001 — клітинка падає сама, не забирає прогін
        logger.exception("indexing_scan_failed client=%s", client_id)
        result.state = "failed"
        result.detail = f"{type(error).__name__}: {error}"
        return result

    context = load_client_context(session, client_id=client_id, today=anchor)
    rows = repo.urls_for_client(client_id)
    result.reasons = score_step.score_client_urls(rows, context, today=anchor)
    repo.flush()

    if client_settings.enabled:
        # Порядок важливий і перевірений живим прогоном: спершу СЬОГОДНІ,
        # потім майбутнє. У зворотному порядку `plan_client` займає
        # єдиного кандидата завтрашнім днем, `ensure_today` бачить його
        # вже «зайнятим» і лишає сьогоднішній день порожнім — тул
        # мовчки не дає роботи на сьогодні.
        result.planned = plan_step.ensure_today(repo, client_settings, today=anchor)
        repo.flush()
        plan_result = plan_step.plan_client(repo, client_settings, today=anchor)
        result.planned += plan_result.created

    if sync_result.errors or result.failed or result.quota_exhausted:
        result.state = "partial"
    return result


@dataclass
class PortfolioScanResult:
    scan_id: str
    status: str
    clients: list[ClientScanResult] = field(default_factory=list)


def run_portfolio_scan(
    session: Session,
    *,
    trigger: str = "manual",
    client_id: str | None = None,
    full: bool = False,
    settings: Settings | None = None,
    today: date | None = None,
    user_id: str | None = None,
) -> PortfolioScanResult:
    conf = settings or get_settings()
    repo = IndexingRepository(session)
    anchor = today or utcnow().date()

    scan = repo.start_scan(
        client_id=client_id,
        trigger=trigger,
        owner=f"{socket.gethostname()}:{id(session)}",
        user_id=user_id,
    )
    repo.commit()

    if client_id is not None:
        names = repo.client_names()
        targets = [(client_id, names.get(client_id, client_id))]
    else:
        targets = [(row.id, row.name) for row in repo.active_clients()]

    def beat() -> None:
        scan.heartbeat_at = utcnow()
        repo.commit()

    results: list[ClientScanResult] = []
    try:
        for target_id, target_name in targets:
            outcome = scan_client(
                session,
                client_id=target_id,
                client_name=target_name,
                settings=conf,
                today=anchor,
                full=full,
                # Квота Google рахується НА PROPERTY, тож кап застосовується
                # до кожного клієнта окремо, а не ділиться між ними.
                quota_budget=conf.indexing_inspection_daily_cap,
                heartbeat=beat,
            )
            results.append(outcome)
            repo.commit()
            beat()
    except Exception as error:  # noqa: BLE001
        logger.exception("indexing_portfolio_scan_failed")
        _finish(repo, scan, results, status="failed", error=error)
        repo.commit()
        return PortfolioScanResult(scan_id=scan.id, status="failed", clients=results)

    considered = [row for row in results if row.state != "not_configured"]
    if any(row.state == "failed" for row in considered):
        status = "partial" if any(row.state == "ok" for row in considered) else "failed"
    elif any(row.state == "partial" for row in considered):
        status = "partial"
    else:
        status = "ok"
    _finish(repo, scan, results, status=status, error=None)
    repo.commit()
    return PortfolioScanResult(scan_id=scan.id, status=status, clients=results)


def _finish(
    repo: IndexingRepository,
    scan: IndexingScan,
    results: list[ClientScanResult],
    *,
    status: str,
    error: Exception | None,
) -> None:
    scan.finished_at = utcnow()
    scan.status = status
    totals = [row.urls_total for row in results if row.urls_total is not None]
    # `None`, а не 0: прогін, який не дійшов до підрахунку обсягу, не має
    # рапортувати «нуль сторінок» (PLAN §12).
    scan.urls_total = sum(totals) if totals else None
    scan.urls_inspected = sum(row.inspected for row in results)
    scan.urls_failed = sum(row.failed for row in results)
    scan.sitemap_added = sum(row.sitemap_added for row in results)
    scan.sitemap_gone = sum(row.sitemap_gone for row in results)
    scan.sitemap_errors_json = [
        entry for row in results for entry in row.sitemap_errors
    ]
    if error is not None:
        scan.error_code = type(error).__name__
        scan.error_message = str(error)[:1000]
