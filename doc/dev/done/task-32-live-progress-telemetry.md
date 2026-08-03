# task-32 — Живий прогрес: телеметрія адаптерів + плавний, інформативний UI

**Wave:** 8 (після task-30 live progress v2, task-31 lanes)
**Depends on:** task-05 (worker/runner), task-09/10/11 (адаптери), task-22/30/31 (sync-modal) — усі в `doc/dev/done/`
**Owned paths:**
- `app/integrations/progress.py` (новий)
- `app/integrations/reporting_ninja.py`, `app/integrations/ahrefs.py`, `app/integrations/gsc.py`
- `app/jobs/runner.py` (тільки emit-и меж фаз, без зміни логіки збору)
- `frontend/js/sync-modal.js`, `frontend/js/formatters.js`, `frontend/js/sse.js`, `frontend/css/app.css`
- `tests/unit/integrations/test_progress_phases.py`, `tests/unit/jobs/test_phase_events.py`

---

## 1. Проблема (діагноз по коду, не за відчуттям)

Прогрес формально працює, але для оператора він **мертвий**: цифри стоять, тексти брешуть, довгі фази виглядають як зависання. Причини — конкретні:

### 1.1 Жоден метричний адаптер не повідомляє `percent`

`make_emitter` персистить `event.percent` ([app/jobs/events.py:107](app/jobs/events.py#L107)), а `set_progress` ігнорує `None` ([app/db/repositories/cells.py:183](app/db/repositories/cells.py#L183)). Але:

| Адаптер | Що емітить | `percent` | `completed/total` |
|---|---|---|---|
| Reporting Ninja | `fetch_{role}_{dataset}`, `fetch_{role}_gbp`, `fetch_gbp_reviews` ([reporting_ninja.py:327](app/integrations/reporting_ninja.py#L327)) | ніколи | ніколи |
| Ahrefs | `export_issues`, `parse_issues` + міст із AISeoTools ([ahrefs.py:152](app/integrations/ahrefs.py#L152)) | ніколи | ніколи |
| GSC | `fetch_{role}_daily` ([gsc.py:287](app/integrations/gsc.py#L287)) | ніколи | ніколи |

Наслідок: `cell.progress_percent` стоїть на `0.0` від старту до термінального статусу, де його одноразово ставлять у `100.0` ([cells.py:165](app/db/repositories/cells.py#L165)). Саме тому на скріншоті lane «PJC · Ahrefs» і «SDS · Ahrefs» показують `0%` протягом усього збору, а mini-progress у матриці — порожні смуги.

Це також тримає **загальний** відсоток дискретним: `progress_percent` job-а — це середнє по клітинках ([app/api/jobs.py:155](app/api/jobs.py#L155)), тобто зараз він фактично рахує лише завершені клітинки і стрибає на 1/21 ≈ 4.8 % раз на кілька хвилин.

### 1.2 Реальні stage-и не мають лейблів → UI пише «Waiting for status»

`cellDetail()` робить `STAGE_LABELS[cell.current_stage] || "Waiting for status"` ([sync-modal.js:178](frontend/js/sync-modal.js#L178)). У `STAGE_LABELS` ([formatters.js:68](frontend/js/formatters.js#L68)) немає ключів, які реально приходять:
`site_audit`, `verify`, `discover`, `fetch_current`, `fetch_previous`, `fetch_current_traffic_sources`, `fetch_current_landing_pages`, `fetch_previous_*` (усі 6 GA4-датасетів × 2 періоди).

Через це **running-клітинка підписана «Waiting for status»** — це прямо видно на скріншоті в колонках Ahrefs/Search Console. Тобто найгірший клас помилки: UI каже неправду про стан, який він насправді знає.

### 1.3 Гранулярність подій не відповідає тривалості роботи

- **Ahrefs (browser)**: за одну клітинку відбувається `BrowserSession` → `resolve_project` → `overview_kpis` → `export_all_issues_zip` (найдовше, хвилини) → `unpack` → `parse_directory` → парс `affected_*.csv` по кожному issue. Емітяться **дві** події на весь цей ланцюг.
- **Reporting Ninja**: 6 датасетів × 2 періоди + GBP-періоди + reviews ≈ 15 мережевих проходів; подія є перед кожним, але без `total` фронт не може перетворити це на відсоток.

### 1.4 Три фази клітинки невидимі

Runner послідовно викликає `collect()` → `collect_dimensions()` → `collect_datasets()` ([runner.py:432-458](app/jobs/runner.py#L432-L458)). Для RN і Ahrefs `collect_datasets` — найдовша частина, і після повідомлення «snapshot ready» UI мовчить ще довго. Оператор читає це як зависання.

### 1.5 Фронт рухається стрибками

- `<progress>` не має CSS-переходу: значення телепортується.
- Число `%` перемальовується текстом (`textContent`), без інтерполяції ([sync-modal.js:1014](frontend/js/sync-modal.js#L1014)).
- ETA мовчить («estimating time…») до 3 завершених клітинок, а потім ділить медіану на `lanes = кількість running` ([sync-modal.js:883](frontend/js/sync-modal.js#L883)) замість реальної кількості lane-ів, і змішує в одну медіану Ahrefs-клітинку (хвилини) з `skipped`-клітинкою GA4.
- `sse.js` отримує повний payload події (`stage`, `percent`, `message`) і **викидає його**, використовуючи стрім лише як сигнал «щось змінилось» ([sse.js:70-77](frontend/js/sse.js#L70-L77)). Найдетальніші дані, які вже долітають до браузера, не показуються ніде.

### 1.6 Інваріант, який не можна порушити

`frontend/AGENTS.md §9`: «Determinate progress uses a real completed/total model. **Do not animate a fake percentage.**» Тому план не додає «оптимістичних» таймерів: усе, що показується як відсоток, має вимірюване `completed/total`; усе, що виміряти не можна, показується як **чесно індетермінантний** стан, а не як повзучий фейк.

---

## 2. Рішення

### Wave A — бекенд: чесна телеметрія

**A1. `app/integrations/progress.py` (новий) — `PhaseTracker`.**
Клітинка ділиться на фази з фіксованими вагами, відомими наперед; всередині фази крок вимірюваний.

```python
CELL_PHASES = {          # ваги в % шкали клітинки
    Provider.ahrefs:          {"kpi": 25, "datasets": 70, "persist": 5},
    Provider.reporting_ninja: {"kpi": 20, "datasets": 75, "persist": 5},
    Provider.gsc:             {"kpi": 35, "dimensions": 35, "datasets": 25, "persist": 5},
}
```

`tracker.step(stage, completed, total, message)` рахує `percent = base + weight * completed/total` і кличе `emit`. `tracker.indeterminate(stage, message)` емітить подію **без** `percent` (`details={"indeterminate": True}`) — для кроків із невідомою тривалістю (очікування ZIP-експорту). Ваги — не вигадка: вони прив'язані до реального розподілу часу, і задокументовані як припущення, яке уточнюється після першого живого прогону.

**A2. Reporting Ninja — детальний крок.**
У `_collect_datasets` порахувати `total` до циклу: `len(_GA4_DATASETS) * 2 + (2 if gbp_locations else 0) + (1 if gbp_locations else 0)`. Кожен `emit` отримує `completed`/`total`/`percent` і message виду `GA4 traffic sources · current · 7/14`. У `_collect` (KPI) обгорнути `_bridge_emit` трекером, щоб `discover` / `fetch_current` / `fetch_previous` розкладались у 0→20 %.

**A3. Ahrefs — розбити мовчазні хвилини.**
`collect_site_audit_detail_browser` ([ahrefs.py:342](app/integrations/ahrefs.py#L342)) отримує явні стадії навколо кожної операції: `resolve_project` → `export_zip` (indeterminate, з підказкою про тривалість) → `unpack` → `parse_issues` → `parse_affected` з `completed/total` по `parsed.sorted_issues()` (кількість відома до циклу).
**Свідоме обмеження:** `AISeoTools` не чіпаємо — пакет пінований, а зміни в ньому дозволені лише адитивні й з повним прогоном старої test suite (CLAUDE.md §4). Усі нові емісії ставимо в нашому adapter-seam, **навколо** викликів AISeoTools. Нуль ризику для пінованого коміту.

**A4. Runner — межі фаз.**
Перед `collect()`, `collect_dimensions()`, `collect_datasets()` і перед persist — по одній події `phase_*` ([runner.py:432-458](app/jobs/runner.py#L432-L458)). Це закриває «мовчання після snapshot ready» і водночас гарантує, що клітинка ніколи не стоїть без stage.

**Побічний ефект A1–A4, який і є головною метою:** оскільки `progress_percent` job-а — середнє по клітинках, загальний відсоток стає дробовим і рухається безперервно **без жодної зміни в API**.

### Wave B — фронт: плавність

**B1.** Замінити `<progress>` на `<div class="bar"><i class="bar__fill">` з `transform: scaleX()` + `transition: transform .45s cubic-bezier(.22,.61,.36,1)`. `@media (prefers-reduced-motion: reduce)` вимикає перехід. ARIA зберігаємо через `role="progressbar" aria-valuenow/min/max`.
**B2.** Монотонність: у `patchCell` кламп `value = max(prev, next)` для non-terminal клітинки — зміна фази ніколи не смикає бар назад.
**B3.** Інтерполяція числа `%`: rAF-твін попереднє→нове за ~400 мс замість стрибка `textContent`.
**B4.** Індетермінантний режим: коли остання подія клітинки має `details.indeterminate`, бар отримує `.is-indeterminate` (рух смуги, без числа) і підпис «duration unknown» — чесно, не фейковий відсоток.
**B5.** Елапс на кожній running-клітинці (`mm:ss` від `cell.started_at`), один спільний тікер (переюзати `ensureClockTicking`, [sync-modal.js:889](frontend/js/sync-modal.js#L889)) — видно, що процес живий, навіть коли фаза індетермінантна.

### Wave C — фронт: інформативність

**C1.** Повне покриття `STAGE_LABELS` усіма реальними stage-ами + **generic-фолбек** `humanizeStage()`: `fetch_current_traffic_sources` → `Fetching traffic sources (current)`. Прибирає «Waiting for status» для running-клітинки назавжди.
**C2.** Показувати крок у клітинці: `GA4 datasets · 7 / 14`, `Ahrefs issues · 23 / 68` — прямо з `completed/total`.
**C3.** Lane-рядок: `—` замість брехливого `0%`, коли percent невідомий; додати фазу й елапс.
**C4.** ETA чесніший: `lanes` = `laneCount(cells)` (а не кількість running), медіана рахується **окремо по провайдеру** — Ahrefs-клітинка і GSC-клітинка мають різні порядки тривалості, спільна медіана дає прогноз, який промахується в рази.
**C5.** Стрічка подій без змін у бекенді: `sse.js` уже отримує повний JSON події — почати передавати його в `onEvent(payload)` і тримати ring-buffer (останні ~40) у модалці. Клік по клітинці → розкриття з її останніми подіями. Це найдешевший спосіб дати «інформативність» — дані вже в браузері, їх просто викидають.

### Wave D — перевірка

- `tests/unit/integrations/test_progress_phases.py`: ваги фаз сумуються в 100; percent монотонно зростає; жодна подія не несе секретів; `indeterminate` не має `percent`.
- `tests/unit/jobs/test_phase_events.py`: клітинка ніколи не лишається без `current_stage`; послідовність фаз для RN/Ahrefs/GSC.
- Фронт: fixture-прогон (`frontend/fixtures`) з підробленим job-ом на всіх станах + чекліст `AGENTS.md §15–16`, перевірка `prefers-reduced-motion`.

---

## 3. Ризики і межі

| Ризик | Рішення |
|---|---|
| Ваги фаз — оцінка, не вимір | Задокументовані як припущення; percent усередині фази — реальний `completed/total`. Уточнити після першого живого прогону. |
| Більше подій → більше рядків `job_events` і записів у SQLite | Кожна подія — коротка окрема транзакція ([events.py:95](app/jobs/events.py#L95)); throttle у трекері: не частіше ніж 1 подія / 750 мс на фазу, крім меж фаз. |
| SSE-шторм на фронті | `sse.js` уже коалесить у 250 мс ([sse.js:10](frontend/js/sse.js#L10)); ring-buffer подій не тригерить re-render матриці. |
| `runner.py` — чужа historically зона (task-05) | Зміни лише адитивні emit-и, без зміни control flow; оголосити в Completion report. |
| AISeoTools | Не чіпаємо взагалі. |

## 4. Definition of Done

1. Running-клітинка **ніколи** не підписана «Waiting for status» — завжди реальна стадія.
2. Ahrefs і Reporting Ninja показують рухомий, вимірюваний відсоток; жодна клітинка не стоїть на `0%` довше однієї фази.
3. Загальний відсоток рухається безперервно, а не стрибками по 1/N.
4. Бари анімовані; `prefers-reduced-motion` шанується; фокус і скрол не губляться (регресія task-30 не повертається).
5. Індетермінантні кроки марковані як такі — ніде немає анімованого фейкового відсотка (`AGENTS.md §9`).
6. ETA рахується по lane-ах і по-провайдерній медіані.
7. `ruff` + `mypy` + `pytest` зелені **в Docker**; нові unit-тести покривають ваги, монотонність і редакцію.

---

## ✅ Completion report

- **Дата:** 2026-08-03
- **Гілка / комміт:** `task-30-live-data-refresh` @ (див. комміт нижче)
- **Статус:** done

### Що зроблено

Прогрес збору став вимірюваним, плавним і не бреше про стан.

- **Клітинка більше ніколи не стоїть на `0%`.** Усі три метричні адаптери тепер шлють реальний `percent`/`completed`/`total`. Reporting Ninja рахує 12–15 кроків (6 GA4-датасетів × 2 періоди + GBP + reviews) з `total`, відомим до циклу; Ahrefs розбив мовчазний браузерний ланцюг на `open_session → resolve_project → export_issues → unpack_export → parse_issues → parse_affected` з лічильником по issue; GSC переведений із власної ad-hoc шкали «7..9 з 9» на ту саму фазову модель.
- **Running-клітинка більше не підписана «Waiting for status».** `humanizeStage()` перетворює будь-яку стадію (зокрема побудовані в рантаймі `fetch_current_ga4_landing_pages`) на читабельний текст, а найбагатше джерело правди — повідомлення адаптера («GA4 landing pages · current · 10 / 14») — тепер показується першим.
- **Смуги анімовані**, з чесним індетермінантним станом для кроків, тривалість яких виміряти неможливо (очікування ZIP-експорту Ahrefs), і з елапсом на кожній running-клітинці.
- **ETA рахується по реальних lane-ах і по-провайдерній медіані** замість спільної медіани й «скільки зараз біжить».
- **Activity log** — стрічка справжніх SSE-подій, згорнута за замовчуванням.

### Як зроблено (ключові рішення)

- **`app/integrations/progress.py` — `PhaseTracker`.** Runner викликає `collect()` → `collect_dimensions()` → `collect_datasets()` окремо, з тим самим `emit`, але без спільного стану. Замість зміни контракту `ProviderAdapter` (task-02, read-only) шкала клітинки поділена на фази зі **статичними вагами**: кожен виклик створює власний трекер і оголошує свою фазу, а базове зміщення виводиться з таблиці. Монотонність між викликами гарантує сама розкладка.
- **`AISeoTools` не змінювався.** Пакет пінований; його події несуть `stage`, але не `percent`. `PhaseTracker.bridge(stages)` мапить позицію стадії в очікуваній послідовності на вимірюваний крок, а незнайома стадія проходить як індетермінантна — нових вимог до зовнішнього пакета нуль.
- **Чесність замість красивості (AGENTS.md §9).** Найдовший крок Ahrefs — очікування, поки сервіс збере експорт — принципово невимірюваний. Він показується як індетермінантний (біжуча смуга + елапс + «duration unknown»), а не повзучим фейковим відсотком. Ціною є смуга, що стоїть на місці протягом найдовшої частини; альтернатива була б брехнею.
- **CSP.** Перша версія смуг керувала заповненням через `transform` в inline-стилі — і **візуальний QA спіймав, що це не працює в проді**: додаток шле `Content-Security-Policy: default-src 'self'` без `style-src 'unsafe-inline'` (`app/core/security.py`), тож і `style=""`, і `el.style.transform = …` блокуються, а смуга лишається порожньою. Значення тепер їде атрибутом `data-p`, який матчиться статичною драбиною зі 101 правила в `app.css`. Саме тому попередня реалізація й трималася на `<progress>`: його `value` — атрибут, не стиль.
- **Activity log — рівня запуску, не клітинки.** Рядок `job_events` несе `provider`, але не `client_id`, а з task-31 провайдер веде кілька клієнтів одночасно, тож прив'язка рядка до конкретної клітинки була б здогадкою. Це свідоме відхилення від початкового плану («клік по клітинці → її події»).

**Головні файли:** `app/integrations/progress.py` — фазовий трекер; `app/integrations/{reporting_ninja,ahrefs,gsc}.py` — телеметрія адаптерів; `app/jobs/runner.py` — фаза `persist`; `frontend/js/sync-modal.js` — смуги, елапс, ETA, стрічка; `frontend/js/formatters.js` — `humanizeStage()` + словник стадій; `frontend/js/sse.js` — `onEvent`; `frontend/css/app.css` — `.bar` + CSP-безпечна драбина.

### Контрактні зміни (Contract changes)

Публічних змін немає: `ProgressEvent`, схема БД і всі HTTP-контракти лишились як були — заповнюються лише поля, які й раніше існували (`percent`, `completed`, `total`, `details`). Внутрішньо: `watchJob()` приймає необов'язковий `onEvent`.

### Як перевірити (verification)

```bash
docker compose up -d --build
docker compose exec api poetry run pytest tests/unit/integrations/test_progress_phases.py -q
docker compose exec api poetry run ruff check app tests && docker compose exec api poetry run mypy app
```

В UI (`?fixture=dashboard-all-states` → «Update metrics» → «Start collection»):
- у перші ~2 с клітинки Ahrefs показують «Locating the project», смугу-свіп і `duration unknown` + елапс;
- далі — «Ahrefs issues · N / 68» і «GA4 landing pages · current · N / 14» з рухомою смугою;
- жодна running-клітинка не підписана «Waiting for status»; lane без відомого відсотка показує `—`, не `0%`;
- у консолі **нуль** CSP-помилок (це і є регресійна перевірка драбини `data-p`).

### Тести

- `tests/unit/integrations/test_progress_phases.py` (15 тестів): суміжність і повнота фазових ваг, монотонність відсотка, відсутність `percent` в індетермінантних подіях, throttle без втрати останнього кроку, bridge для зовнішніх стадій, стійкість до зламаного `emit` і до невалідного `level`.
- `tests/unit/integrations/test_gsc.py` — оновлений під фазову шкалу замість «7..9 з 9».
- Візуальний QA — Playwright у контейнері проти фікстур і проти реального SSE-стріму (стрічка подій).

### Follow-ups / борг

- **Ваги фаз — оцінка, не вимір.** Уточнити після кількох живих прогонів (`CELL_PHASE_WEIGHTS`).
- `INDETERMINATE_STAGES` у `sync-modal.js` дублює список стадій, які адаптери шлють через `indeterminate()`. Чисте рішення — віддавати `details` у DTO клітинки; це зміна контракту (task-02/06) і тому не робилась тут.
- Довге очікування Ahrefs-експорту лишається без прогресу за визначенням. Якщо AISeoTools колись віддасть проміжний сигнал (розмір ZIP, стан черги), фаза стане вимірюваною без змін на фронті.
- `frontend/js/{alt-text-tool,redirect-tool}.js` містять inline-стилі, які CSP блокує так само — **існуючий дефект поза цією таскою**, не чіпав.
