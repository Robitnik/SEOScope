"""Підмодуль аналітики (task-100 §2.6): чи ручне індексування щось дає.

Журнал «послали тоді-то, зайшло тоді-то» сам по собі нічого не доводить:
сторінки заходять в індекс і без нас. Тому головна цифра тут — не медіана
лагу відправлених, а **різниця** між нею і медіаною контрольної групи:
сторінок, які за той самий період зайшли в індекс, жодного разу не
потрапивши у відправку.

Якщо різниці немає, модуль зобов'язаний це показати. Сто ручних операцій
на день — дорога звичка, і найцінніше, що тул може сказати, це «припиніть».

Дві чесності вибірки, без яких цифри брешуть:

1. **Незавершені спостереження не рахуються в медіану.** Сторінка,
   відправлена три дні тому, ще не «не проіндексувалась» — вона в дорозі.
   Вона йде окремим числом `in_flight`, інакше медіана падала б щоразу
   після великої відправки.
2. **Порожня вибірка дає `None`, не `0`.** Нуль днів читався б як «заходить
   того ж дня», тобто найкращий можливий результат (PLAN §12).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.repositories.indexing import IndexingRepository, median, percentile
from app.schemas.indexing import (
    IndexingAnalyticsDTO,
    IndexingClientStatDTO,
    IndexingControlGroupDTO,
    IndexingFunnelDTO,
    IndexingLagDTO,
    IndexingReasonStatDTO,
    IndexingRetentionDTO,
)

DEFAULT_WINDOW_DAYS = 180
RETENTION_HORIZONS = (30, 60, 90)


def _lag(samples: list[int]) -> IndexingLagDTO:
    return IndexingLagDTO(
        sample=len(samples),
        median_days=median(samples),
        p25_days=percentile(samples, 0.25),
        p75_days=percentile(samples, 0.75),
    )


def build_analytics(
    session: Session, *, window_days: int = DEFAULT_WINDOW_DAYS
) -> IndexingAnalyticsDTO:
    repo = IndexingRepository(session)
    since = utcnow() - timedelta(days=window_days)
    clients = repo.active_clients()
    client_ids = [row.id for row in clients]
    names = {row.id: row.name for row in clients}

    funnel_raw = repo.funnel(since=since)
    funnel = IndexingFunnelDTO(**funnel_raw)

    # Операційна метрика — «скільки днів від нашої відправки»: саме її
    # питає оператор.
    submitted_lag = _lag(repo.lag_samples(since=since))
    # Метрика порівняння — обидві групи від ОДНОГО нуль-моменту (коли ми
    # самі бачили сторінку поза індексом). Змішати ці два визначення
    # означало б порівнювати різні речі й видавати різницю визначень за
    # ефект роботи.
    submitted_observed = _lag(
        repo.observed_transition_samples(
            since=since, client_ids=client_ids, submitted=True
        )
    )
    organic_lag = _lag(
        repo.observed_transition_samples(
            since=since, client_ids=client_ids, submitted=False
        )
    )

    # Різниця з невідомим не є нулем: доки хоч одна вибірка порожня,
    # відповіді «чи працює» просто немає.
    difference: Decimal | None = None
    if submitted_observed.median_days is not None and organic_lag.median_days is not None:
        difference = organic_lag.median_days - submitted_observed.median_days

    by_reason = [
        IndexingReasonStatDTO(
            reason_code=reason,  # type: ignore[arg-type]
            sent=sent,
            indexed=indexed,
            in_flight=in_flight,
            median_days=median(days),
            conversion=(Decimal(indexed) / Decimal(sent)) if sent else None,
        )
        for reason, sent, indexed, in_flight, days in repo.reason_stats(since=since)
    ]

    retention = []
    for horizon in RETENTION_HORIZONS:
        observed, still, dropped = repo.retention(
            horizon_days=horizon, client_ids=client_ids
        )
        retention.append(
            IndexingRetentionDTO(
                horizon_days=horizon,
                observed=observed,
                still_indexed=still,
                dropped=dropped,
            )
        )

    settings_map = repo.settings_map()
    by_client = []
    for client in clients:
        counts = repo.client_counts(client.id)
        total = counts["urls_total"]
        coverage = (
            (Decimal(counts["indexed"]) / Decimal(total)) if total else None
        )
        queue = counts["candidates"] if total else None
        client_settings = settings_map.get(client.id)
        cycle_holds: bool | None = None
        if client_settings is not None and queue is not None:
            capacity = max(client_settings.daily_quota, 0)
            if capacity > 0:
                days_needed = -(-queue // capacity) if queue else 0
                cycle_holds = days_needed <= client_settings.resubmit_after_days
        by_client.append(
            IndexingClientStatDTO(
                client_id=client.id,
                client_name=names.get(client.id, client.id),
                urls_total=total if total else None,
                coverage_ratio=coverage,
                queue=queue,
                sent_30d=repo.sent_count(
                    client_id=client.id, since=utcnow() - timedelta(days=30)
                ),
                median_days=median(repo.lag_samples(since=since, client_id=client.id)),
                cycle_holds=cycle_holds,
            )
        )

    return IndexingAnalyticsDTO(
        window_days=window_days,
        funnel=funnel,
        lag=submitted_lag,
        control_group=IndexingControlGroupDTO(
            submitted=submitted_observed, organic=organic_lag, difference_days=difference
        ),
        by_reason=by_reason,
        retention=retention,
        by_client=by_client,
    )
