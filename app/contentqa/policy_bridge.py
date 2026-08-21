"""Lazy-seam над AISeoTools' `seo_audit.policy` — той самий патерн import-у-
момент-виклику, що вже в app.integrations.{ahrefs,blogs,tech_audit}: імпорт
цього модуля ніколи не чіпає сусідній чекаут, тож content-qa і власні правила
(rules.py) працюють, навіть коли AISeoTools не примонтовано. Лише функції, яким
policy.py дійсно потрібен, кидають PolicyUnavailable — один раз, у engine.py,
а не на кожному ContentItem.
"""

from __future__ import annotations

from typing import Protocol


class PolicyUnavailable(RuntimeError):
    """seo_audit.policy не імпортується — AISeoTools не примонтовано."""


class _Policy(Protocol):
    TITLE_MAX: int
    DESC_MAX: int
    DESC_AUTHORED_MAX: int
    TITLE_MAX_PIXELS: int
    TITLE_SIMILARITY_MAX: float
    DESCRIPTION_SIMILARITY_MAX: float
    DUPLICATE_PREFIX_WORDS: int

    def slop_phrases(self, text: str) -> list[str]: ...
    def filler_terms(self, text: str) -> list[str]: ...
    def banned_opener(self, text: str) -> str | None: ...
    def em_dash_hits(self, text: str) -> list[str]: ...
    def process_leak_hits(self, text: str) -> list[str]: ...
    def tool_failure_phrases(self, text: str) -> list[str]: ...
    def keyword_in_title(self, title: str, keyword: str) -> bool: ...
    def meta_length_hits(
        self, title: str, description: str
    ) -> list[tuple[str, int, int]]: ...


def _policy() -> _Policy:
    try:
        from seo_audit import policy
    except ImportError as exc:
        raise PolicyUnavailable(
            "seo_audit.policy is not importable: AISeoTools is not bind-mounted "
            "in this container (compose.yaml AISEOTOOLS_PATH). Meta-length, "
            "slop-phrase, filler-term, banned-opener, em-dash and process-leak "
            "checks are skipped for this run; every other content-qa lane still runs."
        ) from exc
    return policy  # type: ignore[no-any-return]


def probe() -> None:
    """Кидає PolicyUnavailable, якщо policy.py недоступний. Викликати один раз
    на весь прогін (engine.py), а не на кожен ContentItem."""
    _policy()


def title_max() -> int:
    return _policy().TITLE_MAX


def desc_max() -> int:
    return _policy().DESC_MAX


def desc_authored_max() -> int:
    return _policy().DESC_AUTHORED_MAX


def title_max_pixels() -> int:
    return _policy().TITLE_MAX_PIXELS


def title_similarity_max() -> float:
    return _policy().TITLE_SIMILARITY_MAX


def description_similarity_max() -> float:
    return _policy().DESCRIPTION_SIMILARITY_MAX


def duplicate_prefix_words() -> int:
    return _policy().DUPLICATE_PREFIX_WORDS


def slop_phrases(text: str) -> list[str]:
    return _policy().slop_phrases(text)


def filler_terms(text: str) -> list[str]:
    return _policy().filler_terms(text)


def banned_opener(text: str) -> str | None:
    return _policy().banned_opener(text)


def em_dash_hits(text: str) -> list[str]:
    return _policy().em_dash_hits(text)


def process_leak_hits(text: str) -> list[str]:
    return _policy().process_leak_hits(text)


def tool_failure_phrases(text: str) -> list[str]:
    return _policy().tool_failure_phrases(text)


def keyword_in_title(title: str, keyword: str) -> bool:
    return _policy().keyword_in_title(title, keyword)


def meta_length_hits(title: str = "", description: str = "") -> list[tuple[str, int, int]]:
    return _policy().meta_length_hits(title=title, description=description)


def _norm_words(text: str) -> set[str]:
    import re

    return set(re.findall(r"[^\W\d_]+", (text or "").lower()))


def text_similarity(a: str, b: str) -> float:
    """Той самий overlap-принцип, що `policy._overlap` (приватний, тому не
    імпортується напряму): перетин слів / менша з двох множин."""
    wa, wb = _norm_words(a), _norm_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))
