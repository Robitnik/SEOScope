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
from typing import Literal


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
    # Голий `Client.domain`, без схеми — для полів, куди інструмент чекає
    # домен, а не URL (напр. `DOMAIN:` у AhrefsAudit.md). Той самий
    # порожній-рядок-як-None інваріант, що й у `site_url`.
    domain = "domain"


class TemplateKind(StrEnum):
    """Що таке рядок таблиці цього шаблону (task-102)."""

    # Рядок = клієнт; значення полів резолвляться з БД (`render_for_client`).
    client = "client"
    # Рядок = сторінка/стаття зі стратегії (Google Sheets чи xlsx), яку оператор
    # вказує посиланням або шляхом (`app/prompts/strategy_*.py`). Вхідні поля
    # тут — колонки таблиці стратега, а не властивості клієнта в БД.
    strategy = "strategy"


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
    # Адитивно (task-102): дефолти лишають три наявні шаблони без змін.
    kind: TemplateKind = TemplateKind.client
    # Лише для `kind=strategy`: яку роль аркуша стратегії шаблон споживає
    # (`app/contentqa/readers/tabular.py::sheet_role`).
    strategy_role: Literal["page", "blog"] | None = None


_TECHAUDIT_LAUNCHER = (
    "Read {{DOC_PATH}} and run it end to end.\n"
    "\n"
    "MODE:    tech\n"
    "CLIENT:  {{CLIENT}}\n"
    "URL:     {{URL}}\n"
)

_AHREFS_AUDIT_LAUNCHER = (
    "Read {{DOC_PATH}} and run it end to end.\n"
    "\n"
    "MODE:      ahrefs\n"
    "CLIENT:    {{CLIENT}}\n"
    "DOMAIN:    {{DOMAIN}}\n"
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
        key="ahrefs",
        name="Ahrefs Audit",
        summary=(
            "Ahrefs Site Audit, developer-ready — one sheet per issue type, every URL "
            "live-verified against the current page, not the crawl's stale snapshot."
        ),
        doc_relpath="docs/AhrefsAudit.md",
        launcher=_AHREFS_AUDIT_LAUNCHER,
        fields=(
            PromptField(key="CLIENT", label="CLIENT", source=FieldSource.client_label),
            PromptField(key="DOMAIN", label="DOMAIN", source=FieldSource.domain),
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
    # Шапка-форма для цих двох шаблонів будується з рядка стратегії на льоту
    # (`app/prompts/strategy_render.py`), тому `launcher`/`fields` порожні:
    # значення живуть у таблиці стратега, а не в БД клієнтів.
    PromptTemplate(
        key="page_framework",
        name="Page Framework",
        summary=(
            "Content framework for one page from the strategy sheet — competitors, keywords, "
            "outline, meta and (for an existing page) a verdict for every block, built into a DOCX."
        ),
        doc_relpath="docs/PageFramework.md",
        launcher="",
        fields=(),
        kind=TemplateKind.strategy,
        strategy_role="page",
    ),
    PromptTemplate(
        key="blog_framework",
        name="Blog Framework",
        summary=(
            "Content framework for one blog article from the strategy sheet — SERP research, "
            "outline, FAQ, meta and internal links to the money pages, built into a DOCX."
        ),
        doc_relpath="docs/BlogFramework.md",
        launcher="",
        fields=(),
        kind=TemplateKind.strategy,
        strategy_role="blog",
    ),
)

_BY_KEY: dict[str, PromptTemplate] = {t.key: t for t in TEMPLATES}


def get_template(key: str) -> PromptTemplate | None:
    return _BY_KEY.get(key)
