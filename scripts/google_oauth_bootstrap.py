"""Разовий OAuth-bootstrap для прямих Google API (GSC + GA4).

Навіщо: Reporting Ninja — платний посередник над безкоштовними Google API.
Оскільки ВСІ клієнтські property висять на одному агенційному акаунті,
достатньо одного `refresh_token`, щоб читати їх усі напряму.

Запуск (api-контейнер має `network_mode: host`, тож 127.0.0.1 у браузері
хоста і в контейнері — це те саме, loopback-редірект працює):

    docker compose exec api poetry run python scripts/google_oauth_bootstrap.py

Креденшели береться з `.env` (`google_api_client_id` / `google_api_secret`),
а не з argv: аргументи командного рядка видно в `ps` і вони осідають
у логах/транскриптах, а секрети туди не потрапляють (PLAN §7, CLAUDE.md).
З тієї ж причини отриманий `refresh_token` НЕ друкується, а дописується
назад у `.env` — на екран іде лише факт запису.

Свідомо stdlib-only: залежності проєкту міняє лише task-01 (CLAUDE.md),
а це разовий bootstrap, а не частина рантайму.
"""

from __future__ import annotations

import argparse
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = (
    "https://www.googleapis.com/auth/webmasters.readonly "
    "https://www.googleapis.com/auth/analytics.readonly "
    # task-90: Drive/Docs/Sheets на читання. Один `drive.readonly` замість трьох
    # вужчих (`documents.readonly`/`spreadsheets.readonly` + щось для переліку):
    # перегляд Диска в UI — це `files.list`, а він не працює під вужчими, тож
    # restricted scope усе одно потрібен, і три поверх нього дали б не менше
    # прав, а лише довший рядок consent. Review Google не потрібен, бо audience
    # застосунку — Internal (task-90 §1.2).
    "https://www.googleapis.com/auth/drive.readonly"
)
REFRESH_KEY = "GOOGLE_API_REFRESH_TOKEN"
ID_KEYS = ("google_api_client_id", "GOOGLE_API_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_ID")
SECRET_KEYS = ("google_api_secret", "GOOGLE_API_SECRET", "GOOGLE_OAUTH_CLIENT_SECRET")


def _load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _pick(env: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if env.get(key):
            return env[key]
    return None


def _store_refresh_token(path: Path, token: str) -> None:
    """Замінює наявний рядок або дописує новий — щоб повторний запуск не плодив дублікати."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{REFRESH_KEY}="):
            lines[i] = f"{REFRESH_KEY}={token}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{REFRESH_KEY}={token}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class _CodeCatcher(BaseHTTPRequestHandler):
    """Ловить один redirect із `?code=...` і глушить лог доступу."""

    code: str | None = None
    state: str | None = None

    def do_GET(self) -> None:  # noqa: N802 — контракт BaseHTTPRequestHandler
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CodeCatcher.code = (params.get("code") or [None])[0]
        _CodeCatcher.state = (params.get("state") or [None])[0]
        body = b"<h2>OK. You can close this tab and return to the terminal.</h2>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


def _post_form(url: str, payload: dict[str, str]) -> dict:
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        # Тіло помилки Google несе лише код (`invalid_grant` тощо), не секрет.
        return {"__error__": f"{exc.code} {exc.read().decode('utf-8', 'replace')[:300]}"}


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:  # інвентар — best-effort, не валить bootstrap
        return {"__error__": f"{exc.code} {exc.read().decode('utf-8', 'replace')[:400]}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default="/app/.env")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    env_path = Path(args.env_file)
    env = _load_dotenv(env_path)
    client_id = _pick(env, ID_KEYS)
    client_secret = _pick(env, SECRET_KEYS)
    if not client_id or not client_secret:
        print(f"!! У {env_path} немає google_api_client_id / google_api_secret.")
        return 2

    redirect_uri = f"http://127.0.0.1:{args.port}"
    state = secrets.token_urlsafe(16)
    auth_url = AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            # offline + consent — єдиний спосіб гарантовано отримати refresh_token:
            # без `prompt=consent` Google віддає його лише при ПЕРШІЙ згоді акаунта,
            # тож повторний запуск інакше повернув би тільки access_token.
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )

    print("\n1) Відкрий це посилання (під агенційним акаунтом):\n")
    print(auth_url)
    print(f"\n2) Чекаю redirect на {redirect_uri} ...")

    server = HTTPServer(("127.0.0.1", args.port), _CodeCatcher)
    server.timeout = 300
    server.handle_request()
    server.server_close()

    if not _CodeCatcher.code:
        print("!! Код не отримано (відмова у згоді або таймаут 5 хв).")
        return 1
    if _CodeCatcher.state != state:
        print("!! state mismatch — редірект не наш, відкидаю.")
        return 1

    tokens = _post_form(
        TOKEN_URL,
        {
            "code": _CodeCatcher.code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    if "__error__" in tokens:
        print(f"!! Обмін коду на токен не вдався: {tokens['__error__']}")
        return 1

    access = tokens.get("access_token", "")
    refresh = tokens.get("refresh_token")
    if refresh:
        _store_refresh_token(env_path, refresh)
        print(f"\n[ok] {REFRESH_KEY} записано у {env_path} (не друкую — це секрет).")
    else:
        print("\n!! refresh_token не прийшов. Відклич доступ на")
        print("   https://myaccount.google.com/permissions і повтори.")

    print("\n=== GSC sites ===")
    gsc = _get_json("https://www.googleapis.com/webmasters/v3/sites", access)
    if "__error__" in gsc:
        print(gsc["__error__"])
    else:
        entries = gsc.get("siteEntry", [])
        print(f"{len(entries)} шт.")
        for site in sorted(entries, key=lambda s: str(s.get("siteUrl"))):
            print(f"  {site.get('permissionLevel', ''):<22} {site.get('siteUrl')}")

    print("\n=== GA4 properties ===")
    ga = _get_json(
        "https://analyticsadmin.googleapis.com/v1beta/accountSummaries?pageSize=200",
        access,
    )
    if "__error__" in ga:
        print(ga["__error__"])
    else:
        total = 0
        for acc in ga.get("accountSummaries", []):
            print(f"  [{acc.get('displayName')}]")
            for prop in acc.get("propertySummaries", []):
                total += 1
                # `properties/123456789` — саме цей id іде в runReport
                print(f"    {str(prop.get('property')):<28} {prop.get('displayName')}")
        print(f"  разом: {total}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
