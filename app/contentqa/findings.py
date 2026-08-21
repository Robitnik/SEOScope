"""Finding — єдиний тип виводу для кожного правила content-qa."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Severity = Literal["error", "warn", "info"]

VALUE_EXCERPT_LIMIT = 300


def excerpt(text: str | None, limit: int = VALUE_EXCERPT_LIMIT) -> str | None:
    """Обрізає значення поля для показу у finding — без цього довге тіло
    DOCX-секції роздувало б і UI, і `findings_json` у БД."""
    if not text:
        return None
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


@dataclass(frozen=True)
class Finding:
    check: str
    severity: Severity
    ref: str
    detail: str
    # Фактичний текст поля, що спричинило finding (title/description/heading/…).
    # `None`, коли значення немає (наприклад, `meta-title-missing`) або
    # показувати нічого — не порожній рядок замість відсутності (PLAN §12).
    value: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "check": self.check,
            "severity": self.severity,
            "ref": self.ref,
            "detail": self.detail,
            "value": self.value,
        }
