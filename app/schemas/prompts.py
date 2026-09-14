"""DTO для тула Prompt Launcher (task-94).

Новий файл у зоні `app/schemas/` — адитивно, як і `app/schemas/tools.py`
(task-27). Тул нічого не мутує: усі три ролі бачать однакові дані
(`app/core/auth/policy.py::required_role` повертає `viewer` для будь-якого
safe-методу поза allowlist'ами — тут гейти не потрібні).

Як і всюди в проєкті, `rendered` приходить УЖЕ готовим текстом: фронт
копіює рядок один-в-один, ніколи не склеює його з полів сам (PLAN §12 —
бекенд рахує семантику, фронт лише рендерить).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class PromptFieldDTO(BaseModel):
    """Одне ВХІДНЕ поле шаблону (наприклад CLIENT або URL).

    Свідомо не всі `{{…}}` файлу промта — більшість плейсхолдерів у
    документах AISeoTools це шаблон ФІНАЛЬНОГО звіту агента (`{{XLSX_PATH}}`,
    `{{DDMMYYYY}}`…), а не форма, яку оператор заповнює перед запуском.
    Список полів оголошується явно в реєстрі (`app/prompts/registry.py`),
    файл промта на це ніколи не парситься.
    """

    key: str
    label: str


class PromptTemplateDTO(BaseModel):
    key: str
    name: str
    summary: str
    doc_relpath: str
    doc_host_path: str
    doc_available: bool
    doc_updated_at: datetime | None = None
    doc_bytes: int | None = None
    fields: list[PromptFieldDTO]


class PromptTemplatesDTO(BaseModel):
    templates: list[PromptTemplateDTO]


class PromptClientRowDTO(BaseModel):
    client_id: str
    client_name: str
    slug: str
    status: str
    enabled: bool
    is_dev: bool
    # `True` ⇔ клієнт у `ClientRepository.list_collectable()` (видимий, не
    # на паузі) — той самий набір, у який розгортається масовий збір.
    # Фронт групує за цим прапорцем сам НЕ дериваючи його (PLAN §12): рядки
    # з `collectable=False` (пауза/приховано) ідуть у згорнуту секцію.
    collectable: bool
    # Значення полів шаблону для цього клієнта — ключ = `PromptFieldDTO.key`.
    # `None` = поле не вдалось резолвити (PLAN §12: відсутнє ≠ порожній рядок).
    values: dict[str, str | None]
    # Готовий текст лаунчера. `None` тоді й лише тоді, коли `blocked_reason`
    # заповнений — рядок без домену не має давати промт із діркою.
    rendered: str | None
    blocked_reason: str | None = None


class PromptClientsDTO(BaseModel):
    template_key: str
    generated_at: datetime
    rows: list[PromptClientRowDTO]


class PromptSourceDTO(BaseModel):
    text: str
    truncated: bool
