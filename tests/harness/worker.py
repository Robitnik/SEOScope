"""Підняття справжнього worker-а всередині тесту (task-13).

Не імітація: запускається той самий `Dispatcher` (task-05), що і в проді —
доріжка-потік на кожен сервіс, той самий `run_lane`/`run_cell`, той самий
atomic claim. Тести таким чином перевіряють реальну конкурентність
(«між сервісами паралельно, усередині сервісу послідовно», PLAN §9.1), а не
свою переписану її версію.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

# Тестовий таймаут клітинки: сценарій `timeout` у FakeAdapter спить годину,
# тож ждати прод-дефолт (20 хв) не можна. Значення підібране так, щоб
# завідомо перевищувати будь-який «швидкий» сценарій (stage_seconds ~0.05).
TEST_CELL_TIMEOUT_SECONDS = 5


@contextmanager
def running_worker(
    session_scope: Any,
    *,
    providers: tuple[Any, ...] | None = None,
    lease_ttl_seconds: int = 300,
    cell_timeout_seconds: int = TEST_CELL_TIMEOUT_SECONDS,
    poll_interval_seconds: float = 0.05,
    parallelism: dict[Any, int] | None = None,
) -> Iterator[Any]:
    """Запустити диспетчер у фоновому потоці й гарантовано зупинити його.

    `poll_interval_seconds` підмінюється монкіпатчем на рівні модуля `lane`:
    прод-значення 1 с зробило б кожен інтеграційний тест багатосекундним, а
    сам інтервал — деталь налаштування, не частина контракту.
    """
    from app.jobs import lane
    from app.jobs.dispatcher import Dispatcher
    from app.schemas.enums import Provider

    original_poll = lane.POLL_INTERVAL_SECONDS
    lane.POLL_INTERVAL_SECONDS = poll_interval_seconds

    dispatcher = Dispatcher(
        session_scope=session_scope,
        lease_owner="test-worker",
        providers=providers or tuple(Provider),
        lease_ttl_seconds=lease_ttl_seconds,
        cell_timeout_seconds=cell_timeout_seconds,
        supervisor_interval_seconds=0.05,
        # task-31: how many lanes each provider gets. Omitted -> one lane per
        # provider, i.e. exactly the dispatcher every existing test asserts on.
        parallelism=parallelism,
    )
    thread = threading.Thread(target=dispatcher.run_forever, name="test-dispatcher", daemon=True)
    thread.start()
    try:
        yield dispatcher
    finally:
        dispatcher.stop()
        thread.join(timeout=30)
        lane.POLL_INTERVAL_SECONDS = original_poll


def wait_for(
    predicate: Callable[[], bool], *, timeout: float = 15.0, interval: float = 0.05, what: str = ""
) -> None:
    """Активне очікування умови з чесним повідомленням про таймаут.

    Стани клітинок пишуться іншим потоком, тому синхронізація тут — саме
    очікування умови, а не `sleep` на «достатньо довго».
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for: {what or predicate!r}")
