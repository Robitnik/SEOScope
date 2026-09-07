"""Application settings (pydantic-settings), loaded from environment / .env."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.schemas.enums import UserRole


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
    )

    app_name: str = "The Yovth Clients Report"
    environment: str = "development"
    log_level: str = "INFO"

    database_url: str = "sqlite+aiosqlite:///./app_data/app.db"

    artifacts_dir: Path = Path("./app_data/artifacts")
    browser_profiles_dir: Path = Path("./app_data/browser_profiles")
    # Read-only checkout mounted by compose. Provider code, clients.json and
    # historical artifacts remain owned by AISeoTools.
    aiseotools_source_path: Path = Path("/seo/AISeoTools")

    frontend_dir: Path = Path("./frontend")

    # Browser session bridge (task-20, PLAN §8.3/§13/§17). The host name the container uses to
    # reach a warm Chrome's CDP port. Compose runs api/worker with `network_mode: host` (Chrome's
    # CDP socket is loopback-only — see compose.yaml), so this is 127.0.0.1 by default, not a
    # docker-bridge hostname. Never a host filesystem path (PLAN §14). This setting only documents
    # the value for the API layer; the actual liveness switch is the SEOTOOLS_REMOTE_CDP_HOST env
    # var compose sets for AISeoTools directly (zero AISeoTools import here).
    chrome_cdp_host: str = "127.0.0.1"
    browser_login_timeout_seconds: float = 120.0

    # ── Авторизація (task-71) ────────────────────────────────────────────
    # Вимкнення лишається лише для локальної розробки й тестів: при `False`
    # кожен старт пише WARNING, бо це відкриває всі дані всім, хто має URL.
    auth_enabled: bool = True
    auth_cookie_name: str = "yovth_session"
    auth_csrf_cookie_name: str = "yovth_csrf"
    # `False` для http://localhost. За Cloudflare-тунелем (https) — `True`,
    # інакше cookie поїде і по відкритому каналу.
    auth_cookie_secure: bool = True
    # Абсолютний строк життя сесії і строк простою. Перевіряються обидва:
    # вкрадений cookie з ноутбука, яким не користуються, помирає сам.
    session_absolute_ttl_days: int = 14
    session_idle_ttl_hours: int = 72
    # Як часто дозволено оновлювати `last_seen_at`. НЕ косметика: після
    # task-66/67 (writer-лок був зайнятий 97% часу) запис на кожен запит —
    # це податок на всю систему; 5 хв дає рівно ту точність, яка потрібна
    # idle-таймауту, і один запис замість сотень на рендер дашборда.
    session_touch_interval_seconds: int = 300
    login_max_failures: int = 10
    login_lockout_minutes: int = 15
    # Додаткові admin-only префікси поверх `DEFAULT_ADMIN_ONLY_PATHS`
    # (`app/core/auth/policy.py`) — щоб обмежити ще один інструмент, не
    # чіпаючи код: наприклад `["/api/v1/tools/redirects"]`.
    auth_admin_only_paths: list[str] = []
    # Bootstrap першого адміна (`yovth-report ensure-admin` з entrypoint).
    # Спрацьовує ЛИШЕ коли таблиця `users` порожня.
    bootstrap_admin_email: str | None = None
    bootstrap_admin_name: str = "Administrator"

    # ── Журнал дій (task-72) ─────────────────────────────────────────────
    audit_enabled: bool = True
    # Черга й батч. Запит НІКОЛИ не чекає на журнал: він кладе рядок у чергу,
    # а один потік пише їх пачками — після task-66/67 (writer-лок 97%
    # зайнятості) запис на кожну дію повернув би лок-шторм.
    audit_queue_max: int = 10000
    audit_batch_size: int = 200
    audit_flush_interval_s: float = 1.0
    # Куди дописуємо, якщо БД недоступна: інцидент, під час якого база
    # лежить, не має бути єдиним нежурнальованим.
    audit_fallback_path: Path = Path("./app_data/audit_fallback.jsonl")
    audit_retention_days: int = 365
    # Скільки рядків максимум віддає CSV-експорт за один раз.
    audit_export_max_rows: int = 50000

    # ── Dev-доступ без входу (task-73) ───────────────────────────────────
    # Обидва прапорці нижче мертві, коли `environment == "production"` —
    # незалежно від значення, не лише «дефолт вимкнено» (див.
    # `app/core/auth/sessions.py`, `resolve_agent_token`/`resolve_localhost_bypass`).
    #
    # Спільний bearer-токен для паралельних агентів-розробників: `.env`
    # (git-ignored) тримає секрет, який жоден CI/прод-конфіг не отримує.
    # `None` = вимкнено.
    agent_dev_token: str | None = None
    agent_dev_token_role: UserRole = UserRole.admin
    agent_dev_token_label: str = "dev-agent"

    # ── MCP-сервер (task-74, хвиля 1) ────────────────────────────────────
    # `app/mcp/**` ходить у наше ж API як звичайний клієнт (план §2), тому
    # йому потрібні лише адреса й токен — жодних налаштувань БД тут немає.
    # `mcp_token` не задано → фолбек на `agent_dev_token`
    # (`app/mcp/apiclient.py::_resolve_token`) — той самий bearer-шлях, що
    # й у task-73, структурно мертвий на `environment == "production"`.
    #
    # Публічний домен (task-77), не `127.0.0.1` — навіть для хвилі 1
    # (stdio через `docker compose exec`, тобто виклик усередині того самого
    # контейнера) справжня публічна адреса лишається коректною, оскільки
    # контейнер має вихід в інтернет; а для будь-кого, хто піднімає stdio-
    # процес НЕ на docker-хості (майбутнє: власний ноутбук з тим самим
    # образом), localhost за визначенням недосяжний. Override через `.env`
    # лишається, якщо комусь потрібен саме внутрішній адрес.
    mcp_api_base_url: str = "https://seo.theyovth.space"
    mcp_token: str | None = None

    # Вхід без пароля для оператора на localhost. За замовчуванням `False` —
    # `network_mode: host` (compose.yaml) означає, що Cloudflare-тунель
    # форвардить на ту саму адресу `127.0.0.1`, що й людина в браузері;
    # безпечним це робить лише перевірка на відсутність проксі-заголовків
    # у `is_trusted_loopback`, а не сам прапорець.
    auth_localhost_bypass: bool = False

    event_retention_days: int = 30
    job_lease_seconds: int = 300

    # task-75 §3.3: `job_events.seq` is a global autoincrement PK doubling as
    # the SSE `Last-Event-ID` cursor (`EventRepository.since`). Under exactly
    # one writer (today's SQLite worker) that's safe — transactions commit in
    # the same order they took `seq`. Under more than one writer (Postgres,
    # once task-75 lands), a sequence hands out numbers at INSERT time but
    # rows become visible at COMMIT time, and those two orders can differ:
    # writer A takes seq=100, writer B takes seq=101 and commits FIRST, a
    # poll sees only 101 and advances `last_seq` past it, then A commits —
    # event 100 is now permanently unreachable (`seq > last_seq` never
    # matches it again). Silent: the event just never appears in the live
    # sync-modal, and a single-writer test can't reproduce it.
    #
    # Fix: only serve an event once it is older than this window — long
    # enough that any OTHER transaction which grabbed a lower `seq` earlier
    # is guaranteed to have already committed (ordinary commit latency is
    # single-digit milliseconds; 200ms is a generous margin, not a tight
    # one). This trades permanent, guaranteed data loss under any
    # concurrency for an extra ~200ms of live-progress latency — imperceptible
    # in a UI that already polls every second (`app/jobs/events.py`'s
    # `poll_interval_seconds`).
    job_event_cursor_safety_window_ms: int = 200

    # How many cells of ONE provider may collect at the same time (task-31).
    # For ahrefs/semrush this is literally how many windows of the warm host
    # Chrome are driven in parallel on the single logged-in profile.
    #
    # Capped at 4 on purpose: the ceiling is not CPU but the provider — more
    # concurrent sessions from one account look increasingly like automation,
    # and metered units burn N times faster in wall-clock time. `tech_audit`
    # and `blogs` stay at 1 because each already parallelises its own crawl.
    browser_parallel_slots: int = 3
    max_browser_parallel_slots: int = 4
    # Per-provider overrides, set from MEASUREMENT rather than taste
    # (LIVE 2026-07-31, 3 clients each, warm host Chrome, one profile):
    #
    #   ahrefs @ 3 lanes -> KPIs fine, but ALL THREE detail exports timed out
    #                       (`Ahrefs detail export failed: TimeoutError`), so
    #                       every snapshot came back `partial`.
    #   ahrefs @ 2 lanes -> both cells `succeeded`, quality `complete`, no
    #                       warnings, ~100s wall clock for two clients.
    #
    # The cause is structural, not tuning: the bulk export goes through the
    # store-wide `download_lock` (CDP `Browser.setDownloadBehavior` is
    # browser-global — see `seo_audit/browser/profile.py`), so a third lane adds
    # queueing to a phase that cannot run in parallel and pushes the waiting
    # lanes past their Playwright timeouts. Semrush collects almost entirely by
    # export and shares that same lock, so it gets the same ceiling.
    provider_parallel_slots: dict[str, int] = {"ahrefs": 2, "semrush": 2}
    # Seconds between the starts of a provider's lanes. LIVE 2026-07-31: with
    # all three starting in the same millisecond, two of three Ahrefs cells died
    # on a 60s `Page.goto` timeout — three identical heavy SPA loads through one
    # Chrome process — while the third finished normally. Spacing the starts
    # removes that thundering herd; after the first cell the lanes drift apart on
    # their own. Purely a smoothing knob: concurrency itself is bounded by the
    # capacity semaphore, not by this.
    lane_start_stagger_seconds: float = 8.0
    single_lane_providers: list[str] = ["tech_audit", "blogs"]
    http_request_timeout_seconds: float = 30.0

    cors_allow_origins: list[str] = []

    # Alerts engine thresholds (task-17, PLAN §21.5) — noise-control knobs,
    # deliberately configurable rather than hardcoded in app.metrics.alerts.
    alert_min_relative_change_pct: Decimal = Decimal("20")
    alert_min_base_impressions: int = 50
    alert_min_base_clicks: int = 10
    alert_top_n_per_group: int = 20

    # Site Monitor (task-35, PLAN §16 "Етап 8"). Values are measurements, not
    # taste — see task-35 §1.4/§3.3/§3.7 for the live portfolio numbers behind
    # each default.
    monitor_enabled: bool = True
    monitor_light_interval_hours: float = 1.0
    monitor_full_interval_hours: float = 3.0
    # task-58 §0.5/§1.4: 4 was sized for "one client at a time, fully
    # sequential inside it" — with the per-client page pool below, the whole
    # active portfolio (13 clients live 2026-09-04) can run at once instead
    # of queueing behind 4 slots; the serialized DB section's own measured
    # ceiling (~100 pages/s, §0.4) has ample headroom for this.
    monitor_client_workers: int = 16
    # Safety valve, NOT a product limit (§3.3): the largest client today is 608
    # URLs. This exists only so a runaway/looping sitemap cannot hang the
    # engine forever.
    monitor_max_urls_per_client: int = 25_000
    monitor_run_max_minutes: float = 90.0
    monitor_max_body_bytes: int = 3 * 1024 * 1024
    monitor_request_timeout_seconds: float = 20.0
    monitor_host_min_interval_seconds: float = 0.25
    # task-58 §1.4: pages of ONE client checked by this many threads at
    # once, pulling off one shared priority-ordered queue. Bounded well
    # under `monitor_host_concurrency` × any plausible value would still be
    # polite — the real limit on how fast one client's run goes is the
    # site's own TTFB (live-measured 3-5s/page), which parallelism hides,
    # not eliminates.
    monitor_page_workers: int = 6
    # task-58 §1.5: `HostThrottle`'s new `concurrency` — how many of one
    # client's page workers may have a request in flight against the same
    # host at once (`HostThrottle`'s old default, `concurrency=1`, is a full
    # mutex — every OTHER caller of `HostThrottle()` keeps that unchanged).
    monitor_host_concurrency: int = 4
    monitor_key_pages_limit: int = 10
    # Confirmed with the user 2026-08-11: a URL gone from the sitemap stays
    # watched for 30 days before it drops off entirely, so a silent removal
    # is still caught as `page_gone` instead of vanishing unnoticed.
    monitor_gone_retention_days: int = 30
    # Confirmed with the user 2026-08-11: full page bodies are kept forever by
    # default (§3.2.1) — retention is an explicit opt-in via `monitor-prune
    # --bodies-older-than`, never a silent default.
    monitor_store_page_bodies: bool = True
    monitor_body_retention_days: int = 0
    # We deliberately do NOT honor the client's own robots.txt (§3.3): the
    # moment robots.txt closes the site is exactly the incident we exist to
    # catch, and obeying the ban we are supposed to be watching for is a
    # logical trap. `Crawl-delay` is still respected.
    monitor_respect_robots: bool = False
    monitor_run_stale_minutes: float = 60.0
    monitor_doh_resolvers: list[str] = ["dns.google", "cloudflare-dns.com"]
    monitor_sitemap_drop_alert_pct: Decimal = Decimal("10")
    monitor_sitemap_broken_warn_pct: Decimal = Decimal("2")
    monitor_sitemap_broken_warn_count: int = 10
    monitor_sitemap_broken_alert_pct: Decimal = Decimal("10")
    monitor_sitemap_broken_alert_count: int = 50

    # Antibot lane (task-50, PLAN §12 "чесні стани"; revised task-58 §1.1-§1.3
    # — HTTP-first with a per-PAGE browser fallback instead of a whole-run
    # transport choice). Live-measured on alphastyle.store (task-50 §0): 30
    # requests at 0.25s pass clean, ~58 requests wall solid — the exact safe
    # throughput was deliberately NOT determined here (finding it means
    # driving a live client's site into a multi-minute penalty window on
    # purpose); `monitor_antibot_min_interval_seconds` starts conservative
    # and gets measured against the real first antibot-lane run instead.
    monitor_antibot_min_interval_seconds: float = 2.0
    # task-58 §1.3: ONE process-wide cap on concurrently-live Chromium
    # instances, replacing the old dedicated single-client `_antibot_pool` —
    # a page-worker thread's `_LazyBrowser` holds a slot for its whole run,
    # not per navigation, so this bounds "how many Chromiums exist right now
    # across the whole portfolio". Kept small: each one is a real browser
    # process, and the whole point of HTTP-first is that most clients never
    # touch this at all.
    monitor_browser_concurrency: int = 3
    # task-58 §1.2: hard ceiling on how many pages ONE run may escalate to
    # Chromium — `_should_escalate_to_browser` already avoids re-confirming
    # a URL already known dead, but this is the backstop for a site that has
    # gone down entirely: without it, every one of its hundreds of pages
    # would each still cost a Chromium launch+navigation once.
    monitor_browser_fallback_max_per_run: int = 50
    # Fraction of the last 20 completed page attempts that came back blocked
    # before a run ends itself early rather than grinding through the rest
    # of the target list into the same wall (task-50 §0.6: 304 of 362
    # requests in the incident run were spent hammering a wall already up
    # since request ~58). Replaces `monitor_block_streak_abort`
    # (task-58 §2.2): "consecutive" has no meaning once page workers finish
    # out of order, a sliding-window ratio still says the same thing.
    monitor_block_ratio_abort: float = 0.8

    # Redirect monitoring (task-51). Live-measured on the portfolio
    # 2026-08-19: 10 of 14 clients expose `redirection/v1` in `/wp-json/`
    # (authenticated pull), the rest fall back to the blackbox probe.
    monitor_redirects_enabled: bool = True
    # Above this many new rules in one run, collapse them into one
    # `redirects_bulk_added` aggregate instead of one row each (same pattern
    # as `monitor_sitemap_broken_warn_count`) — a bulk permalink change on an
    # active site is one human action, not N events.
    monitor_redirects_bulk_threshold: int = 25
    # Blackbox-probe fallback (task-51 §3.2.3): existing probe rules + key
    # pages, padded from the sitemap up to this many URLs — a request budget
    # roughly on par with the light run's own page checks.
    monitor_redirect_probe_max_urls: int = 50
    # Emergency valve on the pull adapter's pagination, not a product limit —
    # the largest portfolio client today is nowhere near this.
    monitor_redirects_max_rules: int = 20_000
    # Application Passwords, file-based and OUTSIDE the database (task-51
    # §3.3): the project has no encrypted-column mechanism, and
    # `ClientIntegration.config_json` is explicitly documented as
    # non-secret-only. `{"<client_id>": {"user": ..., "app_password": ...}}`,
    # `0600`, never written by this codebase's own migrations/seed data.
    wp_credentials_path: Path = Path("./app_data/secrets/wp_credentials.json")
    # Regular wp-admin login, browser-session fallback (task-51 follow-up,
    # live finding 2026-08-19): no client in the portfolio had an
    # Application Password ever issued. A REGULAR password is a bigger
    # secret than an app password (full account access, not one revocable
    # token) — kept in its OWN file so the two credential kinds are never
    # confused about which one a record holds.
    wp_login_credentials_path: Path = Path("./app_data/secrets/wp_login_credentials.json")

    # Reports engine (task-42, PLAN §16 "Хвиля N"). Defaults are measurements,
    # not taste — see task-42 §1.2/§1.5/§1.7/§3.6/§3.7 for the live numbers
    # behind each one.
    reports_enabled: bool = True
    reports_api_workers: int = 3
    # NOT to be raised: one login, one browser, one cabinet session per
    # process — a second concurrent PDF run would double-click the same UI.
    reports_pdf_workers: int = 1
    reports_max_months: int = 24
    reports_max_comparator_years: int = 4
    # Measured 41-67s per column headless; generous ceiling for a slow one.
    reports_pdf_period_timeout_seconds: float = 300.0
    reports_pdf_retries: int = 3
    # The API path had NO period-level retry at all before this (task-47
    # follow-up, live-found alongside the PDF path's own retry-classification
    # bug): one bad response failed the period outright. Mirrors
    # `reports_pdf_retries`'s shape/default.
    reports_api_period_retries: int = 3
    reports_cross_check_tolerance_pct: Decimal = Decimal("0.5")
    reports_pdf_cache_enabled: bool = True
    # `False` leaves a hook for local debugging under Xvfb.
    reports_headless: bool = True
    # How many COMPLETE months the "Historical Data" block covers (task-45
    # §2.4). 12 mirrors the hand-kept Google Sheets history it replaces; the
    # whole window costs two Reporting Ninja queries (~2s), so this is a
    # readability choice, not a cost one.
    reports_history_months: int = 12

    # Batch + client document/PDF (task-47). Defaults are measurements, not
    # taste — see task-47 §1.6/§2.3 for the live numbers behind each one.
    #
    # Separate from `reports_api_workers` on purpose (task-47 §2.3): without
    # its own pool, a 14-client batch would occupy every API-path slot and an
    # operator's unrelated single-client report would queue behind the whole
    # batch. `ReportScheduler` now runs standalone and batch-sourced API runs
    # in two independent pools of this size.
    reports_batch_client_workers: int = 3
    # The direct Google API report source (task-84 §3.3). Its own scheduler
    # pool, like `reports_batch_client_workers` above: a `source="google_api"`
    # run is claimed by `source` string, so without a dedicated `_fill` call
    # it would sit `queued` forever. Higher than `reports_api_workers` because
    # this path has no browser and no Reporting Ninja 300/min account limit —
    # only GA4's per-property quota, which our ~2 requests/period/client is
    # orders of magnitude under.
    reports_google_api_workers: int = 4
    # Token-bucket ceiling on Reporting Ninja API calls, shared across the
    # whole process (task-47 §1.6/§2.3): the account's own limit is 300/min,
    # and a 14-client batch alone issues ~280 — without a limiter here, one
    # concurrent ad-hoc report on top of a running batch would exceed it.
    # 240 is 80% of the account ceiling, leaving headroom for exactly that.
    reports_api_max_rpm: float = 240.0
    # The API process's own base URL, reachable from the WORKER process too:
    # both run with `network_mode: host` (compose.yaml), so the worker can
    # render a client PDF by driving Chromium against the API's own
    # `/reports/runs/{id}/document` page (task-47 §2.1) exactly like a
    # browser would — one rendered markup, on screen and in the PDF alike.
    reports_internal_base_url: str = "http://127.0.0.1:6524"

    # Cloud storage (task-52, Backblaze B2): off-site DB backup, a mirror of
    # every client report file, and the Site Monitor web archive. Live-tested
    # 2026-08-19 against a PRIVATE bucket (`theyovth-vault`) with its own
    # restricted application key — see the task plan §1.2/§1.3 for why:
    # the account's original key was an unrestricted master key, and the
    # only OTHER bucket on this account (`TheYovth`) is `allPublic`, so
    # storing backups there would need an encryption key on our side whose
    # loss would make every backup unreadable forever (rejected by the user
    # 2026-08-19 as a worse risk than the private bucket).
    cloud_enabled: bool = False
    b2_vault_bucket_name: str = ""
    b2_vault_key_id: str = ""
    b2_vault_application_key: str = ""
    # UTC "HH:MM" — the one daily backup tick (`app/cloud/scheduler.py`).
    cloud_backup_at_utc: str = "03:30"
    cloud_backup_keep_daily: int = 30
    cloud_backup_keep_weekly: int = 12
    cloud_backup_keep_monthly: int = 12
    cloud_sync_reports: bool = True
    cloud_sync_monitor_blobs: bool = True
    # Per-tick sweep size (task-52 §3.6) — the portfolio's own measured
    # growth (300-800 new monitor blobs/day) is far below this even at the
    # scheduler's 60s tick, so this is a safety ceiling, not a throughput knob.
    cloud_sweep_batch_size: int = 200
    cloud_upload_workers: int = 4
    cloud_request_timeout_seconds: float = 60.0

    # Monthly Performance table (task-54): the sheet-fidelity block on the
    # client page. 24 covers the operator's own historical sheet (20 months
    # measured on the live SDS screenshot, task-54 §1.6); GSC columns still
    # read `None` past its own 16-month window regardless of this setting.
    client_monthly_history_months: int = 24
    # `/refresh` runs synchronously (task-54 §2.5) — 3-4 Reporting Ninja
    # queries measured at ~2-4s per client, well under this. A generous
    # ceiling rather than a tuned one: this is the failure mode for an
    # upstream RN slowdown, not the expected path.
    client_monthly_refresh_timeout_seconds: float = 90.0

    # Web Archive (task-59, PLAN §16 wave-U): a self-hosted, Wayback-style
    # copy of every active client's site, captured twice a day. Independent
    # of Site Monitor (task-35) — see `app/webarchive/__init__.py`.
    archive_enabled: bool = True
    archive_capture_times: list[str] = ["13:30", "19:30"]
    archive_timezone: str = "Europe/Kyiv"
    archive_include_paused_clients: bool = False
    archive_max_urls_per_client: int = 25_000
    # "Актуальність тиждень" (user's own words, task-59): an unchanged page
    # still gets a fresh anchor snapshot after this many days even though
    # its fingerprint never changed, so the archive never goes further than
    # a week without a confirmed-live copy of a page.
    archive_refresh_after_days: int = 7
    # Live-measured (task-59 §0.5): conditional GET on a client-site asset
    # returned 304 in 10/10 samples with `Last-Modified` present in 38/38 —
    # an asset checked more recently than this is not re-requested at all.
    archive_asset_revalidate_days: int = 7
    # Live-measured (task-59 §0.4): p90 476 subresource references on one
    # page — a ceiling exists so one unusually heavy first capture cannot
    # dominate a whole capture pass; overage is recorded honestly as
    # `assets_truncated`, never silently dropped.
    archive_new_assets_per_page: int = 300
    archive_html_max_bytes: int = 5 * 1024 * 1024
    archive_asset_max_bytes: int = 10 * 1024 * 1024
    archive_client_workers: int = 6
    archive_page_workers: int = 4
    archive_host_min_interval_seconds: float = 0.25
    # `HostThrottle(concurrency=N)` — 1 until task-58 lands (mirrors
    # `monitor_host_concurrency`'s own pre-task-58 default); safe to raise
    # once that lane's own per-host concurrency guard is in `done/`.
    archive_host_concurrency: int = 1
    archive_request_timeout_seconds: float = 25.0
    archive_capture_deadline_minutes: float = 240.0
    # task-62: how stale `heartbeat_at` may get before a capture is declared
    # dead and reclaimed. Replaces `ArchiveScheduler._in_flight` (in-process
    # memory) as the liveness check — a CLI-run capture, its own OS process,
    # was invisible to that set and got declared `interrupted` within a
    # minute despite running to completion (live-observed, 2026-09-05).
    # Comfortably above the 5s/20-URL counter-flush interval that also
    # drives the heartbeat write (`capture.py`'s `_COUNTER_FLUSH_SECONDS`).
    archive_heartbeat_stale_seconds: float = 90.0
    archive_local_retention_days: int = 30
    archive_blob_cache_gb: float = 20.0
    archive_cloud_prefix: str = "webarchive"
    archive_capture_assets: bool = True
    # task-60 §2: an iframe's `src` is left pointing at the real, live host
    # instead of being archived — the user's own call (archiving a
    # map/video/audio widget is slower than the page itself AND trips
    # antibot). A kill-switch, not a tuning knob: flipping it to `False`
    # reverts BOTH the rewrite (`app.webarchive.rewrite.rewrite_html`) and
    # the replay CSP's `frame-src` (`app/api/archive_replay.py`) to the
    # old fully-archived-or-blocked behavior, without a code deploy.
    archive_live_iframes: bool = True
    # Hosts an iframe `src` must NEVER be left live for even with the flag
    # above on — analytics/tracking beacons (task-60 §2.2): loading these
    # for real on every archived-snapshot view would send a live hit into
    # the CLIENT's own GA/Meta account. A setting, not a hardcoded list, so
    # a newly-spotted beacon host can be blocked without a code change.
    # `facebook.com/tr` (Meta's pixel endpoint specifically, not the whole
    # domain) is matched separately in `app.webarchive.urls` regardless of
    # this list.
    archive_iframe_analytics_hosts: list[str] = [
        "googletagmanager.com", "google-analytics.com", "doubleclick.net", "connect.facebook.net",
    ]
    # task-63 §3.3: some assets are referenced ONLY as a string literal
    # inside a page's own JS (a deferred-loading bootstrap calling
    # `load_js('/wp-content/.../lazyload.min.js')`), never in the page's
    # static HTML at all — `discover_html_assets` cannot see either.
    # `discover_js_string_assets` is a heuristic over already-fetched JS
    # text; this caps how many NEW refs it may contribute per page so a
    # large minified bundle full of unrelated-looking strings cannot flood
    # the asset queue. A kill-switch, not just a tuning knob: `0` disables
    # the heuristic entirely without a code deploy if it proves noisy.
    archive_js_discovered_assets_per_page: int = 20
    # task-81 §2.1-§2.3: replay must not depend on whether a page's own JS
    # ran. WP Rocket LazyLoad ships every `<img>` twice — a lazy copy
    # (`data-lazy-src` + an SVG placeholder `src`) and a `<noscript>` copy
    # with the real `src` — and on 66% of the portfolio's pages the JS that
    # is supposed to promote the lazy copy never gets that far (it dies on
    # its first line inside our sandboxed, `allow-same-origin`-less iframe;
    # task-81 §0.3). `rewrite_html` promotes the lazy attributes to real
    # ones and drops the now-redundant `<noscript>` copy on every render —
    # a kill-switch, same class as `archive_live_iframes`, in case some
    # client's markup turns out not to fit the assumption.
    archive_unlazy_html: bool = True
    # task-81 §2.4: Elementor's entrance-animation class (`elementor-
    # invisible`, `visibility:hidden`) is meant to be removed by
    # `elementor-frontend.js` once the element scrolls into view — a JS
    # step that, like the lazy-load one above, never runs in replay, so the
    # block stays hidden FOREVER (SHB's homepage was a single black
    # rectangle because of this — task-81 §0.6). Injects one `<style>`
    # override next to the shim tag rather than editing `class="..."`
    # attributes with a regex.
    archive_replay_fixup_css: bool = True
    # task-82 §2: a same-host link the crawler discovers can be a button,
    # not an address — `?add-to-cart=<id>` on a WooCommerce product page
    # was live-measured as 92% of one client's entire frontier (task-82
    # §0.1), each one a distinct row that never gets captured and never
    # goes away. `frontier_decision` refuses action-shaped links into the
    # frontier entirely and strips pure-tracking query params from the
    # rest; a kill-switch, same class as `archive_live_iframes`, in case a
    # client's markup uses one of these parameter names for something else.
    archive_frontier_param_policy: bool = True

    # task-66 §1: live-measured 2026-09-05 — `synchronous=FULL`'s default
    # cost (1.42ms/commit vs 0.02ms at NORMAL, a live-benchmarked ~70x) is
    # paid INSIDE the held writer lock, so it multiplies across every other
    # writer queued behind it. NORMAL cannot corrupt the database in WAL
    # mode (SQLite's own guarantee) — the only risk is losing the last few
    # transactions on a power loss/kernel panic (not a process crash), an
    # accepted tradeoff given the nightly B2 backup (task-52).
    sqlite_synchronous: str = "NORMAL"
    # 64MiB: live-measured to make no difference on a warm page cache
    # (72.6ms vs 74.8ms on the same aggregate query) — set for a cold start
    # and long-tail latency, not because it was this task's bottleneck.
    sqlite_cache_kib: int = 65536
    sqlite_mmap_bytes: int = 268435456
    # Live-measured (task-66 §0.4): the old 30s value is itself an
    # amplifier, not a safety net — one stuck writer already means
    # `commit_with_retry`'s own backoff will retry, so a connection sitting
    # inside SQLite's driver-level busy wait for 30s just holds a pool slot
    # idle for that whole time, starving every other request behind it.
    # After the write gate (§2) removes ordinary cross-thread/cross-process
    # write contention, an actual `SQLITE_BUSY` here means something wrote
    # WITHOUT going through the gate — it should fail fast and visibly, not
    # silently absorb 30s per attempt.
    sqlite_busy_timeout_ms: int = 5000
    # QueuePool sizing (task-66 §1.2): the previous default (5 + 10
    # overflow = 15) was live-measured starved by ~140 threads across the
    # api and worker processes (monitor 16x6, archive 6x4, reports/cloud/
    # dispatcher, plus a 40-token anyio threadpool in the api process
    # alone) — one stuck writer holding a connection poisoned the whole
    # pool for everyone else, including plain reads. WAL allows unlimited
    # concurrent readers, so there is no correctness reason to keep this
    # small; it only needs to be bigger than realistic concurrent demand.
    sqlite_pool_size: int = 20
    sqlite_pool_max_overflow: int = 20
    # The async (aiosqlite) pool, task-67: task-66 §1.2 planned this and
    # sized only the sync engine, leaving every `async def` endpoint —
    # `/dashboard` included — on SQLAlchemy's stock 5 + 10.
    sqlite_async_pool_size: int = 16
    sqlite_async_pool_max_overflow: int = 16
    # Live-measured: the old 30s `pool_timeout` let a saturated pool hang a
    # request for half a minute before it even got a connection to try —
    # this is most of the operator's own reported "27 seconds, white
    # page". 5s means a saturated pool fails fast and visibly instead.
    sqlite_pool_timeout_s: float = 5.0
    # task-66 §2.3: `DbWriteGate.submit()` logs (or, under
    # `db_write_gate_strict`, raises) when a queued write job runs longer
    # than this — a long job under the one global write lock almost always
    # means network/blocking I/O leaked into a transaction (the exact bug
    # task-65 fixed once already for `get_or_create_asset`). A knob, not a
    # hardcoded constant, so a legitimately large batch commit can be
    # special-cased without a code change.
    db_write_gate_slow_ms: int = 250
    db_write_gate_strict: bool = False
    # Cross-process serialization (task-66 §2.1): `DbWriteGate` takes an
    # OS-level flock around every actual `session.commit()`, so the api and
    # worker processes — which do not otherwise coordinate at all — can
    # never both be mid-write at the same moment. `None` (the default)
    # means "derive it from `database_url`" (a `<db file>.write.lock`
    # sibling — see `app.db.session.default_write_lock_path`) rather than a
    # fixed path: a fixed path would make every test database (each its own
    # tmp file, `tests/harness/db.py`) share ONE lock with the real
    # production database bind-mounted into the very same container,
    # serializing test writes behind live production writes. Set explicitly
    # only to relocate the real deployment's lock file.
    db_write_lock_path: Path | None = None
    # task-67 §2: `0` DISABLES SQLite's automatic checkpoint, which is the
    # single biggest win measured in this whole area — bigger than
    # `synchronous=NORMAL` and far bigger than any batching.
    #
    # With the stock 1000-page (4MiB) autocheckpoint, whichever ordinary
    # commit happens to overflow the WAL pays for copying it back into the
    # main database — WHILE HOLDING THE WRITER LOCK. Live-measured on a
    # copy of the production DB, 3000 rows into the real
    # `gsc_dimension_metrics` (5 indexes):
    #
    #   autocheckpoint=1000 -> wall 432-571ms, longest single hold 334-432ms
    #   autocheckpoint=0    -> wall  88-127ms, longest single hold 6.6-88ms
    #
    # ~4-5x on throughput and up to ~50x on the longest stall any other
    # writer can see, because the checkpoint stops landing inside a random
    # worker's transaction. The work itself does not vanish — it moves to
    # `DbWriteGate`'s idle checkpointer, which runs it once, deliberately,
    # when nothing is queued (measured: 455ms, WAL 85MB -> 0).
    #
    # This is why the checkpointer is NOT optional: at `0` with nothing
    # reclaiming it, the WAL grows without bound (85MB for a 4x-daily
    # burst). Set this back to a page count only to fall back to SQLite's
    # own behaviour if the checkpointer is ever suspected.
    sqlite_wal_autocheckpoint: int = 0
    # How long `DbWriteGate`'s thread must find its queue EMPTY before it
    # spends time checkpointing. Deliberately short: the gate is the only
    # writer in the process, so an empty queue really does mean "nobody is
    # waiting", and a checkpoint that starts now is one that will not have
    # to run mid-burst later.
    db_checkpoint_idle_s: float = 2.0
    # Skip the checkpoint entirely below this WAL size — a checkpoint of a
    # nearly-empty WAL costs a lock acquisition and some syscalls to
    # reclaim almost nothing, and this thread wakes up every
    # `db_checkpoint_idle_s` on an idle system.
    db_checkpoint_min_wal_mb: float = 4.0
    # task-68 §1: how often the query planner's statistics get refreshed.
    # `sqlite_stat1` had NEVER been populated in production, live-measured
    # to cost ~4000x on the dashboard's own hot query (40ms picking the
    # wrong index vs 0.01ms after `ANALYZE`) — see `app.db.maintenance`'s
    # module docstring for the full A/B. Once a day, off-peak: `ANALYZE`
    # itself costs ~500ms on the full database, cheap next to the win, but
    # not something to run on every write.
    db_maintenance_at_utc: str = "03:00"

    # ── Прямий Google API як транспорт для #/trends (task-83) ─────────────
    # Reporting Ninja — платний посередник над безкоштовними GSC Search
    # Analytics API і GA4 Data API. Усі клієнтські property висять на одному
    # агенційному акаунті, тож одного OAuth-клієнта (`Internal` — токен без
    # 7-денного строку режиму `Testing`) і одного refresh token достатньо,
    # щоб читати їх усі напряму. Транспорт — це НЕ провайдер (task-83 §2):
    # `Provider` не змінюється, розвилка живе тут + у необов'язковому ключі
    # `client_integrations.config_json["transport"]`.
    #
    # `.env` тримає секрети (git-ignored, жоден CI/прод-конфіг їх не має);
    # `scripts/google_oauth_bootstrap.py` відтворює refresh token за 2 хв.
    # Токени НІКОЛИ не потрапляють у БД, provenance, warnings, логи чи
    # помилки, віддані UI (CLAUDE.md, PLAN §7).
    google_api_client_id: str | None = None
    google_api_secret: str | None = None
    google_api_refresh_token: str | None = None
    google_api_timeout_seconds: float = 60.0
    # Глобальний дефолт, коли `config_json["transport"]` не заданий для
    # клієнта (task-83 §6.1). `"reporting_ninja"` = сьогоднішня поведінка,
    # тобто до першої свідомої зміни нічого не змінюється ні для кого.
    # Обране `"google_api"` без `google_api_refresh_token` → клітинка стає
    # `not_configured` з явним warning, НІКОЛИ не тихий фолбек на RN.
    google_api_default_transport: str = "reporting_ninja"


    @model_validator(mode="after")
    def validate_cloud_credentials(self) -> Self:
        # `cloud_enabled=True` with missing credentials must fail loudly at
        # startup (task-52 §3.8) — a silent no-op would look identical to a
        # working off-site backup until the day it's actually needed.
        if self.cloud_enabled and not (
            self.b2_vault_bucket_name and self.b2_vault_key_id and self.b2_vault_application_key
        ):
            raise ValueError(
                "cloud_enabled=True вимагає всіх трьох B2_VAULT_BUCKET_NAME/"
                "B2_VAULT_KEY_ID/B2_VAULT_APPLICATION_KEY"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
