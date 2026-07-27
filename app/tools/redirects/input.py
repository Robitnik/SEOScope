"""Нормалізація вставленого списку посилань (task-29).

Реальний вхід — не чистий список URL, а те, що оператор скопіював: колонка з
Ahrefs/GSC-експорту, markdown-посилання з тікета, рядок із комами, bullet-список
із дока. Тому парсер терпимий до сміття навколо URL, але **показує** кожну свою
правку (`notes`), а не «виправляє» посилання молча — інструмент не має права
перевіряти щось інше, ніж дав оператор.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

# Cap на прогін: 200 URL — це вже кілька хвилин реальних запитів до чужого
# сайту. Більше — свідомо не тихо приймаємо, а просимо розбити список.
MAX_URLS = 200
MAX_URL_LENGTH = 2048

_LIST_MARKER = re.compile(r"^(?:[-*•>]+|\d+[.)])\s+")
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\((?P<url>[^)\s]+)\)")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
# Обгортки, які приїжджають разом зі скопійованим URL.
_WRAPPERS = "\"'`<>«»‹›“”‘’()[]{}"
# Хвостова пунктуація речення. `/` і `-` не чіпаємо: вони — частина URL.
_TRAILING_PUNCTUATION = ".,;:!?"

SUPPORTED_SCHEMES = ("http", "https")


class UrlInputError(ValueError):
    """Рядок, який не є перевірним посиланням."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class UrlEntry:
    """Один прийнятий URL: як вставили + що ми з ним зробили."""

    input_url: str
    url: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RejectedUrl:
    input_url: str
    error_code: str
    message: str


@dataclass(frozen=True)
class ParsedUrls:
    entries: tuple[UrlEntry, ...]
    rejected: tuple[RejectedUrl, ...] = ()
    # Скільки рядків прибрала дедуплікація — видиме число, не тихе зникнення.
    duplicate_count: int = 0
    # Скільки токенів узагалі знайшли (до дедуплікації та cap-у).
    found_count: int = 0
    truncated: bool = False


def _encode_non_ascii(value: str) -> str:
    """Percent-encode лише те, що інакше зламає запит.

    Вже закодовані послідовності (`%D1%84`) лишаються як є — інакше отримали б
    подвійне кодування й запит по неправильному шляху.
    """
    return quote(value, safe="".join(chr(code) for code in range(33, 127)))


def normalize_url(raw: str) -> UrlEntry:
    """Привести вставлений рядок до URL, який можна запитати.

    Кидає `UrlInputError` для того, що посиланням не є (`mailto:`, `javascript:`,
    порожній хост). Це **не** помилка оператора — рядок лишається у звіті з
    чесним кодом причини.
    """
    original = raw.strip()
    token = original
    notes: list[str] = []

    # Чистимо по колу: «…». дає обгортку **після** точки, тож один прохід
    # лишив би `»` у кінці URL.
    while True:
        unwrapped = token.strip(_WRAPPERS + " \t")
        if unwrapped != token and unwrapped:
            token = unwrapped
            if "wrapper_removed" not in notes:
                notes.append("wrapper_removed")
            continue
        tail_trimmed = token.rstrip(_TRAILING_PUNCTUATION)
        if tail_trimmed != token and tail_trimmed:
            token = tail_trimmed
            if "trailing_punctuation_removed" not in notes:
                notes.append("trailing_punctuation_removed")
            continue
        break

    if not token:
        raise UrlInputError("invalid_url", "This line has no URL in it.")

    if token.startswith("//"):
        token = f"https:{token}"
        notes.append("scheme_added")
    elif "://" not in token:
        if _SCHEME.match(token):
            scheme = token.split(":", 1)[0].lower()
            raise UrlInputError(
                "unsupported_scheme",
                f"“{scheme}:” links cannot be traced — only http:// and https:// can.",
            )
        token = f"https://{token}"
        notes.append("scheme_added")

    parts = urlsplit(token)
    if parts.scheme.lower() not in SUPPORTED_SCHEMES:
        raise UrlInputError(
            "unsupported_scheme",
            f"“{parts.scheme.lower()}:” links cannot be traced — "
            "only http:// and https:// can.",
        )
    if not parts.hostname:
        raise UrlInputError("invalid_url", "This URL has no host.")
    if "." not in parts.hostname and parts.hostname != "localhost":
        # Без точки це не публічне імʼя (`intranet`, друкарська помилка) — SSRF-гард
        # усе одно відмовить, але сказати про це варто одразу й зрозуміло.
        raise UrlInputError("invalid_url", f"“{parts.hostname}” is not a public host name.")

    netloc = parts.netloc
    if any(ord(char) > 127 for char in parts.hostname):
        try:
            encoded_host = parts.hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise UrlInputError("invalid_url", "This host name cannot be encoded.") from exc
        netloc = netloc.replace(parts.hostname, encoded_host)
        notes.append("idn_encoded")
    netloc = netloc.lower()

    if parts.fragment:
        # Фрагмент серверу не надсилається — тримати його означало б показувати
        # оператору URL, який ми не запитували.
        notes.append("fragment_dropped")

    path = _encode_non_ascii(parts.path) or "/"
    url = urlunsplit((parts.scheme.lower(), netloc, path, _encode_non_ascii(parts.query), ""))
    if len(url) > MAX_URL_LENGTH:
        raise UrlInputError("invalid_url", f"This URL is longer than {MAX_URL_LENGTH} characters.")
    return UrlEntry(input_url=original[:MAX_URL_LENGTH], url=url, notes=tuple(notes))


def _tokens(text: str) -> list[str]:
    """Витягнути кандидатів у URL з довільно вставленого тексту."""
    tokens: list[str] = []
    for line in text.splitlines():
        candidate = _LIST_MARKER.sub("", line.strip())
        if not candidate or candidate.startswith("#"):
            continue
        # Markdown-посилання: беремо target, підпис відкидаємо.
        links = _MARKDOWN_LINK.findall(candidate)
        if links:
            tokens.extend(links)
            continue
        # У URL не буває неекранованих пробілів і коми між посиланнями — тож
        # розбиття по них безпечне й покриває «a.com, b.com» в одному рядку.
        chunks = [chunk for chunk in re.split(r"[\s,;]+", candidate) if chunk]
        # …але якщо жоден кусок не схожий на посилання, це просто текстовий
        # рядок: рахувати «not a url» як три відкинуті кандидати означало б
        # засипати оператора шумом і збрехати в лічильнику «прислали N».
        tokens.extend(chunks if any(_looks_like_url(chunk) for chunk in chunks) else [candidate])
    return tokens


def _looks_like_url(chunk: str) -> bool:
    return "://" in chunk or "." in chunk.strip(_WRAPPERS + _TRAILING_PUNCTUATION)


def parse_url_list(text: str, *, max_urls: int = MAX_URLS) -> ParsedUrls:
    """Розібрати текст із textarea у впорядкований, дедуплікований список."""
    entries: list[UrlEntry] = []
    rejected: list[RejectedUrl] = []
    seen: set[str] = set()
    duplicates = 0
    truncated = False

    tokens = _tokens(text)
    for token in tokens:
        try:
            entry = normalize_url(token)
        except UrlInputError as exc:
            rejected.append(
                RejectedUrl(input_url=token[:MAX_URL_LENGTH], error_code=exc.code, message=str(exc))
            )
            continue
        if entry.url in seen:
            duplicates += 1
            continue
        if len(entries) >= max_urls:
            truncated = True
            continue
        seen.add(entry.url)
        entries.append(entry)

    return ParsedUrls(
        entries=tuple(entries),
        rejected=tuple(rejected),
        duplicate_count=duplicates,
        found_count=len(tokens),
        truncated=truncated,
    )
