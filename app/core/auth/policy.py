"""Хто що може (task-71): один чекпоінт замість перевірок у 164 хендлерах.

Три правила, у порядку застосування:

1. **Публічні шляхи** — рівно те, без чого не намалюється сторінка логіну,
   плюс сам логін і `/healthz`. Це не «алов-ліст доступу», а механічний
   мінімум: усе інше закрите за замовчуванням, включно з `/index.html`,
   `/js/**`, `/api/v1/**` і replay-роутами архіву.
2. **Admin-only шляхи** — керування користувачами й дорогі/незворотні
   операції: батч-звіти, імпорти, cloud. Список розширюваний через
   `settings.auth_admin_only_paths` без правок коду.
3. **Метод** — `viewer` бачить усе, але будь-який мутуючий метод для нього
   403. Решта (`member`) — звичайна щоденна робота.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.schemas.enums import ROLE_ORDER, UserRole

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Точні шляхи, доступні без сесії.
_PUBLIC_EXACT = frozenset(
    {
        "/login.html",
        "/js/login.js",
        "/css/login.css",
        "/css/tokens.css",
        "/favicon.ico",
        "/healthz",
        "/api/v1/auth/login",
        "/api/v1/auth/session",
    }
)

# Префікси зі статикою, яку тягне сторінка логіну (шрифти, лого, іконки,
# bootstrap). Дані клієнтів тут не лежать — лише брендинг і вендорні файли.
_PUBLIC_PREFIXES = ("/assets/", "/vendor/")

# Дефолтні admin-only зони. Батчі — свідомо тут: один запуск batch-звітів
# витрачає зовнішні квоти по всьому портфелю, імпорт переписує історію, а
# cloud-операції (restore/prune) незворотні.
DEFAULT_ADMIN_ONLY_PATHS: tuple[str, ...] = (
    "/api/v1/admin",
    "/api/v1/reports/batches",
    "/api/v1/imports",
    "/api/v1/cloud",
)


def is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def is_safe_method(method: str) -> bool:
    return method.upper() in SAFE_METHODS


def admin_only_paths(settings: Settings | None = None) -> tuple[str, ...]:
    settings = settings or get_settings()
    return tuple(DEFAULT_ADMIN_ONLY_PATHS) + tuple(settings.auth_admin_only_paths)


def required_role(method: str, path: str, settings: Settings | None = None) -> UserRole:
    """Мінімальна роль для пари (метод, шлях)."""
    settings = settings or get_settings()
    for prefix in admin_only_paths(settings):
        if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            return UserRole.admin
    if is_safe_method(method):
        return UserRole.viewer
    return UserRole.member


def role_satisfies(actual: UserRole, needed: UserRole) -> bool:
    return ROLE_ORDER.index(actual) >= ROLE_ORDER.index(needed)


def admin_only_tool_slugs(settings: Settings | None = None) -> list[str]:
    """Слаги інструментів, які фронт має ховати від non-admin.

    Береться з тієї самої конфігурації, що й серверна заборона, — інакше UI
    і бекенд розійшлися б, і користувач бачив би кнопку, яка завжди дає 403.
    """
    settings = settings or get_settings()
    slugs: list[str] = []
    for prefix in admin_only_paths(settings):
        marker = "/api/v1/tools/"
        if prefix.startswith(marker):
            slug = prefix[len(marker) :].strip("/")
            if slug:
                slugs.append(slug)
    if "/api/v1/reports/batches" in admin_only_paths(settings):
        slugs.append("reports-batch")
    return slugs
