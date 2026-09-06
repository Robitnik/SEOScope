"""Шлях запису журналу (task-72) — найважливіший файл цієї таски.

Чому не «просто INSERT у транзакції запиту»: до task-66/67 writer-лок цієї
бази був зайнятий **97% часу**, і кожен зайвий запис у гарячому шляху
множиться на всіх, хто стоїть у черзі на запис. Журнал, який логує кожну
мутацію, подвоїв би кількість записів у системі й повернув лок-шторм — тобто
інструмент спостереження зламав би те, за чим спостерігає.

Тому: запит кладе рядок у обмежену чергу за мікросекунди й іде далі. Один
потік-флашер збирає їх у батч (≤`batch_size` рядків або ≤`flush_interval_s`)
і пише ОДНІЄЮ транзакцією через `DbWriteGate` — тобто ~1 запис на секунду
замість одного на кожну дію.

Три чесні межі:
- черга обмежена: під навалом ми **скидаємо** рядки, але не тихо — лічильник
  `dropped` видно в `/healthz`, і це видима деградація, а не мовчазна втрата;
- якщо БД недоступна, батч дописується в `audit_fallback.jsonl` — інцидент,
  під час якого база лежить, не має бути єдиним нежурнальованим;
- `flush()` детермінований, щоб тести не спали в очікуванні потоку.
"""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.base import utcnow

logger = get_logger(__name__)


@dataclass
class AuditRecord:
    """Те, що кладеться в чергу. Плоскі дані, без ORM і без сесії:
    об'єкт переживає завершення запиту, який його породив."""

    action: str
    ts: datetime = field(default_factory=utcnow)
    actor_kind: str = "system"
    actor_id: str | None = None
    actor_email: str | None = None
    actor_name: str | None = None
    actor_role: str | None = None
    session_id: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    resource_label: str | None = None
    method: str | None = None
    path: str | None = None
    status_code: int | None = None
    outcome: str = "success"
    duration_ms: int | None = None
    details: dict[str, Any] = field(default_factory=dict)


_STOP = object()


class AuditSink:
    def __init__(
        self,
        *,
        max_queue: int | None = None,
        batch_size: int | None = None,
        flush_interval_s: float | None = None,
        fallback_path: Path | None = None,
    ) -> None:
        settings = get_settings()
        self._max_queue = max_queue if max_queue is not None else settings.audit_queue_max
        self._batch_size = batch_size if batch_size is not None else settings.audit_batch_size
        self._flush_interval_s = (
            flush_interval_s if flush_interval_s is not None else settings.audit_flush_interval_s
        )
        self._fallback_path = fallback_path or Path(settings.audit_fallback_path)
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=self._max_queue)
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self.dropped = 0
        self.written = 0
        self.failed = 0
        # Ретенція живе тут, а не в окремому шедулері, свідомо: цей потік уже
        # має природний момент «нікому нічого не треба» (черга порожня), а
        # заводити ще один демон заради одного виклику на добу — зайва
        # рухома частина. Дата, а не інтервал: рестарт не має ні пропускати
        # прибирання, ні робити його двічі.
        self._last_retention_date: Any = None

    # ── життєвий цикл ────────────────────────────────────────────────────

    def start(self) -> None:
        with self._start_lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="audit-sink", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._start_lock:
            thread = self._thread
            if thread is None:
                return
            self._queue.put(_STOP)
            thread.join(timeout=10)
            self._thread = None

    # ── запис ────────────────────────────────────────────────────────────

    def submit(self, record: AuditRecord) -> bool:
        """Ніколи не блокує викликача і ніколи не кидає винятку.

        Журнал не має права зламати дію, яку він журналює: провал запису — це
        втрачений рядок і WARNING, а не 500 на кнопці «Refresh data».
        """
        self.start()
        try:
            self._idle.clear()
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            self.dropped += 1
            logger.warning(
                "audit_dropped action=%s total_dropped=%s queue_max=%s",
                record.action,
                self.dropped,
                self._max_queue,
            )
            return False

    def flush(self, timeout: float = 10.0) -> None:
        """Дочекатись, поки черга спорожніє і батч ляже в БД.

        Для тестів і для коректного shutdown — щоб «дію зроблено» і «дію
        записано» можна було перевірити без `sleep`.
        """
        if self._thread is None:
            self._drain_inline()
            return
        self._idle.wait(timeout)

    # ── внутрішнє ────────────────────────────────────────────────────────

    def _run(self) -> None:
        batch: list[AuditRecord] = []
        while True:
            try:
                item = self._queue.get(timeout=self._flush_interval_s)
            except queue.Empty:
                if batch:
                    self._write(batch)
                    batch = []
                self._idle.set()
                self._maybe_run_retention()
                continue
            if item is _STOP:
                if batch:
                    self._write(batch)
                self._idle.set()
                return
            batch.append(item)
            if len(batch) >= self._batch_size:
                self._write(batch)
                batch = []
                if self._queue.empty():
                    self._idle.set()

    def _drain_inline(self) -> None:
        batch: list[AuditRecord] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not _STOP:
                batch.append(item)
        if batch:
            self._write(batch)

    def _maybe_run_retention(self) -> None:
        settings = get_settings()
        today = utcnow().date()
        if self._last_retention_date == today or not settings.audit_enabled:
            return
        self._last_retention_date = today
        try:
            from app.audit.retention import run_retention
            from app.db.session import get_sync_sessionmaker

            with get_sync_sessionmaker()() as session:
                result = run_retention(session, settings=settings)
        except Exception as exc:  # noqa: BLE001 - прибирання ніколи не валить потік
            logger.warning("audit_retention_failed error=%s", exc)
            return
        if result["purged"] or result["expired_sessions"]:
            # Сам факт очищення — теж дія системи; без цього рядка в журналі
            # була б дірка, яку нічим пояснити.
            self.submit(
                AuditRecord(
                    action="system.audit.purge",
                    actor_kind="system",
                    details=dict(result),
                )
            )

    def _write(self, batch: list[AuditRecord]) -> None:
        """Один батч — одна транзакція через глобальний write-gate."""
        from app.db.repositories.audit import AuditRepository
        from app.db.session import get_sync_sessionmaker
        from app.db.writegate import get_write_gate

        try:
            with get_sync_sessionmaker()() as session:
                repo = AuditRepository(session)
                get_write_gate().submit(session, lambda: repo.bulk_append(batch))
            self.written += len(batch)
        except Exception as exc:  # noqa: BLE001 - будь-яка помилка БД
            self.failed += len(batch)
            logger.warning("audit_write_failed rows=%s error=%s", len(batch), exc)
            self._write_fallback(batch)

    def _write_fallback(self, batch: list[AuditRecord]) -> None:
        """База недоступна — дописуємо JSONL поруч із нею.

        Саме той момент, коли журнал потрібен найбільше, — і саме тоді
        звичайний шлях не працює.
        """
        try:
            self._fallback_path.parent.mkdir(parents=True, exist_ok=True)
            with self._fallback_path.open("a", encoding="utf-8") as handle:
                for record in batch:
                    handle.write(json.dumps(asdict(record), default=str) + "\n")
        except OSError as exc:
            logger.error("audit_fallback_failed rows=%s error=%s", len(batch), exc)

    def health(self) -> dict[str, int]:
        return {
            "queued": self._queue.qsize(),
            "written": self.written,
            "dropped": self.dropped,
            "failed": self.failed,
        }


_sink: AuditSink | None = None
_sink_lock = threading.Lock()


def get_audit_sink(settings: Settings | None = None) -> AuditSink:
    global _sink
    with _sink_lock:
        if _sink is None:
            _sink = AuditSink()
        return _sink


def reset_audit_sink_for_tests() -> None:
    global _sink
    with _sink_lock:
        if _sink is not None:
            _sink.stop()
        _sink = None
