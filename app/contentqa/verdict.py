"""Спільний тип вердикту поля (task-91 §2.3, винесено в task-95 §2.5).

Живе в окремому модулі без жодних `contentqa`-залежностей, щоб уникнути
циклу імпортів: `matrix.py` (Strategy/Framework/Site) і `links.py`
(Framework -> Copy -> Site) обидва повертають `FieldVerdict`, а `links.py`
уже імпортує `FrameworkOutcome` з `package.py`, який, своєю чергою, буде
викликати `links.py` — якби `FieldVerdict` лишався визначений у
`matrix.py` (яка сама імпортує `package.py`), вийшов би цикл
`matrix -> package -> links -> matrix`. `matrix.py` перевикористовує (і
ре-експортує) звідси той самий тип, як і раніше.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Verdict = Literal["match", "mismatch", "not_comparable"]


@dataclass(frozen=True)
class FieldVerdict:
    state: Verdict
    detail: str | None = None
    # Live feedback (task-91 §3.6): a "mismatch" must say WHICH source is
    # the odd one out, not paint all three sources red — when Strategy and
    # Framework agree and only Site differs, Strategy/Framework should read
    # green (they agree with each other), only Site red. Names of sources
    # ("strategy"/"framework"/"site") whose value is not shared by any
    # other source with a value; `()` for "match"/"not_comparable" states.
    outlier_sources: tuple[str, ...] = ()


__all__ = ["FieldVerdict", "Verdict"]
