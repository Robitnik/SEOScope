"""Юніти матчера автопідключення (task-33).

Матчер — єдине місце, де рішення «до якого акаунта підключити клієнта»
ухвалюється автоматично, тож саме тут ціна помилки найвища: підключення до
чужого GA4-property означає, що клієнт мовчки дивитиметься на чужі дані.
Звідси наголос тестів на тому, коли автозапис **не** відбувається.
"""

from __future__ import annotations

import pytest

from app.integrations import discovery
from app.integrations.discovery import (
    GA4_ACCOUNT_ALIASES,
    IntegrationCandidate,
    discover,
)
from app.schemas.enums import Provider, SourceMode


def _connections(*accounts: tuple[str, str], key: str = "seo@example.com"):
    return [
        {
            "connection_key": key,
            "accounts": [
                {"account_id": account_id, "account_name": account_name}
                for account_id, account_name in accounts
            ],
        }
    ]


@pytest.fixture(autouse=True)
def _clear_cache():
    discovery.reset_cache()
    yield
    discovery.reset_cache()


def _patch_ninja(monkeypatch, payload, *, integration_id_seen=None):
    def _fake(integration_id: str, *, use_cache: bool = True):
        if integration_id_seen is not None:
            integration_id_seen.append(integration_id)
        return payload

    monkeypatch.setattr(discovery, "list_ninja_accounts", _fake)


# --- GSC -------------------------------------------------------------------


def test_gsc_prefers_domain_property_over_url_prefix(monkeypatch):
    """`sc-domain:` покриває всі субдомени, URL-prefix — ні.

    Ninja повертає обидва в довільному порядку, тому порядок у списку не має
    вирішувати: тут URL-prefix іде першим і все одно програє.
    """
    _patch_ninja(
        monkeypatch,
        _connections(
            ("https://www.laxton.se/", "https://www.laxton.se/"),
            ("sc-domain:laxton.se", "sc-domain:laxton.se"),
        ),
    )
    outcome = discover(Provider.gsc, domain="laxton.se", client_name="LAXTON")

    assert outcome.status == "connected"
    assert outcome.auto_apply is not None
    assert outcome.auto_apply.integration_config["account_id"] == "sc-domain:laxton.se"
    assert outcome.auto_apply.source_mode == SourceMode.api


def test_gsc_config_matches_the_importer_shape(monkeypatch):
    """Автопідключення й повний імпорт мусять писати однаковий конфіг."""
    _patch_ninja(monkeypatch, _connections(("sc-domain:laxton.se", "sc-domain:laxton.se")))
    outcome = discover(Provider.gsc, domain="laxton.se", client_name="LAXTON")

    assert outcome.auto_apply is not None
    assert outcome.auto_apply.integration_config == {
        "connection_key": "seo@example.com",
        "account_id": "sc-domain:laxton.se",
        "property": "sc-domain:laxton.se",
        "profile": "gsc",
    }


def test_gsc_unrelated_domain_is_never_auto_applied(monkeypatch):
    _patch_ninja(monkeypatch, _connections(("sc-domain:someoneelse.com", "someoneelse.com")))
    outcome = discover(Provider.gsc, domain="laxton.se", client_name="LAXTON")

    assert outcome.auto_apply is None
    assert outcome.status == "needs_choice"


def test_gsc_miss_names_the_connection_and_shows_its_inventory(monkeypatch):
    """Живий кейс: laxton.se є в Google Search Console, але не в Ninja.

    Порожнє «not found» змушувало оператора думати, що зламався інструмент.
    Звіт має назвати з'єднання, показати скільки property в ньому видно, і
    підказати справжню дію — поділитися property й оновити з'єднання.
    """
    _patch_ninja(
        monkeypatch,
        _connections(
            ("sc-domain:pjclarkes.com", "pjclarkes.com"),
            ("sc-domain:nychearing.com", "nychearing.com"),
            key="seo@theyovth.com",
        ),
    )
    outcome = discover(Provider.gsc, domain="laxton.se", client_name="LAXTON")

    assert outcome.status == "needs_choice"
    assert outcome.auto_apply is None
    assert "seo@theyovth.com" in outcome.message
    assert "2 accounts" in outcome.message
    assert "refresh the connection" in outcome.message
    # Інвентар справді показаний, а не прихований.
    assert {c.label for c in outcome.candidates} == {
        "sc-domain:pjclarkes.com",
        "sc-domain:nychearing.com",
    }
    assert all(c.confidence == "weak" for c in outcome.candidates)
    # ...але позначений як «не пропозиція»: фронт тримає його за «show all»,
    # інакше замість відповіді оператор отримує стіну з 22 чужих доменів.
    assert all(c.suggested is False for c in outcome.candidates)


def test_plausible_candidates_are_suggested_but_the_rest_is_inventory(monkeypatch):
    """Розділення на «показати одразу» і «показати за кнопкою»."""
    _patch_ninja(
        monkeypatch,
        _connections(
            ("properties/1", "Laxton"),
            ("properties/2", "Laxton Nordic"),
            ("properties/3", "Completely Different Co"),
        ),
    )
    monkeypatch.setattr(discovery, "GA4_ACCOUNT_ALIASES", {})

    outcome = discover(Provider.reporting_ninja, domain="laxton.se", client_name="LAXTON")

    by_label = {c.label: c for c in outcome.candidates}
    assert by_label["Laxton"].suggested is True
    assert by_label["Laxton Nordic"].suggested is True
    assert by_label["Completely Different Co"].suggested is False


def test_gsc_related_subdomain_stays_suggested(monkeypatch):
    """Субдомен — правдоподібний варіант, його ховати за кнопкою не можна."""
    _patch_ninja(
        monkeypatch,
        _connections(
            ("sc-domain:blog.laxton.se", "blog.laxton.se"),
            ("sc-domain:someoneelse.com", "someoneelse.com"),
        ),
    )
    outcome = discover(Provider.gsc, domain="laxton.se", client_name="LAXTON")

    by_label = {c.label: c for c in outcome.candidates}
    assert by_label["sc-domain:blog.laxton.se"].suggested is True
    assert by_label["sc-domain:someoneelse.com"].suggested is False


def test_gsc_subdomain_is_offered_as_a_weak_candidate_not_auto_applied(monkeypatch):
    """Субдомен — не той самий ресурс, але краще показати, ніж глухий кут."""
    _patch_ninja(monkeypatch, _connections(("sc-domain:blog.laxton.se", "blog.laxton.se")))
    outcome = discover(Provider.gsc, domain="laxton.se", client_name="LAXTON")

    assert outcome.status == "needs_choice"
    assert outcome.auto_apply is None
    assert [c.confidence for c in outcome.candidates] == ["weak"]


# --- GA4 -------------------------------------------------------------------


@pytest.mark.parametrize(("domain", "account_name"), sorted(GA4_ACCOUNT_ALIASES.items()))
def test_aliases_are_reproduced_by_fuzzy_matching(monkeypatch, domain, account_name):
    """Евристика має покривати всю історичну хардкод-таблицю.

    Це і є доказ, що таблиця з 11 доменів більше не єдиний шлях: якщо матчер
    виводить кожен її запис сам, то й новий клієнт (якого в таблиці ніколи не
    буде) підключиться без ручної правки коду.

    Аліас навмисно вимкнено — інакше тест перевіряв би саму таблицю.
    """
    monkeypatch.setattr(discovery, "GA4_ACCOUNT_ALIASES", {})
    _patch_ninja(monkeypatch, _connections(("properties/123", account_name)))

    outcome = discover(Provider.reporting_ninja, domain=domain, client_name="IRRELEVANT")

    assert outcome.status == "connected", f"{domain} -> {account_name}"
    assert outcome.auto_apply is not None
    assert outcome.auto_apply.integration_config["account_id"] == "properties/123"


def test_ga4_alias_wins_when_the_name_has_nothing_to_do_with_the_domain(monkeypatch):
    _patch_ninja(monkeypatch, _connections(("properties/900", "Legacy Brand Holdings")))
    monkeypatch.setattr(
        discovery, "GA4_ACCOUNT_ALIASES", {"example.com": "Legacy Brand Holdings"}
    )

    outcome = discover(Provider.reporting_ninja, domain="example.com", client_name="EX")

    assert outcome.status == "connected"
    assert outcome.auto_apply is not None
    assert outcome.auto_apply.confidence == "exact"
    assert outcome.auto_apply.integration_config == {
        "connection_key": "seo@example.com",
        "account_id": "properties/900",
    }


def test_ga4_alias_beats_an_equally_similar_neighbour(monkeypatch):
    """Живий кейс: у Ninja є і «19 Cleveland», і «19 Cleveland - GA4».

    Обидві назви однаково схожі на домен, тож за самим score вийшла б нічия і
    оператора питали б щоразу. Аліас — задекларована відповідь, а не здогад,
    тому він застосовується попри нічию.
    """
    _patch_ninja(
        monkeypatch,
        _connections(
            ("properties/279537177", "19 Cleveland"),
            ("properties/279498125", "19 Cleveland - GA4"),
        ),
    )
    outcome = discover(Provider.reporting_ninja, domain="19cleveland.com", client_name="19CLV")

    assert outcome.status == "connected"
    assert outcome.auto_apply is not None
    assert outcome.auto_apply.integration_config["account_id"] == "properties/279498125"


def test_ga4_url_shaped_name_is_decisive(monkeypatch):
    """`http://www.nysapphire.com - GA4` резолвиться в домен — це не здогад."""
    _patch_ninja(
        monkeypatch,
        _connections(
            ("properties/1", "NY Sapphire"),
            ("properties/428527008", "http://www.nysapphire.com - GA4"),
        ),
    )
    monkeypatch.setattr(discovery, "GA4_ACCOUNT_ALIASES", {})

    outcome = discover(Provider.reporting_ninja, domain="nysapphire.com", client_name="SAPHNY")

    assert outcome.status == "connected"
    assert outcome.auto_apply is not None
    assert outcome.auto_apply.integration_config["account_id"] == "properties/428527008"


def test_ga4_strips_the_numeric_suffix_ninja_appends(monkeypatch):
    _patch_ninja(monkeypatch, _connections(("properties/555", "Laxton (428527008)")))
    outcome = discover(Provider.reporting_ninja, domain="laxton.se", client_name="LAXTON")

    assert outcome.status == "connected"
    assert outcome.auto_apply is not None
    assert outcome.auto_apply.integration_config["account_id"] == "properties/555"


def test_ga4_two_equally_plausible_accounts_are_never_auto_applied(monkeypatch):
    """Найважливіший тест модуля.

    Дублікати property для одного сайту — типова ситуація. Підключити «якийсь»
    із них означає тихо показувати клієнту дані не з того джерела, тому вибір
    лишається за оператором.
    """
    _patch_ninja(
        monkeypatch,
        _connections(
            ("properties/1", "Laxton - GA4"),
            ("properties/2", "Laxton - GA4"),
        ),
    )
    outcome = discover(Provider.reporting_ninja, domain="laxton.se", client_name="LAXTON")

    assert outcome.status == "needs_choice"
    assert outcome.auto_apply is None
    assert {c.detail for c in outcome.candidates} == {"properties/1", "properties/2"}


def test_ga4_without_any_plausible_match_still_offers_the_full_list(monkeypatch):
    """Глухого кута не буває: оператор має що обрати навіть при нульовій схожості."""
    _patch_ninja(
        monkeypatch,
        _connections(
            ("properties/1", "Completely Different Co"),
            ("properties/2", "Another Unrelated Brand"),
        ),
    )
    outcome = discover(Provider.reporting_ninja, domain="laxton.se", client_name="LAXTON")

    assert outcome.status == "needs_choice"
    assert outcome.auto_apply is None
    assert len(outcome.candidates) == 2
    assert all(c.confidence == "weak" for c in outcome.candidates)


def test_ga4_confident_match_wins_over_a_distant_runner_up(monkeypatch):
    _patch_ninja(
        monkeypatch,
        _connections(
            ("properties/1", "Completely Different Co"),
            ("properties/2", "Laxton"),
        ),
    )
    outcome = discover(Provider.reporting_ninja, domain="laxton.se", client_name="LAXTON")

    assert outcome.status == "connected"
    assert outcome.auto_apply is not None
    assert outcome.auto_apply.integration_config["account_id"] == "properties/2"


# --- провайдери без віддаленого пошуку -------------------------------------


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        (
            Provider.ahrefs,
            {"domain": "laxton.se", "market": "SE", "ahrefs_client_label": "LAXTON"},
        ),
        (
            Provider.semrush,
            {"domain": "laxton.se", "database": "se", "profile": "semrush"},
        ),
    ],
)
def test_domain_keyed_providers_need_no_lookup(monkeypatch, provider, expected):
    """Ahrefs/Semrush ідентифікуються доменом — жодного мережевого виклику."""

    def _explode(*_args, **_kwargs):
        raise AssertionError("must not call Reporting Ninja for a domain-keyed provider")

    monkeypatch.setattr(discovery, "list_ninja_accounts", _explode)

    outcome = discover(provider, domain="laxton.se", client_name="LAXTON", market="SE")

    assert outcome.status == "connected"
    assert outcome.auto_apply is not None
    assert outcome.auto_apply.integration_config == expected


def test_semrush_database_falls_back_to_us_without_a_market():
    outcome = discover(Provider.semrush, domain="x.com", client_name="X", market=None)

    assert outcome.auto_apply is not None
    assert outcome.auto_apply.integration_config["database"] == "us"


# --- деградація ------------------------------------------------------------


def test_provider_failure_never_leaks_the_exception_text(monkeypatch):
    """У повідомленні лише клас винятку: текст може містити URL із ключем."""
    secret = "https://api.reportingninja.com/v1?api_key=SUPERSECRET"

    def _boom(*_args, **_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(discovery, "list_ninja_accounts", _boom)

    outcome = discover(Provider.gsc, domain="laxton.se", client_name="LAXTON")

    assert outcome.status == "unavailable"
    assert outcome.auto_apply is None
    assert "SUPERSECRET" not in outcome.message
    assert secret not in outcome.message
    assert "RuntimeError" in outcome.message


def test_non_metric_provider_is_not_connectable(monkeypatch):
    outcome = discover(Provider.tech_audit, domain="laxton.se", client_name="LAXTON")

    assert outcome.status == "not_found"
    assert outcome.auto_apply is None


# --- кеш -------------------------------------------------------------------


def test_connections_are_cached_between_clicks(monkeypatch):
    """Ліміт Ninja — 300 запитів/хв, а оператор клікає рядки поспіль."""
    calls: list[str] = []

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def list_connections(self, integration_id: str):
            calls.append(integration_id)
            return _connections(("sc-domain:laxton.se", "laxton.se"))

    from seo_audit.clientreport import api_client

    monkeypatch.setattr(api_client, "ReportingNinjaClient", _FakeClient)

    discovery.list_ninja_accounts("google_search_console")
    discovery.list_ninja_accounts("google_search_console")

    assert calls == ["google_search_console"]


def test_candidate_carries_a_ready_to_persist_config():
    """DTO віддає конфіг браузеру — там не має бути нічого, крім ідентифікаторів."""
    candidate = IntegrationCandidate(
        label="x",
        detail="y",
        confidence="exact",
        reason="r",
        score=1.0,
        source_mode=SourceMode.api,
        integration_config={"connection_key": "seo@example.com", "account_id": "a"},
    )
    forbidden = {"api_key", "password", "token", "secret", "cookie"}

    assert not forbidden & set(candidate.integration_config)
