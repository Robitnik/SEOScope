"""Крок 4 конвеєра: черга -> дні календаря (task-100 §2.3).

Три правила, кожне з ціною помилки:

1. **Сьогодні заморожене.** Нічний переплан не чіпає `planned_for = today`
   взагалі. Інакше оператор, який уже почав нести пачку в GSC, побачив би,
   як з-під рук зникають рядки. Умова стоїть у самому `DELETE`
   (`replace_future_plan`), а не тут — щоб її не можна було обійти з
   іншого місця.
2. **Cooldown — фільтр, а не штраф до балу.** URL, відправлений менш ніж
   `resubmit_after_days` тому, не потрапляє в план за жодного балу:
   повторна відправка раніше не дає нічого, а слот витрачає.
3. **Порожній день — нормальний стан.** Заміряно (план §1.5): у семи з
   десяти клієнтів беклог ≤ 3 URL. `queue_exhausted` — це факт, а не
   помилка й не нуль.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app.db.base import utcnow
from app.db.models import IndexingSettings
from app.db.repositories.indexing import IndexingRepository

# Скільки днів наперед матеріалізується (рішення користувача: сьогодні
# заморожене + 7). Далі — прогноз, не рядки: стан індексації міняється
# щодня, і план на 90 днів застарів би за тиждень.
HORIZON_DAYS = 7


@dataclass
class PlanResult:
    client_id: str
    created: int = 0
    removed: int = 0
    days: dict[str, int] = field(default_factory=dict)
    queue_exhausted: bool = False


def plan_client(
    repo: IndexingRepository,
    settings: IndexingSettings,
    *,
    today: date,
    horizon_days: int = HORIZON_DAYS,
    now: datetime | None = None,
) -> PlanResult:
    moment = now or utcnow()
    result = PlanResult(client_id=settings.client_id)

    result.removed = repo.replace_future_plan(settings.client_id, today=today)
    repo.flush()

    quota = max(settings.daily_quota, 0)
    if quota == 0:
        return result

    # Сьогоднішній день уже зайняв свої URL — вони не мають з'явитись ще
    # раз завтра.
    taken = repo.planned_url_ids(settings.client_id, on_or_after=today)
    cooldown_before = moment - timedelta(days=max(settings.resubmit_after_days, 0))

    # Беремо із запасом: частину відсіє cooldown або вже зайнятий план.
    pool = repo.candidates_for_plan(
        settings.client_id, limit=quota * (horizon_days + 2) + len(taken) + 50
    )

    eligible = [
        row
        for row in pool
        if row.id not in taken
        and (row.last_submitted_at is None or row.last_submitted_at < cooldown_before)
        and row.reason_code is not None
    ]

    day_offset = 1
    index = 0
    while day_offset <= horizon_days and index < len(eligible):
        target_day = today + timedelta(days=day_offset)
        # Уже наявні рядки цього дня (`sent`/`quota_blocked`, які
        # `replace_future_plan` свідомо не чіпає) З'ЇДАЮТЬ квоту дня.
        # Без цього переплан дописував би до них ще `daily_quota` нових і
        # тихо видавав удвічі більше роботи, ніж клієнт погодився робити.
        already = len(
            [
                row
                for row in repo.submissions_for_day(target_day, client_ids=[settings.client_id])
                if row.client_id == settings.client_id
            ]
        )
        placed = 0
        while placed + already < quota and index < len(eligible):
            row = eligible[index]
            index += 1
            repo.add_submission(
                client_id=settings.client_id,
                url=row,
                planned_for=target_day,
                reason_code=row.reason_code or "periodic_refresh",
                reason_json=dict(row.reason_json or {}),
            )
            placed += 1
            result.created += 1
        if placed:
            result.days[target_day.isoformat()] = placed
        day_offset += 1

    result.queue_exhausted = index >= len(eligible)
    return result


def ensure_today(
    repo: IndexingRepository,
    settings: IndexingSettings,
    *,
    today: date,
    now: datetime | None = None,
) -> int:
    """Наповнити СЬОГОДНІШНІЙ день, якщо він порожній.

    Потрібне рівно для двох випадків: перший запуск тула і день, у який
    планувальник не відпрацював (рестарт, вимкнений воркер). Наявний
    сьогоднішній план не доповнюється й не переставляється — заморожене
    значить заморожене.
    """
    existing = [
        row
        for row in repo.submissions_for_day(today, client_ids=[settings.client_id])
        if row.client_id == settings.client_id
    ]
    if existing:
        return 0

    moment = now or utcnow()
    quota = max(settings.daily_quota, 0)
    if quota == 0:
        return 0
    cooldown_before = moment - timedelta(days=max(settings.resubmit_after_days, 0))
    # Майбутні `planned` рядки тут НЕ враховуються як зайняті: їх
    # `plan_client` стирає й перебудовує одразу після цього виклику, а
    # `plan_client` своєю чергою читає вже проставлені сьогоднішні. У
    # зворотному припущенні єдиний кандидат, що лишився від учорашнього
    # плану на завтра, блокував би сьогоднішній день — живо саме це й
    # сталось на SWM.
    pool = repo.candidates_for_plan(settings.client_id, limit=quota + 50)

    created = 0
    for row in pool:
        if created >= quota:
            break
        if row.reason_code is None:
            continue
        if row.last_submitted_at is not None and row.last_submitted_at >= cooldown_before:
            continue
        repo.add_submission(
            client_id=settings.client_id,
            url=row,
            planned_for=today,
            reason_code=row.reason_code,
            reason_json=dict(row.reason_json or {}),
        )
        created += 1
    return created


def forecast(
    *, queue_size: int, daily_capacity: int, cycle_days: int
) -> tuple[int | None, bool | None]:
    """«Черга пройдеться за N робочих днів» + чи витримується цикл повтору.

    `None` на порожній черзі — це не «0 днів», це «нема чого проходити»;
    і `None` на нульовій пропускній здатності — не «ніколи», а «невідомо»
    (PLAN §12).
    """
    if daily_capacity <= 0:
        return None, None
    if queue_size <= 0:
        return None, True
    days = -(-queue_size // daily_capacity)  # ceil
    return days, days <= cycle_days
