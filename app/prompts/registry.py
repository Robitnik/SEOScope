"""Реєстр шаблонів промтів (task-94, дослідження — доданий план §1).

Реєстр у коді, той самий підхід, що в `app/api/tools.py::_TOOLS` (task-27):
набір шаблонів — частина застосунку, тож додавання наступного (`AhrefsAudit`,
`GSC_Audit`, ...) має бути одним записом тут, а не переверсткою тула.

**Чому лише два поля в `techaudit`, а не всі 12 `{{…}}` з `docs/TechAudit.md`:**
дослідження (2026-09-14) показало, що з дванадцяти плейсхолдерів файлу
ВХІДНІ, які оператор заповнює перед запуском, лише `{{CLIENT}}` і `{{URL}}` —
решта (`{{XLSX_PATH}}`, `{{PEAK}}`, `{{TABS}}`, `{{DDMMYYYY}}`, ...) належать
шаблону ФІНАЛЬНОГО звіту, який пише сам агент. Автоматичний парсинг файлу на
предмет `{{…}}` дав би форму з десятком порожніх полів, які ніхто не
заповнює, тому поля оголошуються тут явно.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FieldSource(StrEnum):
    """Звідки резолвиться значення поля для конкретного клієнта (app/prompts/render.py)."""

    # Кодове ім'я клієнта в AISeoTools: `client_integrations.config_json`
    # (`tech_audit`/`blogs` → "client_label", `ahrefs` → "ahrefs_client_label"),
    # інакше — `Client.name`. Значення є завжди (`name` обов'язковий).
    client_label = "client_label"
    # `https://{Client.domain}`. `Client.domain` — обов'язкове поле моделі,
    # але порожній рядок теоретично можливий (легасі-рядок) — тоді поле
    # резолвиться в `None`, а не в биту URL з порожнім хостом.
    site_url = "site_url"


@dataclass(frozen=True)
class PromptField:
    key: str  # "CLIENT" — ім'я плейсхолдера в лаунчері, без дужок
    label: str  # UI-колонка (англійською, як увесь UI-копірайт)
    source: FieldSource


@dataclass(frozen=True)
class PromptTemplate:
    key: str  # "techaudit" — у REST-шляху й у ключі localStorage
    name: str  # "TechAudit" — заголовок у хабі й у селекторі
    summary: str
    doc_relpath: str  # відносно кореня AISeoTools, напр. "docs/TechAudit.md"
    launcher: str  # текст із `{{DOC_PATH}}` + полями з `fields`
    fields: tuple[PromptField, ...]


_TECHAUDIT_LAUNCHER = (
    "Read {{DOC_PATH}} and run it end to end.\n"
    "\n"
    "MODE:    tech\n"
    "CLIENT:  {{CLIENT}}\n"
    "URL:     {{URL}}\n"
)

# SCOPE зафіксовано на "full" (не поле форми): це не властивість клієнта,
# а вибір, який тайп і так робить CLI-дефолтом (`seo-audit gsc-report`
# без `--scope` == full). Оператору, який хоче вужчий звіт (errors/traffic),
# лишається відредагувати рядок SCOPE у скопійованому лаунчер-тексті вручну —
# окреме поле форми під це поза обсягом (Follow-ups task-94 §10).
_GSC_AUDIT_LAUNCHER = (
    "Read {{DOC_PATH}} and run it end to end.\n"
    "\n"
    "MODE:      gsc\n"
    "SCOPE:     full\n"
    "CLIENT:    {{CLIENT}}\n"
    "PROPERTY:  {{TARGET_URL}}\n"
)

# Порядок тут = порядок пунктів у селекторі шаблону.
TEMPLATES: tuple[PromptTemplate, ...] = (
    PromptTemplate(
        key="techaudit",
        name="TechAudit",
        summary=(
            "Standalone technical SEO audit — crawls the whole site, detects bugs on the "
            "live DOM, screenshots every page and builds a multi-tab, developer-ready report."
        ),
        doc_relpath="docs/TechAudit.md",
        launcher=_TECHAUDIT_LAUNCHER,
        fields=(
            PromptField(key="CLIENT", label="CLIENT", source=FieldSource.client_label),
            PromptField(key="URL", label="URL", source=FieldSource.site_url),
        ),
    ),
    PromptTemplate(
        key="gsc",
        name="GSC Audit",
        summary=(
            "Google Search Console audit — indexing errors, traffic decay, CTR-gap rewrites "
            "and opportunities, every reported URL live-verified before it reaches the report."
        ),
        doc_relpath="docs/GSC_Audit.md",
        launcher=_GSC_AUDIT_LAUNCHER,
        fields=(
            PromptField(key="CLIENT", label="CLIENT", source=FieldSource.client_label),
            PromptField(key="TARGET_URL", label="PROPERTY", source=FieldSource.site_url),
        ),
    ),
)

_BY_KEY: dict[str, PromptTemplate] = {t.key: t for t in TEMPLATES}


def get_template(key: str) -> PromptTemplate | None:
    return _BY_KEY.get(key)
