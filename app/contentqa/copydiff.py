"""Сегментація + нормалізація + вирівнювання для посегментної звірки Copy ↔
живий сайт (task-92) — три колонки на URL: «речення у Copy» / «речення на
сайті» / «чи сходиться».

Чиста функція над `DocBlock` (`readers/gdoc.py:copy_tab_blocks`) і
`SiteBlock` (`site.py`), той самий принцип, що `matrix.py`: жодного
HTTP/Docs API/БД тут — усе введення вже прочитане викликачем
(`package.py`).

**Головна знахідка дослідження (task-92 §1.5), яка визначає весь дизайн:
коефіцієнт схожості не може бути вердиктом.** Живий приклад (EFFYS,
чотири сторінки): документ каже `"open daily from 8 AM to 5 PM"`, сайт —
`"from 7 AM to 5 PM"`. `SequenceMatcher.ratio() == 0.99` — типографічно
майже ідентичні рядки, семантично різна година роботи. Будь-який поріг,
що прощає типографіку (0.95-0.98), ховає саме ту помилку, заради якої цей
модуль існує. Тому: `normalize()` знімає типографіку (лапки/тире/nbsp/
регістр), а вердикт `match` — **точна рівність** нормалізованих рядків.
`SequenceMatcher` використовується лише для ЗІСТАВЛЕННЯ речення документа
з реченням сайту в пару (`align_segments`), ніколи — для вирішення, чи
вони однакові.

Директиви верстки (`DocBlock(kind="directive")` — `[IMAGE SECTION: ...]`,
`Breadcrumbs: ...`, рядок з підкреслень) виключені зі знаменника покриття,
але не відкидаються мовчки — повертаються окремими рядками `state=
"excluded"`, щоб оператор бачив, що саме не порівнювалось і чому.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Literal

from app.contentqa.readers.gdoc import DocBlock
from app.contentqa.site import SiteBlock

RowState = Literal["match", "changed", "missing", "extra", "excluded"]
SegmentKind = Literal["heading", "sentence", "item", "directive"]
Confidence = Literal["strong", "weak"]

# Опозиція до типографічної схожості (task-92 §1.5/§1.9): вирівнювання
# двох сегментів у пару відбувається на РІЗНИХ порогах залежно від того,
# чи вкладка документа взагалі тією ж мовою, що жива сторінка
# (`LivePage.lang`). Слабкий рівень — головне джерело хибних "changed" на
# міжмовних парах (LAXTON: `"Searches for EVP in real time"` ↔
# `"Letar EVP i realtid"` дає 0.67 лише через спільний токен `EVP`) — тому
# вимкнений, коли мови розходяться (§2.4).
_STRONG_RATIO = 0.75
_WEAK_RATIO = 0.55


@dataclass(frozen=True)
class Segment:
    """Один порівнюваний шматок — заголовок, речення, пункт списку чи
    директива (виключена з порівняння, але не з виводу). `section` —
    заголовок секції, під яким цей сегмент лежить (для групування у
    фронті/Excel), `None` до першого заголовка документа."""

    kind: SegmentKind
    level: str | None
    section: str | None
    text: str


@dataclass(frozen=True)
class DiffPart:
    """Один опкод послівного diff'у doc_text ↔ site_text. `tag="equal"` —
    спільні слова; `tag="changed"` — розійшлися (одна зі сторін може бути
    порожньою, коли слово лише додане чи лише прибране)."""

    tag: Literal["equal", "changed"]
    doc_text: str
    site_text: str


@dataclass(frozen=True)
class CopyRow:
    state: RowState
    kind: SegmentKind
    section: str | None
    doc_text: str | None
    site_text: str | None
    # `None`, коли порівняння не мало сенсу (missing/extra/excluded) — той
    # самий інваріант PLAN §12 (`None` ≠ значення), піднятий на рівень
    # рядка таблиці.
    ratio: float | None = None
    confidence: Confidence | None = None
    diff_parts: tuple[DiffPart, ...] = ()
    # Короткий текстовий підсумок diff'у ("8 → 7"), для Excel/plain-
    # контекстів, де структурований `diff_parts` незручний.
    diff_summary: str | None = None
    note: str | None = None  # "case-only" | "layout directive" | ...


@dataclass(frozen=True)
class CopyStats:
    """`coverage` — частка речень документа, знайдених на сайті (точно чи
    змінено) над усім порівнюваним (без `excluded`); `0.0`, коли
    порівнювати не було чого. Збережене ПОЛЕ, не `@property` — `stats_of()`
    рахує його один раз при побудові, щоб `dataclasses.asdict()`
    (персистенція `FrameworkOutcome` -> `frameworks_json`, той самий
    прийом, що `matrix.py`) перенесло значення без ручного мапінгу."""

    match: int = 0
    changed: int = 0
    missing: int = 0
    extra: int = 0
    excluded: int = 0
    coverage: float = 0.0


@dataclass(frozen=True)
class CopyTabResult:
    """Результат звірки ОДНІЄЇ Copy-вкладки з живою сторінкою (task-92
    §2.1). `role` — чи це вкладка, обрана як основна для сторінки (за
    покриттям, `package.py`), чи другорядна (мультимовний документ,
    task-92 §1.8/§2.5). `genre`: `"copy"` — готова проза;
    `"edit_brief"` — вкладка правок для розробника (маркери `[KEEP]`/
    `[EDIT]`, task-92 §1.6) — рядки `missing` там не є помилками копії."""

    tab_title: str
    genre: Literal["copy", "edit_brief"]
    role: Literal["primary", "secondary"]
    state: Literal["checked", "no_url", "not_checked", "not_applicable"]
    reason: str | None
    language_hint: str | None
    rows: tuple[CopyRow, ...] = field(default_factory=tuple)
    stats: CopyStats = field(default_factory=CopyStats)


# ── Класифікація жанру (task-92 §1.6) ───────────────────────────────────────


# Live correction (task-92, found via UI verification on LAXTON, not the
# original portfolio scan): готова копія іноді несе РІВНО ОДНУ вкраплену
# editorial-примітку серед завершеної прози — LAXTON_PAGE_FRAMEWORK_
# UTRUSTNING's Content tabs have exactly 1 marker each
# (`[Keep existing OBS-rad + "Se Köpvillkor" link below the FAQ]`), not 0
# as the original §1.6 sample suggested. A single marker must not flip a
# 23-block finished tab into "brief". The measured 3XE minimum is 2
# markers per tab — `>= 2` is the threshold that separates the two
# without reclassifying LAXTON.
_EDIT_BRIEF_MIN_MARKERS = 2


def genre_of(blocks: Sequence[DocBlock]) -> Literal["copy", "edit_brief"]:
    """Бімодальний розподіл, заміряний живо: 0-1 маркер на кожній вкладці
    EFFYS/LAXTON (готова копія, іноді з поодинокою приміткою), >=2 на
    кожній вкладці 3XE (бриф правок)."""
    marked = sum(1 for b in blocks if b.has_marker)
    return "edit_brief" if marked >= _EDIT_BRIEF_MIN_MARKERS else "copy"


# ── Сегментація ──────────────────────────────────────────────────────────────

# Скорочення, після яких крапка не завершує речення — той самий набір, що
# заміряно на живому корпусі EFFYS/3XE/LAXTON (task-92 §2.2), без
# претензії на вичерпність: жодна виміряна помилка сегментації на 17
# живих сторінках не вимагала NLP-бібліотеки (`pyproject.toml` належить
# task-01, зовнішня залежність — окремий запит).
_SENTENCE_END = r"(?<=[.!?…])"
# Lookbehind width includes the terminal period itself — a fixed-width
# `(?<!\bDr)` checks the two characters ending at the split point, which
# with `_SENTENCE_END` anchored on the period are "r." (not "Dr") and so
# NEVER blocks anything (live bug, caught by
# `test_split_sentences_does_not_break_on_common_abbreviations`): the
# abbreviation itself must be checked ENDING AT the period, not before it.
_ABBR = (
    r"(?<!\bMr\.)(?<!\bMrs\.)(?<!\bDr\.)(?<!\bSt\.)(?<!\bInc\.)(?<!\bvs\.)(?<!\bNo\.)"
    r"(?<!\bJr\.)(?<!\bSr\.)(?<![A-ZÅÄÖ]\.)"
)
_SENTENCE_SPLIT_RE = re.compile(rf"{_ABBR}{_SENTENCE_END}[\"')\]”]*\s+(?=[A-ZÅÄÖÉ0-9“\"'(])")


def split_sentences(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    parts = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    return parts or [text]


def segment_doc_blocks(blocks: Iterable[DocBlock]) -> list[Segment]:
    """`DocBlock` (task-92 §2.2) -> `Segment`. Заголовок/директива/пункт
    списку лишаються цілими (пункт списку не має термінальної пунктуації
    — ділити його на речення хибно розрізало б перелік); проза ділиться
    на речення."""
    segments: list[Segment] = []
    section: str | None = None
    for b in blocks:
        if b.kind == "heading":
            section = b.text
            segments.append(Segment(kind="heading", level=b.level, section=section, text=b.text))
        elif b.kind == "directive":
            segments.append(Segment(kind="directive", level=None, section=section, text=b.text))
        elif b.kind == "item":
            segments.append(Segment(kind="item", level=None, section=section, text=b.text))
        else:
            for sentence in split_sentences(b.text):
                segments.append(
                    Segment(kind="sentence", level=None, section=section, text=sentence)
                )
    return segments


def segment_site_blocks(blocks: Iterable[SiteBlock]) -> list[Segment]:
    """Той самий алгоритм для `SiteBlock` (`site.py`) — сайт не має
    директив, лише заголовок/пункт/проза."""
    segments: list[Segment] = []
    section: str | None = None
    for b in blocks:
        if b.kind == "heading":
            section = b.text
            segments.append(Segment(kind="heading", level=b.level, section=section, text=b.text))
        elif b.kind == "item":
            segments.append(Segment(kind="item", level=None, section=section, text=b.text))
        else:
            for sentence in split_sentences(b.text):
                segments.append(
                    Segment(kind="sentence", level=None, section=section, text=sentence)
                )
    return segments


# ── Нормалізація (task-92 §2.3) — що НІКОЛИ не вважається різницею ──────────

_PUNCT_MAP = {
    0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'",
    0x201C: '"', 0x201D: '"', 0x201E: '"',
    0x2032: "'", 0x2033: '"',
    0x2013: "-", 0x2014: "-", 0x2212: "-", 0x2010: "-", 0x2011: "-",
    0x2026: "...",
    0x00A0: " ", 0x202F: " ", 0x2009: " ",
    0x200B: "", 0x00AD: "", 0x2060: "", 0xFEFF: "",
}


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).translate(_PUNCT_MAP)
    t = re.sub(r"\s+", " ", t).strip()
    t = t.strip(" \t·•–-—*:")
    return t.casefold()


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


# ── Послівний diff (для стану "changed") ────────────────────────────────────


def diff_words(doc_text: str, site_text: str) -> tuple[DiffPart, ...]:
    doc_words, site_words = doc_text.split(), site_text.split()
    sm = SequenceMatcher(
        None, [w.casefold() for w in doc_words], [w.casefold() for w in site_words], autojunk=False
    )
    parts: list[DiffPart] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        parts.append(DiffPart(
            tag="equal" if tag == "equal" else "changed",
            doc_text=" ".join(doc_words[i1:i2]), site_text=" ".join(site_words[j1:j2]),
        ))
    return tuple(parts)


def diff_summary(parts: tuple[DiffPart, ...], *, max_len: int = 200) -> str:
    changed = [f"{p.doc_text or '∅'} → {p.site_text or '∅'}" for p in parts if p.tag == "changed"]
    return " | ".join(changed)[:max_len]


def _changed_row(doc: Segment, site: Segment, *, ratio: float, confidence: Confidence) -> CopyRow:
    parts = diff_words(doc.text, site.text)
    return CopyRow(
        state="changed", kind=doc.kind, section=doc.section,
        doc_text=doc.text, site_text=site.text, ratio=ratio, confidence=confidence,
        diff_parts=parts, diff_summary=diff_summary(parts),
    )


def _match_row(doc: Segment, site: Segment) -> CopyRow:
    note = "case-only" if doc.text != site.text else None
    return CopyRow(
        state="match", kind=doc.kind, section=doc.section,
        doc_text=doc.text, site_text=site.text, ratio=1.0, note=note,
    )


def _missing_row(doc: Segment, *, ratio: float) -> CopyRow:
    return CopyRow(
        state="missing", kind=doc.kind, section=doc.section,
        doc_text=doc.text, site_text=None, ratio=ratio,
    )


def _extra_row(site: Segment) -> CopyRow:
    return CopyRow(
        state="extra", kind=site.kind, section=site.section,
        doc_text=None, site_text=site.text,
    )


def _excluded_row(doc: Segment) -> CopyRow:
    return CopyRow(
        state="excluded", kind=doc.kind, section=doc.section,
        doc_text=doc.text, site_text=None, note="layout directive",
    )


# ── Вирівнювання (task-92 §2.4) ──────────────────────────────────────────────


def align_segments(
    doc_segments: Sequence[Segment],
    site_segments: Sequence[Segment],
    *,
    cross_language: bool = False,
) -> list[CopyRow]:
    """`SequenceMatcher` по списку НОРМАЛІЗОВАНИХ сегментів (не по
    символах усієї сторінки) підказує, які речення варто зіставити в
    пару; сам вердикт — точна рівність (§1.5), `ratio` лише вирішує, яка
    пара взагалі утворюється.

    `cross_language=True` (вкладка не тією мовою, що жива сторінка,
    task-92 §1.8/§2.5) вимикає слабкий рівень зіставлення (0.55-0.75) —
    саме він давав хибні пари на спільних токенах-запозиченнях
    (`"...EVP..."` в обох мовах). Лишається лише сильний рівень (>=0.75),
    де реальний переклад все одно рідко випадково потрапляє."""
    # Сегмент, порожній ПІСЛЯ нормалізації, не порівнюється взагалі — живий
    # випадок: абзац Docs, що складається з одного `​` (zero-width
    # space). `str.split()` його не зʼїдає (не whitespace, а format-
    # символ), тож він доживав до вирівнювання як «речення» довжиною 1 і
    # звітувався як `missing` — 10 із 356 рядків-помилок по всіх пакетах,
    # до 40% на окремій сторінці (LAXTON Ouija: 2 з 5). Ані doc-, ані
    # site-бік такого не має породжувати ні `missing`, ні `extra`.
    comparable = [s for s in doc_segments if s.kind != "directive" and normalize(s.text)]
    excluded = [s for s in doc_segments if s.kind == "directive"]
    site_segments = [s for s in site_segments if normalize(s.text)]

    doc_norm = [normalize(s.text) for s in comparable]
    site_norm = [normalize(s.text) for s in site_segments]
    matcher = SequenceMatcher(None, doc_norm, site_norm, autojunk=False)
    floor = _STRONG_RATIO if cross_language else _WEAK_RATIO

    rows: list[CopyRow] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append(_match_row(comparable[i1 + k], site_segments[j1 + k]))
            continue
        right = list(range(j1, j2))
        used: set[int] = set()
        for i in range(i1, i2):
            best_ratio, best_j = 0.0, None
            for j in right:
                if j in used:
                    continue
                r = _ratio(doc_norm[i], site_norm[j])
                if r > best_ratio:
                    best_ratio, best_j = r, j
            if best_j is not None and best_ratio >= floor:
                used.add(best_j)
                if doc_norm[i] == site_norm[best_j]:
                    rows.append(_match_row(comparable[i], site_segments[best_j]))
                else:
                    confidence: Confidence = "strong" if best_ratio >= _STRONG_RATIO else "weak"
                    rows.append(_changed_row(
                        comparable[i], site_segments[best_j],
                        ratio=best_ratio, confidence=confidence,
                    ))
            else:
                rows.append(_missing_row(comparable[i], ratio=best_ratio))
        for j in right:
            if j not in used:
                rows.append(_extra_row(site_segments[j]))

    rows.extend(_excluded_row(s) for s in excluded)
    return rows


def stats_of(rows: Iterable[CopyRow]) -> CopyStats:
    counts = {"match": 0, "changed": 0, "missing": 0, "extra": 0, "excluded": 0}
    for row in rows:
        counts[row.state] += 1
    total = counts["match"] + counts["changed"] + counts["missing"]
    coverage = (counts["match"] + counts["changed"]) / total if total else 0.0
    return CopyStats(**counts, coverage=coverage)


def build_copy_tab_result(
    *,
    tab_title: str,
    doc_blocks: Sequence[DocBlock],
    site_blocks: Sequence[SiteBlock],
    cross_language: bool = False,
) -> CopyTabResult:
    """Один (Copy-вкладка, жива сторінка) прогін — чиста функція, без
    вибору ролі (`role` завжди `"primary"` тут; `package.py` перевизначає
    на `"secondary"` для вкладок з нижчим покриттям, коли документ має
    кілька копі-вкладок, task-92 §2.5)."""
    genre = genre_of(doc_blocks)
    doc_segments = segment_doc_blocks(doc_blocks)
    site_segments = segment_site_blocks(site_blocks)
    rows = align_segments(doc_segments, site_segments, cross_language=cross_language)
    return CopyTabResult(
        tab_title=tab_title, genre=genre, role="primary", state="checked", reason=None,
        language_hint=None, rows=tuple(rows), stats=stats_of(rows),
    )


__all__ = [
    "CopyRow", "CopyStats", "CopyTabResult", "DiffPart", "Segment",
    "align_segments", "build_copy_tab_result", "diff_summary", "diff_words",
    "genre_of", "normalize", "segment_doc_blocks", "segment_site_blocks",
    "split_sentences", "stats_of",
]
