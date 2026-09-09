"""Заголовок -> проза-до-наступного-заголовка. Спільний алгоритм для двох
джерел, які chunk'ують по-своєму (task-90 §4.4): `readers/gdoc.py`
(параграфи Docs API, заголовок = `<h*>`-тег чи стиль) і `contentqa/site.py`
(DOM живої сторінки, заголовок = тег `<h1>`-`<h4>`). Обидва зводяться до
одного потоку `(рівень заголовка | None, текст)`, тому алгоритм групування
пишеться один раз.

Це і є вхід лейну J (Copy ↔ живий сайт, task-90 §3): порівняння йде
секція-в-секцію, зіставлену за заголовком, а не diff усієї сторінки —
навігація/футер/cookie-банер дали б шум, що ховає справжній дрейф.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ContentSection:
    level: str  # "H1".."H4"
    heading: str
    text: str  # проза між цим заголовком і наступним (будь-якого рівня)


def chunk_into_sections(items: Iterable[tuple[str | None, str]]) -> tuple[ContentSection, ...]:
    """`items` — `(рівень, текст)` у порядку документа: `рівень` заданий,
    коли `текст` — сам заголовок; `рівень=None`, коли `текст` — абзац прози,
    що належить поточній (останній побаченій) секції.

    Проза до першого заголовка (якщо є) відкидається — прив'язати її нема
    до чого, і жодна секція Copy/сайту в цьому проєкті не починається без
    заголовка (обидва джерела завжди відкриваються H1)."""
    sections: list[ContentSection] = []
    current_level: str | None = None
    current_heading: str | None = None
    current_body: list[str] = []

    def _flush() -> None:
        if current_heading is not None:
            sections.append(ContentSection(
                level=current_level or "H1", heading=current_heading,
                text="\n\n".join(current_body),
            ))

    for level, text in items:
        if level is not None:
            _flush()
            current_level, current_heading, current_body = level, text, []
        elif current_heading is not None and text:
            current_body.append(text)
    _flush()
    return tuple(sections)
