"""Нормалізована модель контенту: рядок стратегії й фреймворк-документ зводяться
до одного `ContentItem`, тому кожне правило в rules.py пишеться один раз.

`None` = поля в джерелі немає; `""` = поле є і воно порожнє. Два стани ніколи
не підмінюють одне одного (PLAN §12) — саме ця різниця відрізняє «блог ще не
має титулу» від «колонки Title у файлі взагалі немає», і правила нижче на неї
спираються (meta-title-missing спрацьовує лише на `""`, не на `None`).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ContentItem:
    ref: str
    page_name: str | None = None
    url: str | None = None
    intent: str | None = None
    title: str | None = None
    description: str | None = None
    h1: str | None = None
    headings: tuple[tuple[str, str], ...] = ()
    keywords: tuple[str, ...] = ()
    internal_links: tuple[str, ...] = ()
    external_links: tuple[str, ...] = ()
    body: str | None = None
    raw: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ContentDoc:
    source: str
    doc_type: str  # "strategy_xlsx" | "framework_docx"
    items: tuple[ContentItem, ...]
    skipped_sheets: tuple[str, ...] = ()
