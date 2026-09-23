"""Резолвер значень полів + рендер лаунчера для конкретного клієнта (task-94).

Інваріант (PLAN §12, повторений у таск-плані §2): відсутнє значення НІКОЛИ
не підмінюється порожнім рядком. Якщо хоч одне поле шаблону не резолвилось,
`render_for_client` повертає `rendered=None` і непорожній `blocked_reason` —
кнопка `Copy` на фронті вимикається, а не копіює рядок із діркою (текст без
`URL:` запустив би краул не по тому сайту).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Client
from app.db.repositories.clients import ClientRepository
from app.prompts.registry import FieldSource, PromptTemplate
from app.schemas.enums import Provider


def _client_label(session: Session, client: Client) -> str:
    """Кодове ім'я клієнта в AISeoTools.

    Порядок джерел (дослідження task-94 §1.4): `tech_audit.config_json`,
    далі `ahrefs.config_json["ahrefs_client_label"]`, інакше `Client.name`.
    Завжди повертає значення — `Client.name` обов'язковий у моделі.
    """
    repo = ClientRepository(session)
    tech = repo.get_integration(client.id, Provider.tech_audit)
    if tech is not None:
        label = str(tech.config_json.get("client_label") or "").strip()
        if label:
            return label
    ahrefs = repo.get_integration(client.id, Provider.ahrefs)
    if ahrefs is not None:
        label = str(ahrefs.config_json.get("ahrefs_client_label") or "").strip()
        if label:
            return label
    return client.name


def _site_url(client: Client) -> str | None:
    domain = (client.domain or "").strip()
    if not domain:
        return None
    return f"https://{domain}"


def _domain(client: Client) -> str | None:
    domain = (client.domain or "").strip()
    return domain or None


def _resolve_field(source: FieldSource, session: Session, client: Client) -> str | None:
    if source is FieldSource.client_label:
        return _client_label(session, client)
    if source is FieldSource.site_url:
        return _site_url(client)
    if source is FieldSource.domain:
        return _domain(client)
    raise AssertionError(f"unhandled FieldSource: {source!r}")  # pragma: no cover


def _blocked_reason(source: FieldSource) -> str:
    if source is FieldSource.site_url or source is FieldSource.domain:
        return "client has no domain to audit"
    # pragma: no cover — every other source always resolves a value.
    return f"could not resolve {source.value}"


def render_for_client(
    template: PromptTemplate,
    client: Client,
    session: Session,
    *,
    doc_host_path: str,
) -> tuple[dict[str, str | None], str | None, str | None]:
    """Резолвить кожне поле шаблону і, якщо всі є, рендерить лаунчер.

    Повертає `(values, rendered, blocked_reason)`. `rendered` та
    `blocked_reason` завжди у протилежних станах (рівно одне з двох — не
    `None`), як задокументовано в `PromptClientRowDTO`.
    """
    values: dict[str, str | None] = {}
    blocked_reason: str | None = None
    for field in template.fields:
        value = _resolve_field(field.source, session, client)
        values[field.key] = value
        if value is None and blocked_reason is None:
            blocked_reason = _blocked_reason(field.source)

    if blocked_reason is not None:
        return values, None, blocked_reason

    rendered = template.launcher.replace("{{DOC_PATH}}", doc_host_path)
    for key, value in values.items():
        assert value is not None  # гарантовано відсутністю blocked_reason вище
        rendered = rendered.replace("{{" + key + "}}", value)
    return values, rendered, None
