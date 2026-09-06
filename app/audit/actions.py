"""Явна мапа «маршрут → назва дії» (task-72).

Чому мапа, а не автогенерація зі шляху: `action` — це те, що людина читає у
фільтрі й у стрічці журналу. Автоматичне
`http.post.api.v1.clients.{client_id}.integrations.{provider}.preflight` ніхто
читати не буде, а `integration.preflight` — буде. Ціна — цей файл треба
доповнювати новими ендпоінтами; саме тому в тестах є перевірка, що кожен
мутуючий маршрут у ньому є (`tests/unit/audit/test_actions_cover_routes.py`).

`resource_param` — ім'я path-параметра, з якого береться `resource_id`, щоб
фільтр «покажи все, що робили з цим клієнтом» працював без парсингу шляху.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionSpec:
    action: str
    resource_type: str | None = None
    resource_param: str | None = None


_C = "client_id"

ROUTE_ACTIONS: dict[tuple[str, str], ActionSpec] = {
    # ── авторизація й акаунти ────────────────────────────────────────────
    ("POST", "/api/v1/auth/login"): ActionSpec("auth.login", "user"),
    ("POST", "/api/v1/auth/logout"): ActionSpec("auth.logout", "user"),
    ("POST", "/api/v1/auth/logout-all"): ActionSpec("auth.logout_all", "user"),
    ("POST", "/api/v1/admin/users"): ActionSpec("admin.user.create", "user"),
    ("PATCH", "/api/v1/admin/users/{user_id}"): ActionSpec("admin.user.update", "user", "user_id"),
    ("POST", "/api/v1/admin/users/{user_id}/reset-password"): ActionSpec(
        "admin.user.reset_password", "user", "user_id"
    ),
    ("POST", "/api/v1/admin/users/{user_id}/revoke-sessions"): ActionSpec(
        "admin.user.revoke_sessions", "user", "user_id"
    ),
    # task-79: особисті токени — «кожен видає собі сам».
    ("POST", "/api/v1/me/tokens"): ActionSpec("me.token.create", "agent_token"),
    ("DELETE", "/api/v1/me/tokens/{token_id}"): ActionSpec(
        "me.token.revoke", "agent_token", "token_id"
    ),
    # ── клієнти й інтеграції ─────────────────────────────────────────────
    ("POST", "/api/v1/clients"): ActionSpec("client.create", "client"),
    ("PATCH", "/api/v1/clients/{client_id}"): ActionSpec("client.update", "client", _C),
    ("POST", "/api/v1/clients/{client_id}/status"): ActionSpec("client.status", "client", _C),
    ("POST", "/api/v1/clients/{client_id}/logo"): ActionSpec("client.logo", "client", _C),
    ("PUT", "/api/v1/clients/{client_id}/integrations/{provider}"): ActionSpec(
        "integration.update", "client", _C
    ),
    ("POST", "/api/v1/clients/{client_id}/integrations/{provider}/discover"): ActionSpec(
        "integration.discover", "client", _C
    ),
    ("POST", "/api/v1/clients/{client_id}/integrations/{provider}/preflight"): ActionSpec(
        "integration.preflight", "client", _C
    ),
    ("POST", "/api/v1/providers/{provider}/login"): ActionSpec("provider.login", "provider"),
    # ── збір даних ───────────────────────────────────────────────────────
    ("POST", "/api/v1/sync-jobs"): ActionSpec("job.start", "job"),
    ("POST", "/api/v1/sync-jobs/{id}/cancel"): ActionSpec("job.cancel", "job", "id"),
    ("POST", "/api/v1/sync-jobs/{id}/retry"): ActionSpec("job.retry", "job", "id"),
    ("POST", "/api/v1/imports/ai-seo-tools"): ActionSpec("import.run"),
    # ── аудити, блоги, сторінки, monthly ─────────────────────────────────
    ("POST", "/api/v1/tech-audit/runs"): ActionSpec("tech_audit.run_all"),
    ("POST", "/api/v1/clients/{client_id}/tech-audit/runs"): ActionSpec(
        "tech_audit.run", "client", _C
    ),
    ("POST", "/api/v1/clients/{client_id}/tech-audit/findings/recheck"): ActionSpec(
        "tech_audit.recheck", "client", _C
    ),
    ("POST", "/api/v1/clients/{client_id}/blogs/runs"): ActionSpec("blogs.run", "client", _C),
    ("POST", "/api/v1/clients/{client_id}/pages/rebuild"): ActionSpec(
        "pages.rebuild", "client", _C
    ),
    ("POST", "/api/v1/clients/{client_id}/monthly-table/refresh"): ActionSpec(
        "monthly.refresh", "client", _C
    ),
    ("PUT", "/api/v1/clients/{client_id}/monthly-table/settings"): ActionSpec(
        "monthly.settings", "client", _C
    ),
    # ── site monitor ─────────────────────────────────────────────────────
    ("POST", "/api/v1/clients/{client_id}/monitor/runs"): ActionSpec(
        "monitor.run", "client", _C
    ),
    ("PATCH", "/api/v1/clients/{client_id}/monitor/settings"): ActionSpec(
        "monitor.settings", "client", _C
    ),
    ("POST", "/api/v1/clients/{client_id}/monitor/pages/{page_id}/check"): ActionSpec(
        "monitor.page_check", "client", _C
    ),
    ("POST", "/api/v1/clients/{client_id}/monitor/redirects/check"): ActionSpec(
        "monitor.redirects_check", "client", _C
    ),
    ("POST", "/api/v1/clients/{client_id}/monitor/robots/check"): ActionSpec(
        "monitor.robots_check", "client", _C
    ),
    ("POST", "/api/v1/clients/{client_id}/monitor/sitemap/check"): ActionSpec(
        "monitor.sitemap_check", "client", _C
    ),
    ("POST", "/api/v1/monitor/changes/{change_id}/ack"): ActionSpec(
        "monitor.change_ack", "monitor_change", "change_id"
    ),
    # ── звіти ────────────────────────────────────────────────────────────
    ("POST", "/api/v1/reports/runs"): ActionSpec("report.run", "report_run"),
    ("POST", "/api/v1/reports/runs/{run_id}/cancel"): ActionSpec(
        "report.cancel", "report_run", "run_id"
    ),
    ("POST", "/api/v1/reports/runs/{run_id}/rebuild"): ActionSpec(
        "report.rebuild", "report_run", "run_id"
    ),
    ("POST", "/api/v1/reports/runs/{run_id}/client-pdf"): ActionSpec(
        "report.client_pdf", "report_run", "run_id"
    ),
    ("POST", "/api/v1/reports/runs/{run_id}/periods/{label}/retry"): ActionSpec(
        "report.period_retry", "report_run", "run_id"
    ),
    ("POST", "/api/v1/reports/batches"): ActionSpec("report.batch_start", "report_batch"),
    ("POST", "/api/v1/reports/batches/{batch_id}/cancel"): ActionSpec(
        "report.batch_cancel", "report_batch", "batch_id"
    ),
    ("POST", "/api/v1/reports/batches/{batch_id}/clients/{client_id}/retry"): ActionSpec(
        "report.batch_retry", "report_batch", "batch_id"
    ),
    ("PUT", "/api/v1/reports/clients/{client_id}/ninja-report"): ActionSpec(
        "report.binding_update", "client", _C
    ),
    # ── web archive ──────────────────────────────────────────────────────
    ("POST", "/api/v1/archive/captures"): ActionSpec("archive.capture_start", "client"),
    ("POST", "/api/v1/archive/captures/all"): ActionSpec("archive.capture_all"),
    ("POST", "/api/v1/archive/captures/url"): ActionSpec("archive.capture_url", "archive_url"),
    ("POST", "/api/v1/archive/captures/url-adhoc"): ActionSpec("archive.capture_adhoc"),
    ("POST", "/api/v1/archive/captures/{capture_id}/cancel"): ActionSpec(
        "archive.capture_cancel", "archive_capture", "capture_id"
    ),
    # ── інструменти ──────────────────────────────────────────────────────
    ("POST", "/api/v1/tools/alt-text/reports"): ActionSpec("tool.alt_text.run", "alt_text_report"),
    ("POST", "/api/v1/tools/alt-text/uploads"): ActionSpec("tool.alt_text.upload"),
    ("DELETE", "/api/v1/tools/alt-text/reports/{report_id}"): ActionSpec(
        "tool.alt_text.delete", "alt_text_report", "report_id"
    ),
    ("PATCH", "/api/v1/tools/alt-text/reports/{report_id}/items/{item_id}"): ActionSpec(
        "tool.alt_text.edit_item", "alt_text_report", "report_id"
    ),
    (
        "POST",
        "/api/v1/tools/alt-text/reports/{report_id}/items/{item_id}/resolve-media",
    ): ActionSpec("tool.alt_text.resolve_media", "alt_text_report", "report_id"),
    ("POST", "/api/v1/tools/alt-text/reports/{report_id}/items/{item_id}/verify"): ActionSpec(
        "tool.alt_text.verify", "alt_text_report", "report_id"
    ),
    ("POST", "/api/v1/tools/alt-text/reports/{report_id}/resolve-media"): ActionSpec(
        "tool.alt_text.resolve_media_all", "alt_text_report", "report_id"
    ),
    ("POST", "/api/v1/tools/redirects/checks"): ActionSpec("tool.redirects.run", "redirect_check"),
    ("DELETE", "/api/v1/tools/redirects/checks/{check_id}"): ActionSpec(
        "tool.redirects.delete", "redirect_check", "check_id"
    ),
    (
        "POST",
        "/api/v1/tools/redirects/checks/{check_id}/targets/{target_id}/run",
    ): ActionSpec("tool.redirects.run_target", "redirect_check", "check_id"),
    ("POST", "/api/v1/tools/redirects/trace"): ActionSpec("tool.redirects.trace"),
    ("POST", "/api/v1/tools/schema-markup/checks"): ActionSpec("tool.schema_markup.run"),
    ("POST", "/api/v1/tools/strategy-check/check"): ActionSpec("tool.strategy_check.run"),
    ("POST", "/api/v1/tools/framework-check/check"): ActionSpec("tool.framework_check.run"),
    ("POST", "/api/v1/url-inspector/live"): ActionSpec("tool.url_inspector.live"),
    ("POST", "/api/v1/url-screenshots"): ActionSpec("tool.url_screenshots.capture"),
}

# Читання, які теж є діями: перегляд списку людей і самого журналу, а також
# усе, що ВИНОСИТЬ дані назовні (експорти й вивантаження файлів). Звичайні
# GET-и дашборда не журналюються — на один рендер їх десятки, і журнал
# перетворився б на лог трафіку.
READ_ACTIONS: dict[tuple[str, str], ActionSpec] = {
    ("GET", "/api/v1/admin/users"): ActionSpec("admin.user.list", "user"),
    ("GET", "/api/v1/admin/users/{user_id}/sessions"): ActionSpec(
        "admin.user.sessions", "user", "user_id"
    ),
    ("GET", "/api/v1/admin/audit"): ActionSpec("admin.audit.read"),
    ("GET", "/api/v1/admin/audit/export.csv"): ActionSpec("admin.audit.export"),
}

# Префікси шляхів, читання яких вважається винесенням даних назовні.
EXPORT_PREFIXES: tuple[str, ...] = (
    "/api/v1/artifacts",
    "/api/v1/reports/runs/",
    "/api/v1/clients/{client_id}/monthly-table/export",
)


def lookup(method: str, path_template: str) -> ActionSpec | None:
    key = (method.upper(), path_template)
    return ROUTE_ACTIONS.get(key) or READ_ACTIONS.get(key)


def fallback_action(method: str, path_template: str) -> str:
    """Для маршруту, якого ще немає в мапі: назва читабельна рівно настільки,
    щоб було видно, що саме треба довнести сюди."""
    trimmed = path_template.replace("/api/v1/", "").strip("/").replace("/", ".")
    return f"http.{method.lower()}.{trimmed}"[:80]
