"""Application Passwords для `redirection/v1` — файлом поза БД (task-51 §3.3).

У проєкті немає механізму шифрованих полів у БД: `ClientIntegration.config_json`
навмисно документований як «non-secret external identifiers only», а заводити
залежність на криптобібліотеку — зона task-01. Той самий прецедент, що
`browser_profiles_dir`/`app_data/artifacts`: секрет живе на диску поза версійним
контролем, у БД і в API — лише похідний стан (`configured|missing|invalid`),
ніколи значення.

Формат файлу — плаский JSON `{"<client_id>": {"user": "...", "app_password": "..."}}`,
права `0600`. Рекомендація для оператора (документується в таск-плані, не в
коді): заводити на сайті клієнта окремого користувача з роллю, обмеженою
фільтром `redirection_role`, а не адміна — пароль адміна дає повний доступ до
сайту, і тримати такий без потреби не варто.
"""

from __future__ import annotations

import base64
import json
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.core.logging import add_redact_keys, add_redact_patterns

# Самореєстрація за контрактом `app/core/logging.py` («модулі, що працюють із
# секретами, самі реєструють свої патерни»): `authorization`/`password` там
# уже у дефолтних ключах, але наш ключ у файлі креденшелів — саме
# `app_password`, і рядок заголовка Basic auth треба ловити і у вільному
# тексті (traceback, форматоване повідомлення), не лише в ключах словника.
# `Cookie: ...` — той самий клас ризику для `wp_browser.py`'s (task-51
# follow-up) сесійні cookies: не пароль, але повноцінний авторизований токен.
add_redact_keys({"app_password"})
add_redact_patterns([r"Basic [A-Za-z0-9+/=]+", r"Cookie: [^\n]+"])


class CredentialsStatus(StrEnum):
    configured = "configured"
    missing = "missing"
    invalid = "invalid"


@dataclass(frozen=True)
class WpCredentials:
    """`app_password` ніколи не йде в лог чи повідомлення помилки — лише

    в заголовок Basic auth одного запиту. Власний `__repr__` — щоб випадковий
    `logger.debug(creds)` чи вивід у traceback не показав пароль у відкритому
    вигляді (дефолтний dataclass-репр показав би обидва поля як є).
    """

    user: str
    app_password: str

    def __repr__(self) -> str:
        return f"WpCredentials(user={self.user!r}, app_password='***')"


def _load_raw(path: Path) -> dict[str, Any] | None:
    """`None` — «файлу немає» і «файл битий» разом: обидва зводяться до

    того самого «немає, звідки читати» для викликача, різницю видає
    `credentials_status`, а не виняток.
    """
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def load_credentials(path: Path, client_id: str) -> WpCredentials | None:
    """`None` — немає запису для цього клієнта, файл битий, або запис

    неповний. Відсутні креденшели — звичайний, очікуваний стан (PLAN §12),
    не помилка: клієнт, для якого їх не завели, просто йде в probe-режим.
    """
    raw = _load_raw(path)
    if raw is None:
        return None
    entry = raw.get(client_id)
    if not isinstance(entry, dict):
        return None
    user = entry.get("user")
    app_password = entry.get("app_password")
    if not isinstance(user, str) or not user:
        return None
    if not isinstance(app_password, str) or not app_password:
        return None
    return WpCredentials(user=user, app_password=app_password)


def credentials_status(path: Path, client_id: str) -> CredentialsStatus:
    """Похідний стан для API/UI — ніколи не значення секрету."""
    if not path.exists():
        return CredentialsStatus.missing
    raw = _load_raw(path)
    if raw is None:
        return CredentialsStatus.invalid
    if client_id not in raw:
        return CredentialsStatus.missing
    if load_credentials(path, client_id) is None:
        return CredentialsStatus.invalid
    return CredentialsStatus.configured


def basic_auth_header(creds: WpCredentials) -> str:
    token = f"{creds.user}:{creds.app_password}".encode()
    return f"Basic {base64.b64encode(token).decode('ascii')}"


def write_credentials(path: Path, client_id: str, *, user: str, app_password: str) -> None:
    """Мерджить один запис у файл, створює каталог/файл із правами `0600`.

    Викликається лише руками оператора (REPL/скрипт у контейнері) — форми в
    UI для секретів немає, той самий прецедент, що досі не заведена форма
    для `MonitorSettings.base_url_override`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _load_raw(path) or {}
    raw[client_id] = {"user": user, "app_password": app_password}
    path.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


# --- regular wp-admin login (browser fallback, task-51 follow-up 2026-08-19) -
#
# Live finding: no client in the portfolio had an Application Password ever
# issued — `wp_redirection.py`'s Basic-Auth path returned `auth_failed`
# everywhere it was tried, while the agency's own regular wp-admin login
# worked. A REGULAR account password is a materially bigger secret than an
# Application Password (full login, not one revocable-without-touching-the-
# account token) — kept in a SEPARATE file from `WpCredentials` on purpose,
# so the two are never confused about which kind of credential a record
# holds, and so one can be rotated/removed independently of the other.


@dataclass(frozen=True)
class WpLoginCredentials:
    """A REGULAR wp-admin login — `app/monitor/redirects/wp_browser.py`'s

    fallback when no Application Password is configured or working. Same
    repr-masking discipline as `WpCredentials`.
    """

    login: str
    password: str

    def __repr__(self) -> str:
        return f"WpLoginCredentials(login={self.login!r}, password='***')"


def load_login_credentials(path: Path, client_id: str) -> WpLoginCredentials | None:
    raw = _load_raw(path)
    if raw is None:
        return None
    entry = raw.get(client_id)
    if not isinstance(entry, dict):
        return None
    login = entry.get("login")
    password = entry.get("password")
    if not isinstance(login, str) or not login:
        return None
    if not isinstance(password, str) or not password:
        return None
    return WpLoginCredentials(login=login, password=password)


def login_credentials_status(path: Path, client_id: str) -> CredentialsStatus:
    if not path.exists():
        return CredentialsStatus.missing
    raw = _load_raw(path)
    if raw is None:
        return CredentialsStatus.invalid
    if client_id not in raw:
        return CredentialsStatus.missing
    if load_login_credentials(path, client_id) is None:
        return CredentialsStatus.invalid
    return CredentialsStatus.configured


def write_login_credentials(path: Path, client_id: str, *, login: str, password: str) -> None:
    """Same merge/create/`0600` contract as `write_credentials` — a distinct

    function (not a shared helper parametrized by field names) so the two
    call sites stay unambiguous about which secret shape they're writing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _load_raw(path) or {}
    raw[client_id] = {"login": login, "password": password}
    path.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
