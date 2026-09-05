"""Спільний шар безпечних вихідних HTTP-запитів для інструментів (task-29).

Витягнуто з `app/tools/alt_text/verify.py` (task-27) без зміни поведінки:
Redirect Checker потребує **того самого** SSRF-гарда, browser-UA й
no-cache-заголовків. Дублювати гард безпеки — гарантований розсинхрон
(виправлять в одному місці, забудуть у другому), тому він живе тут, а
`verify.py` реекспортує наявні імена, щоб нічого не зламати.

Мережа — stdlib `urllib.request`: `httpx` присутній в образі лише транзитивно
через optional-групу `local` (AISeoTools), тож на CI/деплої без bind-mount
інструменти з ним зламалися б.
"""

from __future__ import annotations

import ipaddress
import secrets
import socket
import threading
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

# Частина хостингів ріже дефолтний `Python-urllib/*` як бота — тоді перевірка
# показувала б «сторінка недоступна» там, де вона цілком доступна.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 TheYovthClientPulse/1.0"
)

NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, max-age=0",
    "Pragma": "no-cache",
}

CACHE_BUSTER_PARAM = "yvcheck"
MAX_REDIRECTS = 5


class UnsafeUrlError(ValueError):
    """URL, який ми відмовляємось запитувати (SSRF-гард, PLAN §13)."""

    code = "unsafe_url"


def build_cache_busted_url(page_url: str, nonce: str | None = None) -> str:
    """Доклеїти рандомний параметр, не зламавши наявний query."""
    parts = urlsplit(page_url.strip())
    token = nonce or secrets.token_hex(4)
    pairs = [
        chunk
        for chunk in parts.query.split("&")
        if chunk and not chunk.startswith(f"{CACHE_BUSTER_PARAM}=")
    ]
    pairs.append(urlencode({CACHE_BUSTER_PARAM: token}))
    # fragment відкидаємо: серверу він однаково не надсилається.
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(pairs), ""))


def assert_public_http_url(url: str) -> None:
    """Пустити лише http(s) на публічну адресу.

    URL приходять із файлу або з поля вводу, тобто це фактично операторський
    ввід — без цього гарда інструмент став би сканером внутрішньої мережі й
    metadata-ендпоінтів. Redirect Checker застосовує гард до **кожного** хопа,
    а не лише до першого URL: `Location` теж може вести в приватну мережу.

    Відомий ліміт: між перевіркою й конектом резолв може змінитися (DNS
    rebinding). Закривати це означало б конектитись по IP із власним SNI —
    окрема робота, не виправдана для внутрішнього інструмента.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise UnsafeUrlError("Only http:// and https:// pages can be checked.")
    host = parts.hostname
    if not host:
        raise UnsafeUrlError("The page URL has no host.")
    try:
        resolved = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError as exc:
        raise UnsafeUrlError(f"Could not resolve {host}.") from exc
    for info in resolved:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError as exc:
            # Резолвер віддав щось, що ми не вміємо перевірити — відмовляємо,
            # а не пропускаємо «про всяк випадок».
            raise UnsafeUrlError(f"{host} resolved to an unrecognized address.") from exc
        if not address.is_global or address.is_multicast:
            raise UnsafeUrlError(f"{host} resolves to a non-public address ({address}).")


class GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Перевіряти SSRF-гардом кожен redirect, а не лише перший URL."""

    max_redirections = MAX_REDIRECTS

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        assert_public_http_url(urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)  # type: ignore[arg-type]


def safe_host(url: str) -> str:
    """Хост для повідомлень і логів.

    URL можуть містити токени в query (`?access_key=…`), тож у деталях помилок
    і в логах фігурує **лише хост**, ніколи повний URL (PLAN §13).
    """
    return urlsplit(url).hostname or "the host"


class HostThrottle:
    """Не більше `concurrency` одночасних запитів на хост + мінімальний
    інтервал.

    Гард живе на сервері, а не на фронті: вставлений список на 200 URL одного
    клієнта інакше став би міні-нагрузочним тестом його сайту, і покладатися в
    такому на клієнтський JS не можна.

    `concurrency=1` (дефолт) — це рівно наявна поведінка байт-в-байт: повний
    м'ютекс на хост, `min_interval` рахується від моменту, коли попередній
    запит **завершився**. Кожен наявний виклик (`app/tools/redirects`,
    `app/tools/alt_text`) створює `HostThrottle()` без аргументів і має цю
    поведінку не чіпати.

    `concurrency > 1` (task-58, Site Monitor's паралельний обхід) — окрема
    гілка: до `concurrency` запитів на хост одночасно, `min_interval`
    рахується від моменту, коли попередній запит **стартував**, а не
    завершився (інакше найповільніша сторінка сайту диктувала б темп усім
    іншим). Обидві гілки поділяють один клас, щоб дозвіл на паралелізм
    залишався локальним рішенням викликача (`HostThrottle(concurrency=N)`),
    а не окремим типом, який довелося б синхронізувати з цим.
    """

    def __init__(
        self,
        min_interval_seconds: float = 0.25,
        ttl_seconds: float = 300.0,
        *,
        concurrency: int = 1,
    ) -> None:
        self._min_interval = min_interval_seconds
        self._ttl = ttl_seconds
        self._concurrency = max(1, concurrency)
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._last_call: dict[str, float] = {}
        # Only used when `concurrency > 1` — kept separate from the fields
        # above so the `concurrency == 1` path never touches new state.
        self._semaphores: dict[str, threading.Semaphore] = {}
        self._start_locks: dict[str, threading.Lock] = {}
        self._last_start: dict[str, float] = {}

    @contextmanager
    def slot(self, host: str) -> Iterator[None]:
        key = host.casefold()
        if self._concurrency == 1:
            with self._guard:
                self._prune()
                lock = self._locks.setdefault(key, threading.Lock())
            with lock:
                wait = self._min_interval - (time.monotonic() - self._last_call.get(key, 0.0))
                if 0 < wait <= self._min_interval:
                    time.sleep(wait)
                try:
                    yield
                finally:
                    self._last_call[key] = time.monotonic()
            return

        with self._guard:
            self._prune()
            semaphore = self._semaphores.setdefault(key, threading.Semaphore(self._concurrency))
            start_lock = self._start_locks.setdefault(key, threading.Lock())
        semaphore.acquire()
        try:
            with start_lock:
                wait = self._min_interval - (time.monotonic() - self._last_start.get(key, 0.0))
                if 0 < wait <= self._min_interval:
                    time.sleep(wait)
                self._last_start[key] = time.monotonic()
            yield
        finally:
            semaphore.release()

    def _prune(self) -> None:
        """Не тримати запис для кожного хоста, який колись бачили."""
        cutoff = time.monotonic() - self._ttl
        stale = [key for key, seen in self._last_call.items() if seen < cutoff]
        for key in stale:
            self._last_call.pop(key, None)
            lock = self._locks.get(key)
            if lock is not None and not lock.locked():
                self._locks.pop(key, None)
        stale_starts = [key for key, seen in self._last_start.items() if seen < cutoff]
        for key in stale_starts:
            self._last_start.pop(key, None)
