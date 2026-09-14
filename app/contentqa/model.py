"""Нормалізована модель контенту: рядок стратегії й фреймворк-документ зводяться
до одного `ContentItem`, тому кожне правило в rules.py пишеться один раз.

`None` = поля в джерелі немає; `""` = поле є і воно порожнє. Два стани ніколи
не підмінюють одне одного (PLAN §12) — саме ця різниця відрізняє «блог ще не
має титулу» від «колонки Title у файлі взагалі немає», і правила нижче на неї
спираються (meta-title-missing спрацьовує лише на `""`, не на `None`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


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
    # Аркуш-джерело (XLSX) — `None` для DOCX (один документ = одна група).
    # Адитивне поле (task-87 §1.3): скоуп крос-сторінкових перевірок
    # (check_cross_page/check_meta_duplicates) групує за ним, коли
    # `Profile.cross_sheet` вимкнено, замість парсити `ref` рядком.
    sheet: str | None = None


# Стан аркуша в результаті прогону (task-87 §1.3) — три різні речі, які
# `skipped_sheets` (лише "немає title/description колонок") не розрізняв:
# оператор або перевірив аркуш, або свідомо його не обрав, або в ньому
# нема на чому перевіряти. `not_selected` — НЕ finding (це не дефект файлу),
# а рядок у блоці Scope звіту.
SheetState = Literal["checked", "not_selected", "no_recognized_columns"]

# task-97: класифікація аркуша за тим, ЩО він описує — сторінки сайту,
# блог-пости, довідкова структура/фічі, чи щось нерозпізнане. Не про
# розпізнані колонки (те вже каже `SheetState`/`load_bearing`) — навіть
# аркуш із title/description може описувати блог, не сторінку. Рушить
# дефолтний вибір форми (`suggested_sheets` в `readers/tabular.py`) і
# групування кандидатів-фреймворків у UI: тул задуманий для перевірки
# **сторінок**, блоговий аркуш/фреймворк ніколи не мав входити в дефолт
# (заміряно живо — жодного `blog_only`-прогону за всю історію портфеля).
SheetRole = Literal["page", "blog", "structure", "other"]


@dataclass(frozen=True)
class SheetInfo:
    name: str
    state: SheetState
    item_count: int
    role: SheetRole = "other"


@dataclass(frozen=True)
class ContentDoc:
    source: str
    doc_type: str  # "strategy_xlsx" | "framework_docx"
    items: tuple[ContentItem, ...]
    skipped_sheets: tuple[str, ...] = ()
    # Стан усіх аркушів workbook (не лише обраних) — `()` для DOCX і для
    # звітів, де вибір аркуша не застосовувався (усе одно "checked").
    sheet_states: tuple[SheetInfo, ...] = ()
