"""Фазова телеметрія прогресу клітинки (task-32).

**Навіщо це існує.** `make_emitter` уже персистить `ProgressEvent.percent`
у `cells.progress_percent` (`app/jobs/events.py`), але жоден метричний адаптер
його не заповнював: усі емітили лише `stage`. `CellRepository.set_progress`
ігнорує `None`, тож відсоток клітинки стояв на `0.0` від старту до
термінального статусу, де його одноразово ставили в `100.0`. UI показував
рухому анімацію навколо мертвого числа, а `progress_percent` усього job-а —
середнє по клітинках (`app/api/jobs.py`) — стрибав дискретно, по 1/N.

**Модель.** Одна клітинка збирається кількома викликами адаптера, які runner
робить послідовно (`collect()` → `collect_dimensions()` → `collect_datasets()`),
і кожен отримує той самий `emit`, але **не** спільний стан. Тому шкала
клітинки ділиться на фази зі статичними вагами: кожен виклик створює власний
трекер, оголошує свою фазу, і базове зміщення виводиться з таблиці ваг. Так
монотонність між викликами гарантується самою розкладкою (фаза `kpi`
закінчується там, де починається `datasets`), без спільного об'єкта і без
зміни контракту `ProviderAdapter` (task-02, read-only).

**Чесність (frontend/AGENTS.md §9).** Всередині фази відсоток рахується з
реального `completed/total`, відомого до початку циклу. Крок, тривалість якого
виміряти неможливо (очікування експорту від зовнішнього сервісу), емітиться
через `indeterminate()` — **без** `percent`, з маркером `details.indeterminate`,
щоб UI показав чесний невизначений стан замість анімованого фейкового числа.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from app.schemas.adapter import ProgressEvent
from app.schemas.enums import Provider

# Ваги фаз у відсотках шкали однієї клітинки. Це ОЦІНКА розподілу часу, а не
# вимір: всередині фази відсоток чесний (`completed/total`), а пропорції між
# фазами відображають спостережену тривалість (для Ahrefs і Reporting Ninja
# датасети — на порядок довші за KPI-знімок). Порядок ключів = порядок фаз,
# з нього виводиться базове зміщення; сума кожної розкладки має бути 100.
CELL_PHASE_WEIGHTS: dict[Provider, dict[str, float]] = {
    Provider.ahrefs: {"kpi": 25.0, "datasets": 70.0, "persist": 5.0},
    Provider.reporting_ninja: {"kpi": 20.0, "datasets": 75.0, "persist": 5.0},
    Provider.gsc: {"kpi": 30.0, "dimensions": 40.0, "datasets": 25.0, "persist": 5.0},
    Provider.semrush: {"kpi": 95.0, "persist": 5.0},
}

# Провайдер без власної розкладки (або нова фаза, ще не описана тут).
DEFAULT_PHASE_WEIGHTS: dict[str, float] = {"kpi": 60.0, "datasets": 35.0, "persist": 5.0}

# Не частіше однієї події на фазу за цей інтервал. Межі фаз, зміна стадії,
# останній крок і не-`info` рівні проходять повз throttle: саме вони несуть
# інформацію, а проміжні тіки — лише згладжування.
DEFAULT_THROTTLE_SECONDS = 0.75

_LEVELS = frozenset({"info", "warning", "error"})


def _safe_level(value: Any) -> str:
    """`ProgressEvent.level` — це `Literal`; чужий рівень із зовнішнього пакета
    інакше зронив би валідацію і подія просто зникла б."""
    text = str(value or "info")
    return text if text in _LEVELS else "info"


def phase_bounds(weights: Mapping[str, float], phase: str) -> tuple[float, float]:
    """`(base, span)` фази у шкалі 0..100.

    Невідома фаза свідомо не падає і не пропускає прогрес: вона трактується як
    єдина фаза на всю шкалу. Прогрес — діагностика, він ніколи не є приводом
    зламати збір (`app/integrations/contract.py`).
    """
    if phase not in weights:
        return 0.0, 100.0
    base = 0.0
    for name, weight in weights.items():
        if name == phase:
            return base, float(weight)
        base += float(weight)
    return base, 100.0 - base


class PhaseTracker:
    """Перетворює кроки однієї фази на `ProgressEvent` з реальним `percent`.

    Створюється на кожен виклик адаптера (`collect`, `collect_datasets`, …).
    Усі методи best-effort: зламаний `emit` або дивні аргументи ніколи не
    вилітають назовні — це той самий контракт, що й у `make_emitter`.
    """

    def __init__(
        self,
        provider: Provider,
        emit: Callable[[ProgressEvent], None],
        phase: str,
        *,
        weights: Mapping[str, float] | None = None,
        throttle_seconds: float = DEFAULT_THROTTLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider
        self.phase = phase
        self._emit = emit
        layout = weights or CELL_PHASE_WEIGHTS.get(provider, DEFAULT_PHASE_WEIGHTS)
        self.base, self.span = phase_bounds(layout, phase)
        self._throttle = max(0.0, throttle_seconds)
        self._clock = clock
        self._last_emit_at: float | None = None
        self._last_stage: str | None = None
        self._last_percent: float = self.base

    # -- внутрішнє ---------------------------------------------------------

    def _percent_for(self, completed: float, total: float) -> float:
        if total <= 0:
            return self._last_percent
        fraction = min(1.0, max(0.0, completed / total))
        # Монотонність у межах фази: зміна стадії не має смикати смугу назад.
        return max(self._last_percent, self.base + self.span * fraction)

    def _should_emit(self, stage: str, *, force: bool) -> bool:
        if force or stage != self._last_stage or self._last_emit_at is None:
            return True
        return (self._clock() - self._last_emit_at) >= self._throttle

    def _send(
        self,
        stage: str,
        *,
        percent: float | None,
        completed: int | None,
        total: int | None,
        message: str,
        level: str,
        details: Mapping[str, Any] | None,
        force: bool,
    ) -> None:
        try:
            if not self._should_emit(stage, force=force):
                return
            payload: dict[str, Any] = {"phase": self.phase}
            if details:
                payload.update(details)
            self._emit(
                ProgressEvent(
                    provider=self.provider,
                    stage=stage,
                    completed=completed,
                    total=total,
                    percent=percent,
                    level=_safe_level(level),  # type: ignore[arg-type]
                    message=message,
                    details=payload,
                )
            )
            self._last_emit_at = self._clock()
            self._last_stage = stage
            if percent is not None:
                self._last_percent = percent
        except Exception:  # noqa: BLE001 - прогрес ніколи не ламає збір
            pass

    # -- публічне ----------------------------------------------------------

    def begin(
        self, stage: str, *, message: str = "", details: Mapping[str, Any] | None = None
    ) -> None:
        """Межа фази: відсоток стає на її базу. Ніколи не throttle-иться."""
        self._send(
            stage,
            percent=max(self._last_percent, self.base),
            completed=None,
            total=None,
            message=message,
            level="info",
            details=details,
            force=True,
        )

    def step(
        self,
        stage: str,
        *,
        completed: int,
        total: int,
        message: str = "",
        level: str = "info",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Вимірюваний крок: `completed` із `total`, відомого до початку циклу."""
        self._send(
            stage,
            percent=self._percent_for(completed, total),
            completed=completed,
            total=total,
            message=message,
            level=level,
            details=details,
            # Останній крок фази і будь-яка проблема мають з'явитись негайно.
            force=(total > 0 and completed >= total) or _safe_level(level) != "info",
        )

    def indeterminate(
        self, stage: str, *, message: str = "", details: Mapping[str, Any] | None = None
    ) -> None:
        """Крок невідомої тривалості: без `percent`, з явним маркером для UI.

        `set_progress` ігнорує `percent=None`, тож смуга лишається там, де була,
        а `details.indeterminate` дозволяє фронту показати «триває, тривалість
        невідома» замість повзучого фейкового числа (AGENTS.md §9).
        """
        self._send(
            stage,
            percent=None,
            completed=None,
            total=None,
            message=message,
            level="info",
            details={**(details or {}), "indeterminate": True},
            force=True,
        )

    def done(self, stage: str = "complete", *, message: str = "") -> None:
        """Фаза завершена: відсоток на її верхній межі."""
        self._send(
            stage,
            percent=max(self._last_percent, self.base + self.span),
            completed=None,
            total=None,
            message=message,
            level="info",
            details=None,
            force=True,
        )

    def bridge(self, stages: Iterable[str]) -> Callable[[Any], None]:
        """Обгортка над подіями AISeoTools, що додає їм відсоток.

        Зовнішній пакет пінований і змінам не підлягає (CLAUDE.md §4), а його
        події несуть `stage`, але не `percent`. `stages` — очікувана
        послідовність стадій цієї фази; позиція стадії в ній дає `completed`.
        Стадія поза списком (нова у пакеті) проходить як індетермінантна: це
        не втрата даних, лише відсутність відсотка для незнайомого кроку.
        """
        order = list(stages)
        total = len(order)

        def _on_event(event: Any) -> None:
            try:
                stage = str(getattr(event, "stage", "") or "")
                message = str(getattr(event, "message", "") or "")
                level = str(getattr(event, "level", "info") or "info")
                details = dict(getattr(event, "details", {}) or {})
                if stage in order:
                    self.step(
                        stage,
                        completed=order.index(stage) + 1,
                        total=total,
                        message=message,
                        level=level,
                        details=details,
                    )
                else:
                    self.indeterminate(stage, message=message, details=details)
            except Exception:  # noqa: BLE001 - прогрес ніколи не ламає збір
                pass

        return _on_event
