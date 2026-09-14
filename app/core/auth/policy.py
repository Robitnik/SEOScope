"""Хто що може (task-71): один чекпоінт замість перевірок у 164 хендлерах.

Три правила, у порядку застосування:

1. **Публічні шляхи** — рівно те, без чого не намалюється сторінка логіну,
   плюс сам логін і `/healthz`. Це не «алов-ліст доступу», а механічний
   мінімум: усе інше закрите за замовчуванням, включно з `/index.html`,
   `/js/**`, `/api/v1/**` і replay-роутами архіву.
2. **Admin-only шляхи** — керування користувачами й дорогі/незворотні
   операції: імпорти, cloud. Список розширюваний через
   `settings.auth_admin_only_paths` без правок коду.
2a. **Admin-only на запис** — зони, де admin потрібен лише на мутацію, а
   читання відкрите всім автентифікованим: батч-звіти. Запустити батч —
   дорога дія; ПРОЧИТАТИ вже готовий звіт — щоденна робота команди.
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
        # OAuth-ендпоінти remote-конектора (task-78). Вони не «відкриті»
        # в сенсі даних: `/mcp` сам відповідає 401 із `WWW-Authenticate`,
        # а `/authorize` лише реєструє намір і веде людину на логін. Якщо
        # їх не пустити повз AuthMiddleware, вона поверне 303 на форму
        # входу — а Claude чекає 401 і на редірект просто здається.
        "/mcp",
        "/authorize",
        "/token",
        "/register",
        "/revoke",
    }
)

# Префікси зі статикою, яку тягне сторінка логіну (шрифти, лого, іконки,
# bootstrap). Дані клієнтів тут не лежать — лише брендинг і вендорні файли.
# `/.well-known/**` — discovery-документи OAuth (RFC 8414/9728). Вони за
# визначенням анонімні: клієнт читає їх ДО того, як має бодай якийсь токен.
_PUBLIC_PREFIXES = ("/assets/", "/vendor/", "/.well-known/")

# Дефолтні admin-only зони — і на читання, і на запис: імпорт переписує
# історію, cloud-операції (restore/prune) незворотні, а `/admin` — це чужі
# облікові записи й журнал дій. `/authorship` (task-93) приєднується з тієї
# самої причини, що й `/admin`: хто що створив — не рольово-інваріантна
# інформація, тож вона взагалі не потрапляє у відповіді, відкриті всім
# ролям (`GET /reports/runs` тощо лишаються незмінними для будь-кого).
DEFAULT_ADMIN_ONLY_PATHS: tuple[str, ...] = (
    "/api/v1/admin",
    "/api/v1/imports",
    "/api/v1/cloud",
    "/api/v1/authorship",
)

# Зони, де admin потрібен ЛИШЕ на мутацію, а безпечні методи відкриті всім
# автентифікованим. Батчі саме такі: один ЗАПУСК палить зовнішні квоти по
# всьому портфелю (рішення оператора — лишається за admin), але результат,
# який з нього вийшов, — звичайний робочий артефакт агенції. Поки батчі
# були admin-only цілком, member/viewer отримували 403 навіть на
# `GET /reports/batches`, а разом із ним падала вся сторінка Reports:
# `reports-page.js` вантажить історію одним `Promise.all([runs, batches])`.
# Хто хоче закрити батчі повністю, як раніше, — додає той самий префікс у
# `settings.auth_admin_only_paths`: він перевіряється першим і б'є цей список.
DEFAULT_ADMIN_WRITE_ONLY_PATHS: tuple[str, ...] = ("/api/v1/reports/batches",)

# Самообслуговування (task-79): мутація тут дозволена НАВІТЬ viewer'у, бо
# вона завжди діє лише на власний ресурс виклика (перевіряється в самому
# хендлері за `Actor`, не за параметром запиту) — рівень ролі тут про сам
# факт автентифікації, а не про право писати чужі дані. Без цього винятку
# viewer не міг би видати собі навіть read-only токен: загальне правило
# нижче вимагає `member` для будь-якого не-safe методу.
SELF_SERVICE_PATHS: tuple[str, ...] = ("/api/v1/me",)


def is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def is_safe_method(method: str) -> bool:
    return method.upper() in SAFE_METHODS


def admin_only_paths(settings: Settings | None = None) -> tuple[str, ...]:
    settings = settings or get_settings()
    return tuple(DEFAULT_ADMIN_ONLY_PATHS) + tuple(settings.auth_admin_only_paths)


def admin_write_only_paths() -> tuple[str, ...]:
    """Без конфіг-хвоста, на відміну від `admin_only_paths`: щоб посилити
    правило, префікс додають у `auth_admin_only_paths` (перевіряється
    першим і закриває шлях цілком) — послаблювати ж тут нема куди."""
    return DEFAULT_ADMIN_WRITE_ONLY_PATHS


def _matches_prefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def required_role(method: str, path: str, settings: Settings | None = None) -> UserRole:
    """Мінімальна роль для пари (метод, шлях)."""
    settings = settings or get_settings()
    for prefix in admin_only_paths(settings):
        if _matches_prefix(path, prefix):
            return UserRole.admin
    if not is_safe_method(method):
        for prefix in admin_write_only_paths():
            if _matches_prefix(path, prefix):
                return UserRole.admin
    if any(_matches_prefix(path, prefix) for prefix in SELF_SERVICE_PATHS):
        return UserRole.viewer
    if is_safe_method(method):
        return UserRole.viewer
    return UserRole.member


def role_satisfies(actual: UserRole, needed: UserRole) -> bool:
    return ROLE_ORDER.index(actual) >= ROLE_ORDER.index(needed)


def min_role(a: UserRole, b: UserRole) -> UserRole:
    """Менш привілейована з двох ролей — використовується там, де фактичне
    право = перетин двох стель (task-79: роль токена й поточна роль
    власника, `resolve_personal_token_sync`)."""
    return a if ROLE_ORDER.index(a) <= ROLE_ORDER.index(b) else b


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
    # Батч-звіт: читання відкрите всім, але ЗАПУСК лишається admin-only —
    # для UI це той самий сигнал «не малюй кнопку, яка дасть 403».
    batch_prefix = "/api/v1/reports/batches"
    if batch_prefix in admin_only_paths(settings) or batch_prefix in admin_write_only_paths():
        slugs.append("reports-batch")
    return slugs
