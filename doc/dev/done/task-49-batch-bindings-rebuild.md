# task-49 — Батч: прив'язки видимі до запуску, кеш керований, «перезібрати» існує

- **Пакет:** масовий прогін перестає стартувати наперед провальним. Оператор бачить **таблицю прив'язок** усіх клієнтів у формі генерації і прямо там або **під'єднує** клієнта, або **виключає** його зі звіту. Плюс: кеш PDF стає видимим і **керованим** — з'являється «перезібрати» (прогон, клієнт, окремий період), якого зараз немає ніде, а `Retry this client` перестає бути пасткою.
- **Хвиля:** N — Reports (round 5, після 47).
- **Depends on:** **47** ✅ (батчі, матриця, клієнтський PDF), **45** ✅ (`periods_json`, guard прив'язки). Не конфліктує з **48** (та володіє `pdf_parse.py`/`verify.py`/`ninja_browser.py`-дискаверингом; тут — API/UI батчу і кеш).
- **PLAN.md:** §9.2 (ізоляція збоїв — уточнюємо межу між ізоляцією й плануванням), §10 (REST), §12, §13.
- **frontend/AGENTS.md:** **обов'язковий** — уся таска про форму й матрицю батчу.
- **Міграція:** не потрібна.

---

## Мета

Живий прогін 2026-08-19: **9 із 12 клієнтів упали за 0 секунд** — не через збій збору, а тому що в них не збережена назва звіту Reporting Ninja. Форма про це не попередила, бо **не має звідки знати**. Ще двоє «успішних» клієнтів насправді нічого не збирали — усі 6 періодів прийшли з кешу вчорашнього дня, і в матриці це виглядає як звичайний `succeeded`. Кнопки «перезібрати» немає ні для прогону, ні для клієнта, ні для періоду.

Закриваємо: **до натискання Start видно, хто готовий, хто ні і чому; недоготового клієнта можна під'єднати або виключити не виходячи з форми; після прогону видно, що прийшло з кешу, і будь-що можна перезібрати заново.**

---

## 1. Дослідження (жива база, батч `c9c6665a`, 2026-08-19)

### 1.1 Що насправді сталося

```
BATCH c9c6665a  monthly · pdf · force_refresh=false · 12 клієнтів
queued 06:57:22 → finished 07:03:21 (6 хв) → status=partial (3 done / 9 failed)
```

| Клієнт | Статус | Періоди | Причина |
|---|---|---|---|
| 3XE | succeeded | 6/6 | **усі 6 із кешу** |
| GRP | succeeded | 6/6 | **усі 6 із кешу** |
| EFFYS | partial | 4/6 | 2 періоди впали на зборі |
| ALPHA, GOREPORT, HWELL, LAXTON, NYCHA, SAPHNY, SDS, SHB, SWM | **failed** | **0/6** | `report_name_missing`, 0 с кожен |

Тобто **6 хвилин прогону** — це майже виключно EFFYS. Дев'ять клієнтів були провальними ще до старту, двоє не зробили жодного запиту в браузер.

### 1.2 🔴 Форма **не могла** попередити: у клієнтському DTO немає поля прив'язки

Чекліст клієнтів вимикає рядок лише за однією ознакою ([reports-wizard.js:91](../../../frontend/js/reports-wizard.js#L91)):

```js
function clientDisabledReason(client) {
  if (!clientNinjaConfigured(client)) return "Reporting Ninja isn't connected";
  return null;
}
```

`clientNinjaConfigured` дивиться на `client.providers` — тобто «чи є інтеграція взагалі». **Збереженої назви звіту (`ninja_report_name`) у клієнтському DTO немає зовсім** — перевірено: поле не зустрічається ні в `app/schemas/api.py`, ні в `api/dashboard.py`, ні в `api/clients.py`. Отже форма фізично не може показати прив'язку, навіть якби хотіла.

Рішення в коді задокументоване так: клієнт без назви звіту «просто впаде своєю дитиною в ізоляції, як і будь-який інший збій (task-42 §3.7.5)». Для **рантайму** це правильно. Для **планування** — ні: це перевірка по полю в БД, вона безкоштовна, і 9 червоних рядків замість попередження — не ізоляція збою, а відсутність превʼю.

> **Межа, яку фіксуємо:** ізоляція збоїв стосується того, що **не можна знати наперед** (мережа, дрейф селектора, таймаут). Те, що **можна прочитати з БД за мілісекунду**, мусить бути показане до запуску. Ці дві речі більше не змішуємо.

### 1.3 Текст помилки — інструкція для розробника, показана оператору

```
No Reporting Ninja report name saved for this client — discover and save one via PUT /clients/{id}/ninja-report
```

Він називає REST-ендпоінт. Оператор не може «зробити PUT»; посилання на місце, де це лагодиться, немає. Це порушує §14 `frontend/AGENTS.md` (копірайт як дія, а не як внутрішній ідентифікатор) — і саме цей рядок оператор бачив дев'ять разів підряд.

### 1.4 `Retry this client` для цих 9 клієнтів — гарантована повторна помилка

[reports-batch.js:216](../../../frontend/js/reports-batch.js#L216):

```js
const result = await createReportRun({
  client_id: clientId, kind: batch.kind, source: batch.source, ...batch.params,
});
```

Три наслідки:

1. **Причина не усунена** — прив'язки досі немає, тож новий прогін упаде так само. Кнопка коштує клік і ще один провальний рядок в історії.
2. `...batch.params` несе `force_refresh: false` — навіть для клієнта, який лагодять саме тому, що дані «не ті».
3. Створюється **окремий** прогін **поза батчем**: батч назавжди лишається `partial`, а історія набирає сиріт. Матриця батчу ніколи не стане зеленою, навіть коли всі клієнти зроблені.

### 1.5 Кеш працює — але невидимий і незворотний з UI

12 подій у батчі: `Reused cached PDF for This Month (downloaded 2026-08-18)` × 6 для 3XE і × 6 для GRP. Тобто два «успішні» клієнти не відкривали браузер узагалі.

- У матриці це `succeeded 6/6` — **нічим не відрізняється** від справжнього збору. Оператор не має способу дізнатись, що звіт побудований на вчорашніх файлах.
- `force_refresh` існує **лише на створенні** ([renderPdfExtras](../../../frontend/js/reports-wizard.js#L520)). Готовий прогін не має жодної дії «перезібрати».
- Ретрай **одного періоду** кеш обходить (`_refetch_pdf` кличе `ninja_browser.download_periods` напряму, минаючи перевірку кешу в `runner.py`) — але ендпоінт віддає `409 period_not_retryable` для будь-чого, крім `failed`. Період із кешу має стан `cached`, тож **шляху перезібрати його з UI не існує**.

Разом: кеш — правильна оптимізація (у task-42 §3.5 вона економить десятки хвилин), але без видимості й без «перезібрати» вона перетворюється на «звіт зібрався сам із чогось, і я не знаю з чого».

---

## 2. Що робимо

### 2.1 Прив'язки як дані: `GET /api/v1/reports/ninja-bindings`

Один запит, усі клієнти:

```json
{"rows": [
  {"client_id": "…", "name": "GRP", "domain": "grillpointnyc.com",
   "connected": true, "ninja_report_name": "GRP-Internal report",
   "state": "ready"},
  {"client_id": "…", "name": "SAPHNY", "connected": true,
   "ninja_report_name": null, "state": "no_report_name"},
  {"client_id": "…", "name": "ALPHA", "connected": false,
   "ninja_report_name": null, "state": "not_connected"}
]}
```

`state`: `ready | no_report_name | not_connected | foreign_binding` (останній — назва не починається з імені клієнта, детектор із task-45/48). Жодного браузера: усе читається з `client_integrations`, тож виклик дешевий і йде разом з рештою даних форми.

Дублювати поле в дашбордний DTO **не** будемо: прив'язка потрібна лише формі звітів, а `ClientDTO` споживають чотири інші сторінки.

### 2.2 Таблиця прив'язок у формі — з діями на місці

Крок вибору клієнтів перестає бути списком чекбоксів і стає таблицею:

| ☑ | Client | Reporting Ninja report | State | Action |
|---|---|---|---|---|
| ☑ | 3XE | `3XE-Internal report` | **Ready** | — |
| ☐ | SAPHNY | — | **No report name** | `Connect` · `Exclude` |
| ☐ | ALPHA | — | **Not connected** | `Open client page` |
| ☑ | SWM | `EFFYS-Internal report` | **Foreign binding** | `Re-bind` |

Правила:

- **`Ready`** — вибирається як зараз.
- **`no_report_name`** — чекбокс **не** заблокований, але при виборі рядок підсвічується попередженням, а кнопка Start показує точну ціну: `2 of 12 clients will fail — connect or exclude them`. Стартувати все одно можна (це рішення оператора), але **свідомо**, а не наосліп.
- **`Connect`** відкриває дискаверинг **тут же**, у рядку: пошук назви в кабінеті, `matches`/`others`, підтвердження для чужої назви (уся механіка task-45 §2.2 уже є — переиспользуємо, не пишемо вдруге). Після збереження рядок сам стає `Ready` без перезавантаження форми.
- **`Exclude`** знімає клієнта з вибору й лишає видимим із позначкою `Excluded` — щоб «12 of 14» не перетворилось на здогадки, кого саме прибрали.
- **`Select all`** обирає **лише `Ready`**. Це і є відповідь на «запустив усі»: масовий вибір більше не втягує наперед провальних.
- **`not_connected`** лишається заблокованим (інтеграції немає — це не лагодиться з форми) із посиланням на сторінку клієнта.
- Для `source=api` колонка звіту не потрібна — таблиця показує лише `connected`, бо назва звіту потрібна тільки PDF-шляху. Один компонент, дві конфігурації.

### 2.3 Текст помилки, з якого видно дію

Замість `…via PUT /clients/{id}/ninja-report`:

> **Reporting Ninja report not set for SAPHNY.** Open the report wizard and use *Connect* next to this client, or exclude it from the batch.

Плюс: у рядку матриці батчу для цієї причини — кнопка `Connect Reporting Ninja` замість `Retry this client` (§2.4).

### 2.4 `Retry this client` перестає бути пасткою

1. **Ретрай лишається в батчі.** `POST /reports/batches/{id}/clients/{client_id}/retry` створює новий дитячий прогін **з тим самим `batch_id`** і замінює попередній у матриці; лічильники батчу перераховуються, і батч може дійти до `succeeded`. Сиріт більше не з'являється.
2. **Причина, яку ретрай не полагодить, ретраєм не пропонується.** `error_code=report_name_missing` → кнопка `Connect Reporting Ninja` (веде в діалог прив'язки), а не `Retry`.
3. **Ретрай успадковує `force_refresh` явно:** у кнопки з'являється меню з двома діями — `Retry (use cached PDFs)` і `Rebuild (re-download everything)`. Мовчазного успадкування `force_refresh: false` більше немає.

### 2.5 «Перезібрати» на трьох рівнях

| Рівень | Дія | Семантика |
|---|---|---|
| Період | `Re-download this period` — тепер доступна для `cached`/`done`, не лише `failed` | `_refetch_pdf` уже обходить кеш; змінюється лише гейт `period_not_retryable` |
| Прогін | `POST /runs/{id}/rebuild` | новий прогін із тими самими параметрами + `force_refresh=true`; лінкується до того самого батчу, якщо був |
| Батч | `Rebuild failed clients` / `Rebuild all` | те саме для вибірки клієнтів, з підтвердженням і оцінкою часу (для 12 клієнтів PDF-шляху це ~70 хв — цифра мусить бути перед очима **до** підтвердження) |

Кнопка `Rebuild` — не `primary`: дефолт лишається кешованим, бо він дешевий і майже завжди правильний.

### 2.6 Кеш стає видимим

- У матриці батчу поруч зі станом клієнта — склад прогону: `6 cached` / `4 downloaded · 2 cached`. Клієнт, чиї **всі** періоди з кешу, отримує чіп `From cache` (нейтральний тон, не помилка).
- На сторінці прогону — рядок під шапкою: `Data: 6 of 6 periods reused from PDFs downloaded Aug 18`. Дата — найстаріша з перевикористаних, бо саме вона визначає свіжість звіту.
- Клієнтський документ (task-47) у футері отримує `Source PDFs downloaded <date>`, коли хоч один період із кешу. Клієнт має бачити, на чому побудований його звіт; це та сама вимога правди даних, що й `None ≠ 0`.

### 2.7 Чого свідомо **не** робимо

- Не вимикаємо кеш і не робимо `force_refresh` дефолтом — 3XE і GRP отримали свої звіти за секунди, і це правильна поведінка.
- Не автопідв'язуємо назви звітів здогадом — саме це створило чужу прив'язку SWM (task-48 §1.6).
- Не показуємо прив'язки в дашбордному `ClientDTO` — не тягнемо звітну специфіку в спільний контракт.

---

## 3. Owned paths

```
app/api/reports.py              (ninja-bindings, batch client retry, run rebuild, period gate)
app/schemas/reports.py          (NinjaBindingsDTO, RebuildRequest, cache-склад у DTO прогону/батчу)
app/reports/period_retry.py     (дозволити re-fetch для cached/done)
app/reports/batch.py            (ретрай усередині батчу + перерахунок лічильників)
app/reports/runner.py           (склад кешу в результат прогону)
app/reports/document.py         (футер: дата вихідних PDF, коли є кеш)
frontend/js/reports-wizard.js   (таблиця прив'язок + inline Connect/Exclude)
frontend/js/reports-batch.js    (склад кешу, Connect замість Retry, Rebuild)
frontend/js/reports-run.js      (рядок свіжості, Rebuild, re-download періоду)
frontend/js/api.js              (нові виклики, ключі за наміром)
frontend/css/reports.css
tests/unit/reports/**, tests/unit/api/test_reports_api.py, tests/e2e/frontend/reports.cjs
```

Перетин із **task-48**: та володіє `ninja_browser.py` (пошуковий дискаверинг), `pdf_parse.py`, `verify.py`. Тут ці файли **не** редагуються; таблиця прив'язок споживає `state`-детектор, який task-48 уже вводить, і до її завершення читає префікс сама (одна функція, узгоджена в обох планах).

---

## 4. Тести

- `api/ninja-bindings` — усі чотири `state` на живій формі даних (14 клієнтів: 1 `ready`, N `no_report_name`, 3 `not_connected`); жодного браузерного виклику (тест на кількість зовнішніх звернень = 0);
- `batch retry` — новий дитячий прогін лишається в батчі, лічильники перераховуються, батч може дійти до `succeeded`; сирота не створюється;
- `rebuild` — новий прогін несе `force_refresh=true`; батчевий `Rebuild all` не чіпає клієнтів поза вибіркою;
- `period_retry` — `cached` період тепер перезбирається; `queued`/`running` — досі `409`;
- `runner` — склад кешу (`cached_periods`, `oldest_cached_at`) у результаті; прогін без кешу не отримує чіпа;
- `document` — футер несе дату вихідних PDF лише коли кеш використано;
- **e2e:** `Select all` обирає лише `Ready`; вибір клієнта без назви звіту дає видиме попередження й точну ціну на Start; inline `Connect` доводить рядок до `Ready` без перезавантаження; `Exclude` лишає рядок видимим; у матриці `From cache` видно; `Rebuild` питає підтвердження з оцінкою часу.
- **Живий гейт:** повторити батч на 12 клієнтах — 0 клієнтів зі станом `no_report_name` у вибірці; клієнти з кешу позначені; `Rebuild failed clients` перезбирає лише їх.

---

## 5. Definition of Done / Gate

- [ ] Форма показує таблицю: клієнт → назва звіту в кабінеті → стан, для всіх клієнтів портфеля.
- [ ] `Select all` не обирає клієнтів, які впадуть; вибір такого клієнта вручну дає видиме попередження й точне число на кнопці Start.
- [ ] Клієнта можна під'єднати **з форми** (inline-дискаверинг) і виключити зі звіту, не втрачаючи його з очей.
- [ ] Жодне повідомлення в UI не містить шляху REST-ендпоінта; для `report_name_missing` пропонується `Connect`, а не `Retry`.
- [ ] Ретрай клієнта лишається в батчі; батч після ретраїв може стати `succeeded`; сиріт немає.
- [ ] Є `Rebuild` на трьох рівнях (період / прогін / батч), і `force_refresh` більше не успадковується мовчки.
- [ ] Період зі стану `cached` можна перезавантажити з UI.
- [ ] Прогін і матриця показують склад кешу; документ клієнта несе дату вихідних PDF, коли їх перевикористано.
- [ ] `ruff check .`, `mypy app`, `pytest` зелені; `openapi_snapshot.json` оновлено.
- [ ] Чекліст `frontend/AGENTS.md §16` пройдено; таблиця прив'язок перевірена на 390/768/1440.

---

## 6. Порядок робіт

1. `GET /reports/ninja-bindings` + таблиця у формі (стани, `Select all` лише `Ready`, `Exclude`). Це саме те, що зупиняє повторення живого інциденту.
2. Inline `Connect` (переиспользування діалогу прив'язки task-45).
3. Текст помилок + `Connect` у матриці замість `Retry`.
4. Ретрай усередині батчу + перерахунок лічильників.
5. `Rebuild` на трьох рівнях + гейт `period_not_retryable`.
6. Видимість кешу (матриця, прогін, футер документа).

---

## ✅ Completion report

- **Дата:** 2026-08-19
- **Гілка / комміт:** `task-34-client-lifecycle-status` @ (див. коміт цієї таски)
- **Статус:** done-with-followups

### Що зроблено

- **Прив'язки видно до Start.** `GET /api/v1/reports/ninja-bindings` — один дешевий прохід по БД (жодного браузера) з чотирма станами `ready | no_report_name | not_connected | foreign_binding`. Крок «Scope» у формі перестав бути списком чекбоксів і став таблицею *клієнт → назва звіту в кабінеті → стан → дія*.
- **`Select all` більше не втягує наперед провальних** — обирає лише `ready`. Вибрати проблемного вручну можна (це рішення оператора), але тоді біля Start з'являється попередження з точним числом «N of M clients will fail», і Start **не** блокується.
- **Inline `Connect` / `Exclude`.** Дискаверинг task-45 (`matches`/`others`/`confirm_mismatch`) переиспользовано **порядково**: `state.ninja*` з одиночного слота став `state.inlineConnect[clientId]`, тож кілька рядків розкриваються незалежно. Після збереження рядок сам стає `Ready` без перезавантаження форми. `Exclude` лишає клієнта видимим із позначкою.
- **Ретрай усередині батчу.** `POST /reports/batches/{id}/clients/{client_id}/retry` створює нового дитячого прогону з тим самим `batch_id` — сиріт поза батчем більше немає, і батч після ретраїв доходить до `succeeded`.
- **`Rebuild` на трьох рівнях** — період (↻ для `cached`/`done`), прогін (`POST /runs/{id}/rebuild`), батч (`Rebuild failed clients` / `Rebuild all` із підтвердженням і оцінкою часу). `force_refresh` ніде не успадковується мовчки: кнопка називає, який саме режим запускає.
- **Кеш став видимим:** склад у матриці (`From cache` / `N downloaded · M cached`), рядок свіжості на сторінці прогону («Data: 6 of 6 periods reused from PDFs downloaded Aug 18»), і футер клієнтського документа несе дату вихідних PDF.
- **Текст помилки більше не називає REST-ендпоінт**; для `report_name_missing` у матриці пропонується `Connect Reporting Ninja`, а не приречений `Retry`.
- **Понад план (знайдено живою перевіркою, див. нижче):** валідація збереження назви звіту перероблена з прокрутки на пошук кабінету — без цього inline `Connect` не працював для 8 із 9 цільових клієнтів.

### Як зроблено (ключові рішення)

- **Дедуплікація батчу без міграції.** Ретрай створює *новий* `ReportRun` з тим самим `batch_id`, тож у батчі стає два рядки на клієнта. Колонки `superseded_by` немає (план: «міграція не потрібна»), тому `latest_runs_by_client()` (`app/reports/batch.py`) бере останній прогін на клієнта, спираючись на те, що `dict` зберігає позицію ключа при перезаписі значення: порядок рядків матриці стабільний, а витіснений прогін просто зникає з виду. Використовується і в `_reconcile_one` (лічильники), і в `_batch_dto` (матриця).
- **Реанімація завершеного батчу.** Ретрай у термінальному батчі прямо скидає `status="running"`/`finished_at=None` на ORM-об'єкті — інакше `non_terminal_batch_ids()` більше ніколи на нього не гляне. Мутація в `app/reports/batch.py` (свій файл), а не новий сетер у `repositories/reports.py` (чужий).
- **Один примітив на обидва рівні.** `rebuild_run()` клонує прогін із явним `force_refresh`, зберігаючи `batch_id`; і `POST /runs/{id}/rebuild`, і батчевий ретрай викликають саме його. Батчевий «Rebuild all» — **не** третій ендпоінт, а цикл фронта по тому самому per-client виклику (один браузер на весь батч і так робить це послідовно).
- **`cached_at` як дані.** `_split_cached` дописує дату оригінального завантаження в `periods_json` (переприсвоєння всього списку — та сама конвенція, що в `set_periods`), а хвіст `run()` зводить це в `result["cached_periods"]`/`["oldest_cached_at"]`. Банер свіжості на фронті рахується **з `run.periods`**, без нового поля в DTO прогону — той самий прецедент, що `isForeignBinding()` з task-48.
- **Два звичайні кнопки замість split-button.** У кодовій базі немає компонента dropdown/menu, а дії рідкісні (тільки на проваленому рядку) — вигадувати новий інтеракційний примітив під це не варто (AGENTS §3).
- Головні файли: `app/api/reports.py` (3 нові ендпоінти + валідація), `app/reports/batch.py` (`latest_runs_by_client`, `rebuild_run`), `app/reports/runner.py` (`cached_at`, склад кешу, текст помилки), `app/reports/period_retry.py` (гейт), `app/reports/document.py` (футер), `frontend/js/reports-{wizard,batch,run}.js`, `frontend/js/api.js`, `frontend/css/reports.css`.

**Відхилення від плану:** точне число провальних клієнтів показується в рядку-попередженні поруч зі Start, а не всередині лейбла кнопки — лейбл уже несе «Generate N monthly reports (Ninja PDFs)», і два числа в одному рядку читалися б як одне.

### Контрактні зміни (Contract changes)

Синхронізація `task-02` **не потрібна**: `app/schemas/reports.py` не входить у його owned paths (`enums/adapter/metrics/api.py`), він належить рекордній гілці task-42.

Адитивні (нічого не видалено, `openapi_snapshot.json` оновлено — 314 рядків додано, 0 видалено):
- нові ендпоінти `GET /reports/ninja-bindings`, `POST /reports/runs/{id}/rebuild`, `POST /reports/batches/{id}/clients/{client_id}/retry`;
- нові DTO `NinjaBindingRowDTO`/`NinjaBindingsDTO` (+ `NinjaBindingState`), `RunRebuildRequest`, `BatchClientRetryRequest`;
- нові поля: `ReportRunPeriodDTO.cached_at`, `ReportTableDTO.cached_periods`/`oldest_cached_at`, `ReportBatchClientRowDTO.error_code`/`cached_count`.

Поведінкові зміни наявних ендпоінтів (форма не змінилась, семантика — так):
- `PUT /reports/clients/{id}/ninja-report` перевіряє існування назви **пошуком** кабінету, а не прокрученим списком (див. Follow-ups);
- `NinjaReportNameDTO.others_truncated` тепер `True` і тоді, коли пошук довів неповноту списку, навіть якщо прокрутка заявила `truncated=False`;
- `GET /reports/batches/{id}` віддає по одному рядку на клієнта (останній прогін), а не по рядку на кожен дитячий прогін.

### Як перевірити (verification)

```bash
docker compose up -d --build
docker compose exec api poetry run pytest tests/unit/reports tests/unit/api -q
docker compose exec api poetry run ruff check .
docker compose exec api poetry run mypy app
NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://localhost:6524 \
  CHROMIUM_PATH=/snap/bin/chromium node tests/e2e/frontend/reports.cjs
curl -s localhost:6524/api/v1/reports/ninja-bindings | python3 -m json.tool
```

В UI (демо-хуки для станів, яких немає в фікстурах):
- `#/reports` → **Generate report** → *All clients*: таблиця прив'язок; `Select all` позначає лише `Ready`; `Connect` розкриває дискаверинг у рядку; `Exclude` лишає рядок видимим.
- `?fixture=all-states&demo=report_name_missing` → батч: у рядках `Connect Reporting Ninja` замість `Retry`, у шапці `Rebuild failed clients`.
- `?fixture=all-states&demo=cache` → прогін: банер «Data: 6 of 6 periods reused…», ↻ біля кожного періоду, `Rebuild (re-download)` у шапці.

### Тести

- `tests/unit/api/test_reports_api.py` (+11): усі чотири стани `ninja-bindings` без жодного браузерного виклику; батч-ретрай (лишається в батчі, доходить до `succeeded`, не чіпає сусідів, 404/409); `rebuild` (`force_refresh=true`, збереження `batch_id`, 404/409); збереження назви, якої прокручений список не бачив; «не змогли перевірити» без credentials; `others_truncated` при доведеній неповноті.
- `tests/unit/reports/test_batch.py` (+5): `latest_runs_by_client`, `rebuild_run` (батч-лінк, реанімація завершеного батчу, 2 негативні).
- `tests/unit/reports/test_period_retry.py` (+2, 1 переписано): `cached`/`done` перезбираються, `waiting` — досі `409`.
- `tests/unit/reports/test_report_runner.py` (+1, 1 розширено): склад кешу в `result_json` лише коли кеш був; текст `report_name_missing` без REST-шляху.
- `tests/unit/reports/test_document.py` (+2): футер із датою і без неї.
- `tests/e2e/frontend/reports.cjs`: прохід `scope=all` (стани, `Select all`, ціна на Start, inline `Connect`, `Exclude` з клавіатури), матриця батчу (`Connect` замість `Retry`, підтвердження `Rebuild` із оцінкою), сторінка прогону (банер кешу, ↻ на кожному періоді).

### Follow-ups / борг

1. **🔴 `_scroll_all_names` у `app/reports/ninja_browser.py` бреше про повноту.** Живий вимір 2026-08-19: повертає 16 назв, обриваючись на `GRP-Internal report`, і при цьому `truncated=False` — евристика «набір перестав рости» не відрізняє завершену прокрутку від застряглої. Через це `PUT .../ninja-report` відкидав як `report_name_unknown` **кожного клієнта після «G»** (SWM, SAPHNY, SDS, SHB, HWELL, LAXTON, NYCHA, PJC), тобто рівно тих, заради кого ця таска. Обійдено тут переходом валідації на пошук, але корінь лишається і псує `others` у дискаверингу та `yovth-report audit-ninja-bindings` (`app/cli.py:604`). Файл — **owned path task-48**, тому не редагувався (CLAUDE.md §паралельність). Потрібна окрема таска.
2. **Живий гейт виконано частково.** Перевірено наживо: `ninja-bindings` на 14 клієнтах портфеля, реальне збереження `SWM-Internal report` (стан → `ready`). **Не** зроблено: повторний прогін батчу на 12 клієнтах — він потребує спершу прив'язати решту 9 клієнтів (за §2.7 автопідв'язка здогадом заборонена) і ~70 хв браузерної роботи.
3. **9 клієнтів усе ще `no_report_name`** (ALPHA, GOREPORT, HWELL, LAXTON, NYCHA, PJC, SAPHNY, SDS, SHB) — прив'язуються оператором через нову inline-форму.
4. `tests/contract/test_aiseotools_signatures.py` червоний **до і незалежно від** цієї таски: дрейф публічної поверхні зовнішнього AISeoTools (`BrowserSession`, `TechVerdict`, `TechMachineFindings`, `run_import` отримали нові optional-kwargs). Потребує рішення «оновити снапшот чи пінити інший commit» поза межами цієї таски.
