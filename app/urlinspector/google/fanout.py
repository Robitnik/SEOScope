"""Розклад незалежних запитів у пул (task-88 §0.6).

Причина не теоретична. Хвиля зрізів — це ~20 запитів (4 GSC-дименсії, 6 типів
пошуку, 10 GA4-зрізів), і послідовно вони дали **6399 мс** на живому прогоні,
хоча кожен окремо коштує 300-900 мс. Ті самі 20 паралельно вкладаються в
латентність найповільнішого. Тому колектори не викликають API рядком за
рядком, а віддають розклад, і хто його виконує — вирішує оркестратор.

Два інструменти, бо задачі різні:

- `defer` — для КІЛЬКОХ РІЗНОТИПНИХ запитів (серія + тотали + запити). Типи
  зберігаються, бо кожен `Deferred[T]` знає свій `T`; паралелізм працює, поки
  всі `defer` викликані ДО першого `.result()`.
- `fanout` — для ОДНОТИПНОГО списку (усі зрізи однієї форми). Виняток
  приходить значенням, а не піднімається: збій одного зрізу мусить лишити
  картку на сторінці зі станом `failed`, а не забрати з нею решту (§2.5).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import Executor, Future
from typing import Generic, TypeVar

T = TypeVar("T")


class Deferred(Generic[T]):
    """Значення, яке або вже обчислене, або ще в пулі.

    `.result()` піднімає виняток задачі — на відміну від `fanout`: там збій
    однієї картки локальний, а тут викличник збирає кілька частин ОДНІЄЇ
    панелі, і половина панелі гірша за честний `failed` на всю.
    """

    __slots__ = ("_future", "_value")

    def __init__(self, *, future: Future[T] | None = None, value: T | None = None) -> None:
        self._future = future
        self._value = value

    def result(self) -> T:
        if self._future is not None:
            return self._future.result()
        return self._value  # type: ignore[return-value]


def defer(fn: Callable[[], T], *, executor: Executor | None) -> Deferred[T]:
    if executor is None:
        return Deferred(value=fn())
    return Deferred(future=executor.submit(fn))


def fanout(
    tasks: Sequence[Callable[[], T]], *, executor: Executor | None
) -> list[T | BaseException]:
    """Порядок результатів завжди відповідає порядку `tasks` — картки на
    сторінці не мають перестрибувати між завантаженнями."""
    if executor is None:
        return [_safe(task) for task in tasks]
    futures = [executor.submit(task) for task in tasks]
    out: list[T | BaseException] = []
    for future in futures:
        try:
            out.append(future.result())
        except BaseException as exc:  # noqa: BLE001 — навмисно як значення
            out.append(exc)
    return out


def _safe(task: Callable[[], T]) -> T | BaseException:
    try:
        return task()
    except BaseException as exc:  # noqa: BLE001
        return exc
