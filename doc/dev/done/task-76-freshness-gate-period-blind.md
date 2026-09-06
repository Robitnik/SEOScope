# task-76 — Гейт свіжості період-сліпий: custom-refresh глушить штатний тижневий збір

- **Пакет:** `_is_fresh_enough` порівнює лише **дату** снапшота з кінцем періоду, ігноруючи його **діапазон**. Тому будь-який custom-прогон (rolling-window refresh, task-30) робить наступний тижневий `collect_missing` порожнім: клітинка йде в `skipped`, тиждень не збирається ніколи, а дашборд — який шукає **точний** збіг `period_start`/`period_end` — показує `unavailable` → «Data not received». Живий інцидент: 8 із 15 клієнтів простояли так з ~18 серпня до 6 вересня 2026, при повністю справних креденшелах і «успішних» батчах.
- **Хвиля:** W — інфраструктура даних.
- **Depends on:** нічого не блокує (правка локальна в `app/jobs/runner.py`). Дотична до **68** (той самий `_build_provider_summary`), але не конфліктує: 68 чіпає продуктивність, 76 — семантику.
  - **Міграція НЕ потрібна.** Ні таблиць, ні колонок. `SnapshotRepository.latest_success` **уже** приймає `period_start`/`period_end` ([snapshots.py:57-90](../../../app/db/repositories/snapshots.py#L57-L90)) — їх просто ніхто не передає з воркера.
- **Owned paths:** `app/jobs/runner.py`, `app/jobs/events.py`, `app/api/dashboard.py` (точковий дотик у §3), `tests/unit/jobs/test_freshness_gate.py` (новий), `tests/unit/jobs/test_runner*.py`.
- **Нові залежності:** нуль.
- **PLAN.md:** §9.1 (ізоляція клітинок), §9.2 (`not_configured` ≠ нуль ≠ збій), §12 (snapshot- vs range-метрики — саме та відмінність, яку гейт зараз не бачить).

---

## §0. Що виміряно на бойовій базі (2026-09-06)

Дашборд просить закритий тиждень `2026-08-24 → 2026-08-30`. Рядки `metric_values` за цей тиждень існували рівно у чотирьох клієнтів:

| клієнт | звідки взявся тиждень |
|---|---|
| HWELL | окремий ручний `week` + `force_refresh`, 4 вер 06:07 |
| GOREPORT | окремий ручний `week` + `force_refresh`, 4 вер 09:37 |
| ALPHA | окремий ручний `week` + `force_refresh`, 1 вер 16:27 |
| LAXTON | штатний `collect_missing` **проліз**, бо попередній снапшот був від 17 серпня |

Решта восьми (3XE, EFFYS, GRP, NYCHA, SAPHNY, SDS, SHB, SWM) — порожньо. Послідовність для кожного з них ідентична:

```
5 вер 08:56  period_kind=custom  2026-08-22 → 2026-09-04  mode=force_refresh
             → снапшот записано, data_through = 2026-09-03
5 вер 09:13  period_kind=week    closed (= 2026-08-24 → 2026-08-30)  mode=collect_missing
             → _is_fresh_enough(latest, date(2026,8,30))
               reference = 2026-09-03  >=  2026-08-30  → True
               → CellStatus.skipped, error_code=NULL
6 вер 14:13  те саме → skipped
```

Тиждень не зібрано жодного разу. Агрегати по `sync_job_providers` за весь час: `gsc` — 76 `skipped`, `reporting_ninja` — 152 `skipped`. Це не рідкісний край, це основний режим.

**Чому саме з 18 серпня.** Rolling-window пресет ([sync-modal.js:575-596](../../../frontend/js/sync-modal.js#L575-L596)) будує вікно, що закінчується **вчора**, і завжди з `force_refresh` — інакше він сам би себе заблокував (це чесно описано в коментарі там). Його `data_through` тому майже завжди свіжіший за кінець будь-якого закритого періоду. Кожен запуск пресету глушить наступний штатний збір. Прогони 18 сер, 1 вер, 2 вер, 5 вер — у базі всі.

---

## §1. Корінь: гейт не питає, **за який період** снапшот

[runner.py:120-122](../../../app/jobs/runner.py#L120-L122):

```python
def _is_fresh_enough(snapshot: Any, current_end: date) -> bool:
    reference: date = snapshot.data_through or snapshot.observed_at.date()
    return reference >= current_end
```

І сам відбір кандидата — [runner.py:398-422](../../../app/jobs/runner.py#L398-L422) — теж період-сліпий:

```python
latest = SnapshotRepository(session).latest_success(
    handle.client_id,
    handle.provider,
    source_mode=SourceMode.api if handle.provider == Provider.gsc else None,
)
```

Тобто «найсвіжіший успішний снапшот взагалі», без огляду на діапазон.

Дашборд же ([dashboard.py:270-278](../../../app/api/dashboard.py#L270-L278)) для range-провайдерів вимагає **точний** збіг:

```python
current_snapshot = _latest_gsc_aware_success(
    ..., period_start=periods.current.start if has_range_metrics else None,
         period_end=periods.current.end if has_range_metrics else None,
)
```

Дві частини системи розходяться в означенні «дані за період уже є». Воркер каже «є», дашборд каже «немає». Обидва праві у своїх термінах — і саме тому ніхто не помітив три тижні.

### Фікс

Передати період у відбір кандидата — рівно так, як це робить дашборд, і **лише** для range-провайдерів (`comparison_kind == "range"`). Для snapshot-провайдерів (Ahrefs, Semrush) поточна поведінка правильна за означенням: там «поточне значення» — це останнє відоме показання, а не показання за період (PLAN §12), і період-скоуп зламав би їх.

```python
specs = metrics_for(handle.provider)
has_range_metrics = any(spec.comparison_kind == "range" for spec in specs)
latest = SnapshotRepository(session).latest_success(
    handle.client_id,
    handle.provider,
    source_mode=SourceMode.api if handle.provider == Provider.gsc else None,
    period_start=periods.current.start if has_range_metrics else None,
    period_end=periods.current.end if has_range_metrics else None,
)
```

`metrics_for` уже поруч — [catalog.py:185](../../../app/metrics/catalog.py#L185); `CATALOG` імпортовано в runner на [рядку 36](../../../app/jobs/runner.py#L36).

### Наслідок, який треба свідомо прийняти

Після фікса `_is_fresh_enough` буде застосовуватись до снапшота, чий діапазон **уже** збігся з періодом. Для GSC із його лагом 2–3 дні свіжозакритий тиждень може мати `data_through` < кінця тижня — тоді клітинка перезбиратиметься при кожному прогоні, доки GSC не дотягне. Це **бажана** поведінка (дозбір неповного періоду), і вона сама припиняється. Але це реальний приріст викликів API — порахувати в §5 перед злиттям, не після.

---

## §2. `skipped` невидимий для оператора

Клітинка завершується `skipped` з `error_code=NULL`, `error_message=NULL`. У журнал подій вона лягає під стадією `preflight` ([events.py:125](../../../app/jobs/events.py#L125)) — тобто виглядає як звичайний технічний крок. Батч рапортує `succeeded`. На картці клієнта — «Data not received» ([formatters.js:39](../../../frontend/js/formatters.js#L39)) без жодного натяку, що збір свідомо нічого не робив.

Оператор бачить «збір пройшов успішно» і «даних немає» одночасно, і між цими двома фактами немає видимого зв'язку. Саме через це інцидент жив три тижні.

**Фікс:** записувати причину пропуску в `error_message` (це не помилка — поле вже використовується як людиночитний коментар), у форматі, що називає діапазон-винуватець:

```
skipped: snapshot 2026-08-22..2026-09-04 (data_through 2026-09-03) already covers this cell
```

Після §1 такий пропуск стане рідкісним і чесним («цей самий тиждень уже зібрано»), але лишиться діагностованим.

---

## §3. Мертві колонки `client_integrations.last_success_at` / `last_attempt_at`

Обидві оголошені ([models.py:124-125](../../../app/db/models.py#L124-L125)) і читаються у шести місцях — [dashboard.py:315](../../../app/api/dashboard.py#L315), [:328](../../../app/api/dashboard.py#L328), [:340](../../../app/api/dashboard.py#L340), [clients.py:140-141](../../../app/api/clients.py#L140-L141), [discovery.py:51-52](../../../app/api/discovery.py#L51-L52), [providers.py:88](../../../app/api/providers.py#L88) — але **не пишуться ніде**. На бойовій базі всі 27 рядків мають `NULL` в обох.

`jobs.py:279` пише `last_success_at`, але на `SyncJobProvider` ([models.py:209](../../../app/db/models.py#L209)) — інша таблиця, інша колонка з тим самим іменем.

Дашборд це обходить (бере час із клітинки), а от `/providers` і `/discovery` віддають `null` як «ніколи не перевірялось» — брехня.

**Рішення — одне з двох, обрати свідомо, не обидва:**
- **(а)** писати їх у тій самій транзакції, де воркер закриває клітинку. Мінус: ще один UPDATE на клітинку.
- **(б)** прибрати колонки зі схеми й вивести читачів на `SyncJobProvider` (єдине джерело істини вже там).

Схиляюсь до **(б)**: колонки дублюють те, що клітинки й так знають точніше, а після переїзду на Postgres (task-75) економія на записі вже не аргумент за (а). Але (б) — це міграція, тобто запит до **task-03**, а не правка `migrations/` тут.

---

## §4. Окремо: PJC і 19CLV — інша причина, не цей баг

- **PJC** — `unavailable`, останній збір 3 серпня.
- **19CLV** — `stale`, останній збір 11 серпня.

Гейт тут ні до чого: їхні снапшоти старіші за кінець періоду, `_is_fresh_enough` повернув би `False`. Їх просто **ніхто не запускав**. Планувальника збору метрик у проєкті немає взагалі — [app/cloud/scheduler.py](../../../app/cloud/scheduler.py) веде лише бекап, звіти й архів; збір метрик існує тільки як ручна дія в UI.

Це не входить у скоуп 76, але має стати окремою таскою: **штатний збір без людини**. Поки його немає, будь-який фікс гейту лікує тільки тих клієнтів, кого хтось згадав натиснути.

---

## §5. Definition of Done / Gate

1. `tests/unit/jobs/test_freshness_gate.py` (новий) покриває:
   - custom-снапшот з `data_through` > кінця тижня **не** блокує тижневу клітинку range-провайдера (регресія цього інциденту, з реальними датами з §0);
   - снапшот із точним збігом тижня **блокує** (дедуп не зламано);
   - snapshot-провайдер (Ahrefs) поводиться як раніше — період не звужує відбір;
   - неповний період (`data_through` < кінця) перезбирається.
2. Уся наявна `tests/unit/jobs` зелена — гейт має багато чинних тестів на дедуп, жоден не має впасти.
3. `ruff check .` + `mypy app` чисті.
4. **Живий доказ на бойовій базі:** прогнати rolling-window пресет на одному клієнті, одразу після нього — `week` + `collect_missing`, і переконатись, що другий **зібрав** тиждень (до фікса — `skipped`). Далі `GET /api/v1/dashboard` показує `complete`, не `unavailable`.
5. §5 з §1 порахований: у Completion report назвати, на скільки виросла кількість викликів GSC/RN за типовий тиждень.
6. Рядок у [INDEX.md](INDEX.md) додати **після** того, як `git status` на ньому буде чистий — зараз файл тримає сусідня сесія (task-74).

---

## §6. Разова ліквідація наслідків — вже зроблено

6 вересня 2026 запущено `POST /api/v1/sync-jobs` — `week` / `closed` / `force_refresh`, провайдери `gsc` + `reporting_ninja`, batch `045a997f-c5dc-45a1-9e07-e4b4d7b7c5f8`, вісім клієнтів з §0. Результат: **16/16 клітинок `succeeded`**, дашборд віддає `complete` для всіх восьми (3XE / `reporting_ninja` — `truncated`, тобто дані отримано частково; це стан якості даних, не збою).

Це прибрало симптом, а не причину: наступний rolling-window refresh відтворить його рівно так само.

---

## ✅ Completion report

- **Дата:** 2026-09-06
- **Гілка / комміт:** `feat/task-61-url-inspector` @ `fb357df` (окремої feature-гілки під цю таску не заводилось — працювали поверх поточної гілки репозиторію)
- **Статус:** done-with-followups

### Що зроблено

§1 і §2 реалізовано повністю. §3 (мертві колонки `last_success_at`/`last_attempt_at`) і §4 (немає штатного планувальника збору метрик) свідомо залишені follow-up — обидва вимагають рішень поза скоупом цієї таски (§3 — міграція через **task-03**, §4 — окрема таска на автозбір). Разова ліквідація наслідків інциденту (§6) виконана раніше й лишається чинною.

### Як зроблено (ключові рішення)

- **[app/jobs/runner.py](../../../app/jobs/runner.py)** — `_run_cell_unguarded`: перед пошуком «останнього успішного снапшота» тепер рахується `has_range_metrics = any(spec.comparison_kind == "range" for spec in metrics_for(handle.provider))`. Для range-провайдерів (GSC, Reporting Ninja) у `SnapshotRepository.latest_success` передаються `period_start=periods.current.start`, `period_end=periods.current.end` — ті самі аргументи, які дашборд уже використовує в `_latest_gsc_aware_success`. Snapshot-провайдери (Ahrefs/Semrush) свідомо лишились без цього скоупу: PLAN §12 визначає їхнє «поточне» значення як останнє відоме показання, не показання за період.
- Новий `_skip_reason()` формує людиночитний `error_message` для `CellStatus.skipped` — раніше `error_message=None` робив пропуск невидимим і в журналі подій, і на батч-рівні. Повідомлення називає обсерв-дату й `data_through` знайденого снапшота та запитаний період.
- `_terminate` вже приймав `error_message` — контракт не змінювався. `app/jobs/events.py` не зачіпався: повідомлення прокидається наявним шляхом.

### Контрактні зміни (Contract changes)

Немає. Публічний контракт (`app/schemas/*`, REST API) не зачеплено — зміна лежить повністю у внутрішній логіці воркера.

### Як перевірити (verification)

```bash
docker compose exec api poetry run pytest tests/unit/jobs/test_freshness_gate.py -q
docker compose exec api poetry run ruff check app/jobs/runner.py
docker compose exec api poetry run mypy app/jobs/runner.py
```

Регресійний доказ (виконано в цій сесії): тимчасовий `git stash` на `app/jobs/runner.py` (відкат лише фікса, тест лишається) → `test_wide_custom_refresh_does_not_block_the_next_closed_week` і `test_exact_same_week_rerun_is_still_deduped` падають на непочиненому коді (перший — `skipped` замість `succeeded`, точне відтворення інциденту; другий — `error_message is None`). `git stash pop` повертає фікс → усі 4 тести зелені.

⚠️ **`tests/unit/jobs` повністю** зараз нестабільний незалежно від цієї зміни: паралельний прогін файлів дає `psycopg.errors.DeadlockDetected` (`TRUNCATE ... RESTART IDENTITY CASCADE` проти фонового потоку з `test_timeout_does_not_block_forever_and_marks_failed`, який навмисно імітує зависання адаптера й не встигає завершитись до truncate наступного тесту). Відтворено й **без** цієї зміни (3 базові прогони, 5–11 випадкових падінь щоразу, завжди включно з тим самим timeout-тестом) — це передіснуюча вада тестового харнесу після переходу на Postgres (task-75 §9), не регресія цієї таски. Вартий окремої таски по `tests/unit/jobs/conftest.py`.

### Тести

Новий [tests/unit/jobs/test_freshness_gate.py](../../../tests/unit/jobs/test_freshness_gate.py), 4 тести, усі проти реальної тестової Postgres-бази (`yovth_test`, `tests/harness/db.py`), не проти моків:

1. `test_wide_custom_refresh_does_not_block_the_next_closed_week` — точний інцидент: 14-денний custom `force_refresh` на GSC, потім closed-week `collect_missing` — має зібрати тиждень (`succeeded`), а не пропустити.
2. `test_exact_same_week_rerun_is_still_deduped` — дедуп не зламано: повторний запуск **того самого** тижня все ще `skipped`, і тепер із заповненим `error_message`, що містить `"already covers"`.
3. `test_snapshot_provider_freshness_stays_period_blind` — Ahrefs (snapshot-метрики) навмисно лишається period-сліпим: вужчий пізніший запит усе одно бачить ширший custom-снапшот як свіжий і пропускає.
4. `test_incomplete_period_still_recollects` — снапшот з точним збігом періоду, але `data_through` коротшим за кінець періоду (типовий лаг GSC), усе одно перезбирається, а не позначається як готовий.

### Follow-ups / борг

- **§3** — `client_integrations.last_success_at`/`last_attempt_at` читаються у 6 місцях (`dashboard.py`, `clients.py`, `discovery.py`, `providers.py`) і не пишуться ніде; на бойовій базі всі рядки `NULL`. Рішення (прибрати колонки, вивести читачів на `SyncJobProvider`) вимагає міграції — запит до **task-03**.
- **§4** — планувальника штатного збору метрик у проєкті немає; PJC і 19CLV зламані саме через це, не через баг цієї таски. Потребує окремої таски «штатний збір без людини».
- Приріст викликів GSC/Reporting Ninja API від точнішого гейту (неповний період тепер коректно перезбирається щоразу, доки GSC не дотягне `data_through`) не заміряно окремо на бойовому трафіку — вартий спостереження на наступному тижневому циклі.
- `tests/unit/jobs` — передіснуюча нестабільність харнесу під Postgres (описана вище в «Як перевірити»), не усунена в межах цієї таски.
