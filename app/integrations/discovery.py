"""Пошук провайдерського акаунта за доменом клієнта (task-33).

Кнопка «Connect» в UI більше не форма для ручного вписування ID: цей модуль
знаходить акаунт у Reporting Ninja за доменом (GSC) або за назвою (GA4) і
віддає готовий `integration_config`, який ендпоінт лише записує.

Модуль свідомо не знає ні про БД, ні про FastAPI — це чистий матчинг, тому
покривається юнітами без стенду.

`seo_audit` імпортується **тільки лениво, всередині тіла функції** — як у всіх
адаптерах, щоб середовище без чекауту AISeoTools (CI) не падало на імпорті.

Секрети сюди не потрапляють: `connection_key` — це Google-акаунт (email), явно
класифікований як неcекрет у `docs/security.md`. `REPORTING_NINJA_API_KEY` цей
модуль не читає взагалі — його бере всередині себе `ReportingNinjaClient`.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Any, Literal

from app.importers.client_mapping import normalize_domain
from app.schemas.enums import Provider, SourceMode

Confidence = Literal["exact", "strong", "weak"]
DiscoveryStatus = Literal["connected", "needs_choice", "not_found", "unavailable"]

# Reporting Ninja `integration_id` для двох потрібних нам джерел.
GSC_INTEGRATION_ID = "google_search_console"
GA4_INTEGRATION_ID = "ga4"

# Пороги матчингу. `_STRONG_SCORE` — межа, з якої збіг вважається достатнім для
# автозапису; `_AUTO_MARGIN` — обов'язковий відрив від наступного кандидата, щоб
# два однаково схожі акаунти ніколи не підключились «навмання».
_STRONG_SCORE = 0.92
_WEAK_SCORE = 0.55
_AUTO_MARGIN = 0.15
# Інвентар з'єднання цілком: фронт показує правдоподібні одразу, решту — за
# кнопкою і через пошук, тож обрізати список немає сенсу (у Ninja їх ~30).
_MAX_INVENTORY = 100

# Входження коротшого рядка в довший рахуємо окремим сигналом, але лише від цієї
# довжини — інакше «nyc» збігалося б із половиною ростера.
_MIN_CONTAINMENT_CHARS = 6
_CONTAINMENT_SCORE = 0.93

_CACHE_TTL_SECONDS = 60.0

# Історична таблиця з task-07: залишається як точний override на випадок, коли
# назва в Ninja не має нічого спільного з доменом. Евристика нижче виводить усі
# ці 11 записів і сама (див. `test_aliases_are_reproduced_by_fuzzy_matching`),
# тому таблиця більше не є єдиним шляхом — лише страховкою.
GA4_ACCOUNT_ALIASES: dict[str, str] = {
    "19cleveland.com": "19 Cleveland - GA4",
    "exxxoticaexpo.com": "EXXXOTICA Expo - Homepage - GA4",
    "effyscafe.com": "Effys Cafe",
    "grillpointnyc.com": "Grill Point NYC",
    "hearwellservices.com": "Hear Well Services",
    "nychearing.com": "NYC Hearing Associate",
    "nysapphire.com": "http://www.nysapphire.com - GA4",
    "pjclarkes.com": "PJ Clarke's - GA4",
    "shawarma-mia.com": "Shawarma-Mia",
    "shorelinedentalstudio.com": "Shoreline Dental Studio GA4",
    "smashhouseburgers.com": "Smash House Burgers",
}

_TRAILING_ID_RE = re.compile(r"\s*\(\d+\)\s*$")
_GA4_TOKEN_RE = re.compile(r"\bga\s*[-_]?\s*4\b", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class IntegrationCandidate:
    """Один варіант підключення, готовий до запису.

    `integration_config` — саме те, що ляже в `client_integrations.config_json`,
    тож ендпоінту не треба нічого добудовувати.
    """

    label: str
    detail: str
    confidence: Confidence
    reason: str
    score: float
    source_mode: SourceMode
    integration_config: dict[str, Any]
    # Чи варто показувати цей варіант одразу. `False` — це просто інвентар
    # з'єднання: він потрібен, щоб оператор бачив, що взагалі доступне, але
    # вивалювати 20+ нерелевантних рядків за замовчуванням не можна.
    suggested: bool = True
    # Пряма ознака, а не статистична схожість: явний аліас або назва акаунта,
    # що сама резолвиться в домен клієнта. Такий збіг застосовується попри
    # наявність не менш схожого сусіда — інакше «19 Cleveland» програвало б
    # нічию задекларованому «19 Cleveland - GA4».
    decisive: bool = False


@dataclass(frozen=True)
class DiscoveryOutcome:
    status: DiscoveryStatus
    candidates: list[IntegrationCandidate]
    auto_apply: IntegrationCandidate | None
    message: str


# --- нормалізація назв -----------------------------------------------------


def _strip_decorations(raw: str) -> str:
    """Прибрати те, чим Ninja прикрашає назву акаунта.

    Два стійких патерни: числовий хвіст `Client GA4 (123456)` і сам токен
    «GA4» будь-де в рядку.
    """
    value = _TRAILING_ID_RE.sub("", (raw or "").strip())
    value = _GA4_TOKEN_RE.sub(" ", value)
    return value.strip().strip("-–—").strip()


def _name_key(raw: str) -> str:
    """Порівнювана форма назви: лише малі літери й цифри."""
    return _NON_ALNUM_RE.sub("", _strip_decorations(raw).casefold())


def _domain_of_name(raw: str) -> str | None:
    """Назва акаунта інколи сама є URL — `http://www.nysapphire.com - GA4`.

    У такому разі домен із назви є точнішим сигналом за будь-яку схожість
    рядків, тож пробуємо його першим.
    """
    stripped = _strip_decorations(raw)
    if not stripped:
        return None
    head = stripped.split()[0]
    if "://" not in head and not head.lower().startswith("www."):
        return None
    return normalize_domain(head)


def _similarity(name_key: str, target: str) -> float:
    if not name_key or not target:
        return 0.0
    if name_key == target:
        return 1.0
    ratio = SequenceMatcher(None, name_key, target).ratio()
    # Ninja часто дописує до назви зайве: «NYC Hearing Associate» для
    # nychearing.com, «EXXXOTICA Expo - Homepage» для exxxoticaexpo.com. Голий
    # ratio такі випадки топить (0.69), хоча збіг очевидний, — тому входження
    # коротшого рядка в довший рахуємо окремим сильним сигналом.
    shorter, longer = (name_key, target) if len(name_key) <= len(target) else (target, name_key)
    if len(shorter) >= _MIN_CONTAINMENT_CHARS and shorter in longer:
        return max(ratio, _CONTAINMENT_SCORE)
    return ratio


def _match_targets(domain: str, client_name: str) -> list[str]:
    """Проти чого міряємо схожість назви акаунта.

    Домен без TLD — найкорисніший орієнтир (`laxton.se` → `laxton`), але назва
    клієнта інколи єдина зачіпка, коли домен і бренд розходяться.
    """
    normalized = normalize_domain(domain) or ""
    targets = [_name_key(normalized), _name_key(client_name)]
    if "." in normalized:
        targets.append(_name_key(normalized.rsplit(".", 1)[0]))
    return [target for target in dict.fromkeys(targets) if target]


def _tier(score: float) -> Confidence:
    if score >= 1.0:
        return "exact"
    if score >= _STRONG_SCORE:
        return "strong"
    return "weak"


# --- транспорт -------------------------------------------------------------


def list_ninja_accounts(integration_id: str, *, use_cache: bool = True) -> list[dict[str, Any]]:
    """`connections` з Reporting Ninja, з коротким TTL-кешем.

    Оператор зазвичай клікає кілька рядків поспіль; ліміт API — 300 запитів на
    хвилину, тож повторювати однакову вибірку на кожен клік немає сенсу.
    """
    now = time.monotonic()
    if use_cache:
        with _cache_lock:
            cached = _cache.get(integration_id)
            if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
                return cached[1]

    from seo_audit.clientreport.api_client import ReportingNinjaClient

    with ReportingNinjaClient() as client:
        connections = list(client.list_connections(integration_id))

    with _cache_lock:
        _cache[integration_id] = (time.monotonic(), connections)
    return connections


def reset_cache() -> None:
    """Скинути кеш — для тестів і для повторної спроби після збою."""
    with _cache_lock:
        _cache.clear()


def _iter_accounts(connections: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Розкласти вкладену структуру Ninja у `(connection_key, id, name)`."""
    rows: list[tuple[str, str, str]] = []
    for connection in connections:
        connection_key = str(connection.get("connection_key") or "")
        for account in connection.get("accounts") or []:
            account_id = str(account.get("account_id") or "")
            account_name = str(account.get("account_name") or "")
            if connection_key and account_id:
                rows.append((connection_key, account_id, account_name))
    return rows


# --- матчери, спільні з імпортером (task-07) -------------------------------


def accounts_by_domain(connections: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """`нормалізований домен -> (connection_key, account_id)` для GSC."""
    found: dict[str, tuple[str, str]] = {}
    priorities: dict[str, int] = {}
    for connection_key, account_id, _name in _iter_accounts(connections):
        domain = normalize_domain(account_id)
        if not domain:
            continue
        # Domain-property покриває будь-який протокол/субдомен, URL-prefix — ні.
        # Ninja може повернути обидва в довільному порядку, тож порядок у списку
        # не має вирішувати.
        priority = 1 if account_id.casefold().startswith("sc-domain:") else 0
        if domain not in found or priority > priorities[domain]:
            found[domain] = (connection_key, account_id)
            priorities[domain] = priority
    return found


def accounts_by_name(connections: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """`назва акаунта (casefold) -> (connection_key, account_id)` для GA4."""
    found: dict[str, tuple[str, str]] = {}
    for connection_key, account_id, account_name in _iter_accounts(connections):
        name = account_name.strip().casefold()
        if not name:
            continue
        found.setdefault(name, (connection_key, account_id))
        # Ninja дописує до людської назви числовий id: `Client GA4 (123456)`.
        # Матчимо стабільну назву, зберігаючи точний ID.
        found.setdefault(_TRAILING_ID_RE.sub("", name).strip(), (connection_key, account_id))
    return found


# --- побудова кандидатів ---------------------------------------------------


def _gsc_config(connection_key: str, account_id: str) -> dict[str, Any]:
    # Форма один-в-один із тим, що пише імпортер (`aiseotools_sync`), щоб
    # автопідключення й повний імпорт не розійшлися.
    return {
        "connection_key": connection_key,
        "account_id": account_id,
        "property": account_id,
        "profile": "gsc",
    }


def _gsc_candidates(
    connections: list[dict[str, Any]], *, domain: str
) -> list[IntegrationCandidate]:
    target = normalize_domain(domain)
    if not target:
        return []

    exact: IntegrationCandidate | None = None
    exact_priority = -1
    others: list[IntegrationCandidate] = []

    for connection_key, account_id, _name in _iter_accounts(connections):
        account_domain = normalize_domain(account_id)
        if not account_domain:
            continue
        config = _gsc_config(connection_key, account_id)
        if account_domain == target:
            priority = 1 if account_id.casefold().startswith("sc-domain:") else 0
            if priority > exact_priority:
                exact_priority = priority
                exact = IntegrationCandidate(
                    label=account_id,
                    detail=connection_key,
                    confidence="exact",
                    reason=(
                        "domain property covering every subdomain"
                        if priority
                        else "URL-prefix property matching the domain"
                    ),
                    score=1.0,
                    source_mode=SourceMode.api,
                    integration_config=config,
                )
            continue
        # Не збіг — але оператор має побачити, що саме з'єднання взагалі
        # містить. Порожній список змушує його гадати, чи то інструмент
        # зламався, чи то property справді немає (AGENTS §6).
        related = target in account_domain or account_domain in target
        score = _similarity(_name_key(account_domain), _name_key(target))
        others.append(
            IntegrationCandidate(
                label=account_id,
                detail=connection_key,
                confidence="weak",
                reason=(
                    f"related domain ({account_domain})"
                    if related
                    else f"available in this connection · similarity {score:.2f}"
                ),
                score=max(score, _WEAK_SCORE) if related else score,
                source_mode=SourceMode.api,
                integration_config=config,
                suggested=related,
            )
        )

    if exact is not None:
        return [exact]
    others.sort(key=lambda candidate: candidate.score, reverse=True)
    return others[:_MAX_INVENTORY]


def _ga4_candidates(
    connections: list[dict[str, Any]], *, domain: str, client_name: str
) -> list[IntegrationCandidate]:
    normalized_domain = normalize_domain(domain) or ""
    alias = GA4_ACCOUNT_ALIASES.get(normalized_domain)
    alias_key = alias.strip().casefold() if alias else None
    targets = _match_targets(domain, client_name)

    scored: list[IntegrationCandidate] = []
    for connection_key, account_id, account_name in _iter_accounts(connections):
        config = {"connection_key": connection_key, "account_id": account_id}
        label = account_name.strip() or account_id

        if alias_key and account_name.strip().casefold() == alias_key:
            score, reason, decisive = 1.0, "known account name for this domain", True
        elif _domain_of_name(account_name) == normalized_domain and normalized_domain:
            score, reason, decisive = 1.0, "account name resolves to the client domain", True
        else:
            name_key = _name_key(account_name)
            score = max((_similarity(name_key, target) for target in targets), default=0.0)
            reason, decisive = f"name similarity {score:.2f}", False

        scored.append(
            IntegrationCandidate(
                label=label,
                detail=account_id,
                confidence=_tier(score),
                reason=reason,
                score=score,
                source_mode=SourceMode.auto,
                integration_config=config,
                decisive=decisive,
            )
        )

    # Рішучі збіги — першими серед рівних за score.
    # Рішучі збіги — першими серед рівних за score.
    scored.sort(key=lambda candidate: (candidate.score, candidate.decisive), reverse=True)
    # Правдоподібні показуємо одразу, решту віддаємо як інвентар за кнопкою
    # «show all» — глухого кута не буває, але й стіни з 30 рядків теж.
    return [
        replace(candidate, suggested=candidate.score >= _WEAK_SCORE)
        for candidate in scored[:_MAX_INVENTORY]
    ]


def best_ga4_account(
    connections: list[dict[str, Any]], *, domain: str, client_name: str
) -> tuple[str, str] | None:
    """`(connection_key, account_id)` для GA4 або `None`, якщо впевненості немає.

    Точка входу для імпортера (task-07): він працює без оператора, тож мусить
    підкорятися тому самому правилу — жодних записів «навмання».
    """
    auto = _pick_auto(_ga4_candidates(connections, domain=domain, client_name=client_name))
    if auto is None:
        return None
    config = auto.integration_config
    return str(config["connection_key"]), str(config["account_id"])


def _local_candidate(
    provider: Provider, *, domain: str, client_name: str, market: str | None
) -> IntegrationCandidate:
    """Ahrefs і Semrush ідентифікуються самим доменом — шукати нічого.

    Конфіги повторюють `aiseotools_sync` дослівно.
    """
    if provider == Provider.ahrefs:
        config: dict[str, Any] = {
            "domain": domain,
            "market": market or "",
            "ahrefs_client_label": client_name,
        }
    else:
        config = {
            "domain": domain,
            "database": (market or "us").lower(),
            "profile": "semrush",
        }
    return IntegrationCandidate(
        label=domain,
        detail=client_name,
        confidence="exact",
        reason="this provider is keyed by the client domain",
        score=1.0,
        source_mode=SourceMode.auto,
        integration_config=config,
    )


def _pick_auto(candidates: list[IntegrationCandidate]) -> IntegrationCandidate | None:
    """Автозапис лише коли лідер впевнений і відірвався від наступного.

    Два однаково схожі акаунти (типова ситуація «Client - GA4» і «Client - GA4
    old») ніколи не підключаються навмання — це вибір оператора.
    """
    if not candidates:
        return None
    best = candidates[0]
    if best.confidence == "weak":
        return None
    # Пряма ознака (аліас / домен у назві) не потребує відриву: це не здогад.
    if best.decisive:
        return best
    if len(candidates) > 1 and best.score - candidates[1].score < _AUTO_MARGIN:
        return None
    return best


# --- публічна точка входу --------------------------------------------------


def discover(
    provider: Provider,
    *,
    domain: str,
    client_name: str,
    market: str | None = None,
) -> DiscoveryOutcome:
    """Знайти акаунт провайдера для клієнта.

    Ніколи не кидає через збій провайдера: недоступний Reporting Ninja — це
    штатний стан `unavailable`, а не 500.
    """
    if provider in (Provider.ahrefs, Provider.semrush):
        candidate = _local_candidate(
            provider, domain=domain, client_name=client_name, market=market
        )
        return DiscoveryOutcome(
            status="connected",
            candidates=[candidate],
            auto_apply=candidate,
            message=f"Configured from the client domain ({domain}).",
        )

    if provider == Provider.gsc:
        integration_id, label = GSC_INTEGRATION_ID, "Search Console"
    elif provider == Provider.reporting_ninja:
        integration_id, label = GA4_INTEGRATION_ID, "GA4"
    else:
        return DiscoveryOutcome(
            status="not_found",
            candidates=[],
            auto_apply=None,
            message=f"{provider.value} has no auto-connect support.",
        )

    try:
        connections = list_ninja_accounts(integration_id)
    except Exception as exc:  # noqa: BLE001 - недоступність провайдера ≠ помилка застосунку
        # Текст винятку не віддаємо: він може містити URL із ключем.
        return DiscoveryOutcome(
            status="unavailable",
            candidates=[],
            auto_apply=None,
            message=(
                f"Reporting Ninja is unavailable ({type(exc).__name__}). "
                "Check the API key, or enter the account manually."
            ),
        )

    if provider == Provider.gsc:
        candidates = _gsc_candidates(connections, domain=domain)
    else:
        candidates = _ga4_candidates(connections, domain=domain, client_name=client_name)

    if not candidates:
        return DiscoveryOutcome(
            status="not_found",
            candidates=[],
            auto_apply=None,
            message=f"No {label} account matches {domain}.",
        )

    auto = _pick_auto(candidates)
    if auto is not None:
        return DiscoveryOutcome(
            status="connected",
            candidates=candidates,
            auto_apply=auto,
            message=f"Matched {label} account {auto.label}.",
        )

    if any(candidate.score >= _STRONG_SCORE for candidate in candidates):
        return DiscoveryOutcome(
            status="needs_choice",
            candidates=candidates,
            auto_apply=None,
            message=f"Several {label} accounts could match {domain}. Pick the right one.",
        )

    # Нічого правдоподібного. Найважливіше тут — сказати, ДЕ саме шукали і
    # скільки всього видно: інакше оператор бачить у Google property, якого
    # немає в Ninja, і думає, що зламався інструмент.
    accounts = _iter_accounts(connections)
    keys = sorted({connection_key for connection_key, _id, _name in accounts})
    where = f"Connection {keys[0]}" if len(keys) == 1 else f"{len(keys)} connections"
    return DiscoveryOutcome(
        status="needs_choice",
        candidates=candidates,
        auto_apply=None,
        message=(
            f"No {label} account matches {domain}. {where} exposes {len(accounts)} "
            f"accounts, listed below — pick one, or share the property with that "
            f"account in Google and refresh the connection in Reporting Ninja."
        ),
    )
