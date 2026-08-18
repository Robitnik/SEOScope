# task-45 — Reports, раунд 2: справжній прогрес, бейджі дат, XLSX для API, історична таблиця

- **Пакет:** доробка сторінки Reports після живої перевірки: (1) прогрес стає **даними**, а не реконструкцією з тексту логів; (2) PDF більше не дозволяє прив'язати клієнту **чужий** звіт кабінету; (3) колонки періодів отримують бейджі з реальними діапазонами дат; (4) API-прогони теж дають **XLSX**; (5) додається друга таблиця — **Historical Data** (місяць · Clicks · Impressions · CTR · Conversions) в UI та в XLSX; (6) закривається порушення `None ≠ 0`, знайдене в тому самому скріншоті.
- **Хвиля:** N — Reports (round 2, після 42/43).
- **Depends on:** **42** ✅, **43** ✅
- **PLAN.md:** §9.2 (безперервна персистентність прогресу), §10 (REST), **§12 (`None` ≠ `0`)**, §13 (секрети/шляхи).
- **frontend/AGENTS.md:** **обов'язковий** — таска чіпає `frontend/js/reports-*.js` і `frontend/css/reports.css`.
- **Міграція:** одна — `report_runs.periods_json`.

---

## Мета

Прогон живе 18 секунд і виглядає як «нічого не відбувається, потім раптом усе готово». Колонки називаються `This Month` / `Month YOY`, і жодного натяку, які це насправді дати. Секція **Files** каже «No files were saved for this run» — тобто вивантажити результат нікуди. А в PDF-режимі клієнтові можна назавжди прив'язати звіт іншого клієнта.

Закриваємо: **оператор бачить, що саме зараз робиться і скільки це тривало; кожна колонка підписана своїм діапазоном дат; будь-який прогон дає XLSX; під основною таблицею є місячна історія з CTR; і жодне «немає даних» більше не виглядає як «нуль».**

---

## 1. Дослідження (жива перевірка 2026-08-17 на зібраному стеку)

### 1.1 Прогрес реконструюється з **тексту** повідомлень — тому його майже немає

[reports-run.js:281](../../../frontend/js/reports-run.js#L281) будує список періодів, розбираючи рядки логів:

```js
else if (message.startsWith("Reused cached PDF")) row.state = "cached";
else if (message.startsWith("Downloading"))       row.state = "running";
else if (message.endsWith(" done"))               row.state = "done";
```

Наслідки, усі видимі в живому прогоні:

| Симптом | Причина в коді |
|---|---|
| На старті список — `Period 1 … Period 6` без назв і дат | рядок з'являється лише після **першої події** з `details.period`; решта добивається заглушками ([reports-run.js:296](../../../frontend/js/reports-run.js#L296)) |
| Період ніколи не показує `Running` на API-шляху | `_run_api` емітить лише `"{label} done"` **після** роботи ([runner.py:277](../../../app/reports/runner.py#L277)) — стану «почав» не існує |
| Тривалості немає, хоч у розмітці під неї лишені два порожні `<span></span>` | у DTO немає ні часу початку, ні часу завершення періоду |
| Зміна тексту лога тихо ламає прогрес | стан виводиться з `startsWith`/`endsWith` по людському тексту |

`ReportRunDTO` несе лише `period_count` / `periods_done` / `periods_failed` — три числа. Тобто **прогрес не є даними**, і жодна UI-доробка цього не полагодить.

### 1.2 🔴 У PDF-режимі клієнтові можна прив'язати **чужий** звіт — і це зберігається назавжди

[api/reports.py:472](../../../app/api/reports.py#L472):

```python
candidates = [
    NinjaReportCandidateDTO(name=name, exact_match=name.upper().startswith(client_name.upper()))
    for name in names
]
candidates.sort(key=lambda c: not c.exact_match)
```

`names` — це **всі** звіти кабінету (жива перевірка: **32** рядки — `19CLV-client`, `3XE-Internal report`, `ADA-client`, `ANITA-…`, `BVI-…`, `CAB-…`, `EFFYS-…`, `GRP-…`, …). `exact_match` лише **сортує**, нічого не обмежує. Тобто для `SAPHNY` можна вибрати `EFFYS-Internal report`, натиснути Save — і воно ляже в `ClientIntegration.config_json["ninja_report_name"]` **без жодної перевірки** ([api/reports.py:497](../../../app/api/reports.py#L497)).

Далі кожен PDF-прогон цього клієнта тягне PDF **іншого** клієнта, парсить його 8/8 метрик (успішно!), і віддає як звіт SAPHNY. Крос-перевірка проти API **зловить** це (`mismatch`), але вже після 4–7 хвилин браузера — і лише якщо хтось прочитає вердикт. Це найгірший клас дефекту в проєкті: **правдоподібні, але чужі числа під іменем клієнта.**

### 1.3 Бейджі дат — це чиста UI-робота, дані вже є

`ReportTableDTO.periods` уже несе `date_from`, `date_to`, `is_partial` ([schemas/reports.py](../../../app/schemas/reports.py)). Фронт їх просто не малює — у шапці лише лейбл:

```js
${periods.map((p, i) => `<th data-partial="${p.is_partial}" …>${escapeHtml(p.label)}</th>`)}
```
([reports-run.js:444](../../../frontend/js/reports-run.js#L444))

Тобто «щоб розуміти що і як» коштує рівно одного рядка розмітки в шапці + стилю. Бекенд не змінюється.

### 1.4 API-прогони не створюють XLSX узагалі

`build_xlsx` викликається **лише** в `_run_pdf` ([runner.py:388](../../../app/reports/runner.py#L388)). `_run_api` завершується, повернувши таблицю, і жодного файлу не пише — звідси «No files were saved for this run» у скріншоті. При цьому сам `build_xlsx` уже вміє все потрібне (аркуші `Report` / `Import Data`, а `Data Sources`/`Cross-check` — лише для PDF), тож це питання **виклику**, не нового модуля.

### 1.5 Історична таблиця була свідомо викинута — і добувається **одним** запитом

[xlsx.py:6](../../../app/reports/xlsx.py#L6) прямо це фіксує:

> `"Historical Data"/monthly-row-append sheets dropped — those served a hand-kept Google Sheets history that has no equivalent here`

Тепер вона потрібна (фото 2). Живий вимір Reporting Ninja:

| Запит | Час | Результат |
|---|---|---|
| GSC `["month","clicks","impressions"]`, 8 місяців | **0,5 с** | `2025-12 … 2026-07`, по місяцях |
| GA4 `["yearMonth","firstUserDefaultChannelGroup","keyEvents"]` | **1,3 с** | organic keyEvents по місяцях |

І — головне — **числа збігаються з рештою системи**. Липень 2026, EFFYS:

| | Historical-запит | Колонка `This Month` у звіті | Бакет `Organic Conversions` у Trends (task-44) |
|---|---|---|---|
| Clicks | 2 528 | 2 528 | — |
| Impressions | 31 564 | 31 564 | — |
| Organic Conversions | **654** | **654** | **654** |

CTR = 2 528 / 31 564 = **8,01 %**, що дорівнює власному полю `ctr` Reporting Ninja (8,009 %). Тобто CTR рахується як `clicks/impressions`, а не тягнеться окремою метрикою.

> **Пастка успадкованого коду:** AISeoTools' [`historical_fetcher.py:95`](/home/elon/dev/seo/AISeoTools/seo_audit/clientreport/historical_fetcher.py#L95) бере organic через **`sessionMedium`** — ту саму помилкову атрибуцію, яку task-42 уже виправив у решті звіту (розбіжність 5–15 %). Портуємо **виправленим**: `firstUserDefaultChannelGroup == "Organic Search"`. Інакше колонка `Conversions` в історичній таблиці розійдеться з колонкою `Organic Conversions` таблицею вище — в одному й тому самому файлі.

### 1.6 🔴 Reporting Ninja віддає **рядок нулів** там, де даних немає — і `0` у скріншоті саме звідти

У скріншоті оператора `Clicks` і `Impressions` за `Month YOY` та `Quarterly YOY` = **0**, а відсоткові колонки — `—`. Перевірка на `3XE` (`sc-domain:exxxoticaexpo.com`):

| Запит | Липень 2026 | Липень 2025 | Січень 2023 |
|---|---|---|---|
| без розмірності (як зараз) | `{clicks: 36885, impressions: 228232}` | **`{clicks: 0, impressions: 0}`** | **`{clicks: 0, impressions: 0}`** |
| з розмірністю `month` | 1 рядок, 36 885 | 1 рядок, **0** | 1 рядок, **0** |
| **з розмірністю `date`** | **31 рядок** | **0 рядків** | **0 рядків** |

Січень 2023 — за роки до появи property. Тобто нуль там не означає «нуль кліків», він означає «даних немає»; наш код чесно переносить те, що віддав провайдер, і в результаті UI показує `0` там, де за PLAN §12 має бути `—`.

**Дискримінатор знайдено, і він безкоштовний:** запит із розмірністю `date` віддає **нуль рядків** для періоду без даних і повний набір днів для періоду з даними. Один запит, ті самі 0,5 с. Немає жодного дня → `None`; дні є → справжня сума (і справжній нульовий день лишається нулем).

Цей самий дискримінатор потрібен історичній таблиці: місяць без даних мусить бути `—` з порожнім CTR, а не рядком нулів із CTR 0,00 %.

---

## 2. Рішення

### 2.1 Прогрес — це дані, а не парсинг тексту

Нова колонка `report_runs.periods_json` (одна міграція) — список станів періодів, який пише сам `runner`:

```json
[{"label": "This Month", "date_from": "2026-07-01", "date_to": "2026-07-31",
  "is_partial": false, "state": "running", "started_at": "…", "finished_at": null,
  "duration_seconds": null, "from_cache": false, "error": null}]
```

- **Заповнюється одразу при старті прогону** — усі періоди зі станом `waiting` і вже відомими датами. Тому список у UI повний і підписаний **з першої секунди**, а не після першої події.
- `state`: `waiting | running | done | cached | failed | skipped`. Перехід у `running` пишеться **перед** роботою над періодом — на обох шляхах (саме цього бракує `_run_api`).
- Оновлення — та сама коротка транзакція, що вже робить `update_progress`; персистентність прогресу лишається безперервною (PLAN §9.2), тож F5 і рестарт нічого не втрачають.
- `ReportRunDTO.periods: list[ReportRunPeriodDTO]` — і фронт **видаляє** `derivePeriodRows()` цілком. Жодного `startsWith`/`endsWith` по логах.

Логи лишаються як були — вони для читання людиною, а не як носій стану.

### 2.2 Прив'язка звіту кабінету — тільки підтверджена

Три зміни, кожна закриває свою частину §1.2:

1. `NinjaReportNameDTO` розділяє відповідь: `matches` (звіти, що починаються з `Client.name`) і `others` (решта кабінету). У списку `others` більше немає радіо-кнопки поруч зі `matches` — це окремий, згорнутий блок.
2. Вибір із `others` вимагає **явного підтвердження**: чекбокс `I confirm «EFFYS-Internal report» is the report for SAPHNY` з іменами обох сторін у тексті. Без нього Save заблокований.
3. `PUT /clients/{id}/ninja-report` перевіряє на бекенді: назва **мусить** бути в списку кабінету (не довільний рядок), а при розбіжності префікса вимагає `confirm_mismatch: true` у тілі — інакше `422 report_name_mismatch`. UI не може бути єдиним запобіжником.

Додатково: збережена назва **видима** в UI (у шапці прогону й у майстрі), щоб хибну прив'язку було видно без походу в базу.

### 2.3 XLSX — один шлях для обох джерел

`build_xlsx` переїжджає з `_run_pdf` у спільний хвіст `run_report` — після того, як таблиця готова, незалежно від `source`. Аркуші `Data Sources` / `Cross-check` лишаються умовними (вони справді PDF-only), `Report` / `Historical Data` / `Import Data` — завжди.

Файл лінкується як `report_files(kind="xlsx")`, тобто секція **Files** починає віддавати XLSX для будь-якого прогону. Ім'я — детерміноване, `safe_resolve` як зараз (PLAN §13).

### 2.4 Історична таблиця — один `date`-запит, групування у нас

Замість двох місячних запитів (§1.5) робимо:

- GSC: **один** запит `["date","clicks","impressions"]` на все вікно (≈365 рядків, 1 запит) → групуємо по місяцях самі. Це одночасно дає дискримінатор §1.6: місяць без жодного дня → `None`.
- GA4: **один** запит `["yearMonth","firstUserDefaultChannelGroup","keyEvents"]` → organic conversions по місяцях.

Разом ~2 с на прогон. Вікно — `reports_history_months` (дефолт **12**), **тільки повні місяці** (поточний неповний не потрапляє — саме так і виглядає фото 2, де останній рядок July 2026 при сьогоднішньому 17 серпня).

CTR — обчислюваний: `clicks/impressions`, `None` коли будь-що з двох `None` або impressions = 0. В XLSX — **формула** (`=B15/C15`), щоб рядок лишався живим після вставки в Google Sheets, як у первісному робочому процесі оператора.

Клієнт без GSC → таблиця є, але `Clicks`/`Impressions`/`CTR` = `—`, а `Conversions` заповнені; блок несе явну ноту, а не зникає.

### 2.5 `None ≠ 0` у GSC-запитах

`api_source.fetch_period_metrics` переходить на `date`-розмірність для GSC (§1.6): нуль рядків → `None` для `clicks`/`impressions`. Це виправляє і основну таблицю (YOY-колонки у скріншоті), і історичну одним рішенням.

GA4 такої проблеми не має (перевірено раніше: період без даних не віддає рядків каналів), але тест на це все одно з'являється — щоб регресія була видимою.

---

## 3. Специфікація UI

### 3.1 Прогрес (живий прогон)

Рядок на період: **лейбл**, **діапазон дат**, чіп стану, тривалість. Плюс над списком — `3 of 6 periods · 00:42 elapsed` і смуга. Стани не колір-онлі (іконка + підпис). `cached` показується як окремий стан із підписом `Reused` — на PDF-шляху це різниця між 0 і 60 секундами, і оператор має її бачити.

### 3.2 Бейджі дат у таблиці METRICS

Шапка кожної колонки періоду — два рядки: назва (`This Month`) і діапазон (`Jul 1 – Jul 31, 2026`), дати — локаль-aware, `translate="no"` не потрібен. Неповний період додає чіп `Partial`. Ті самі бейджі — у списку прогресу й у майстрі (крок 2 уже показує прев'ю діапазонів — беремо той самий формат, щоб не було двох мов дат).

### 3.3 Друга таблиця — Historical Data

Під METRICS, окремою секцією `HISTORY`:

| Date | Clicks | Impressions | CTR | Conversions |
|---|---|---|---|---|
| December 2025 | 2 369 | 32 391 | 7.31% | 885 |
| … | | | | |
| July 2026 | 1 630 | 143 177 | 1.14% | 515 |

- порядок — від старішого до новішого (як у фото 2);
- `Conversions` — це **Organic** Conversions; підпис колонки несе це в тултипі/підзаголовку, бо саме так велася історія в Google Sheets;
- числа табличні, `—` для відсутніх, CTR — 2 знаки;
- кнопка `Copy table` (той самий сценарій, що XLSX-аркуш `Import Data`).

### 3.4 Files

Завжди щонайменше XLSX. Порожній стан «No files were saved for this run» лишається тільки для реально порожнього випадку (прогін упав до побудови таблиці) і формулюється як причина, а не як факт.

---

## 4. Owned paths

```
app/reports/runner.py          (periods_json, спільний виклик build_xlsx, історична таблиця)
app/reports/api_source.py      (date-розмірність GSC; fetch_history)
app/reports/history.py         (новий: місячна історія + групування + CTR)
app/reports/xlsx.py            (аркуш Historical Data, формула CTR)
app/reports/table.py           (за потреби — носій історії в результаті)
app/api/reports.py             (DTO periods, matches/others, валідація PUT)
app/schemas/reports.py         (ReportRunPeriodDTO, ReportHistoryDTO, розділені кандидати)
app/db/models.py               (адитивно: report_runs.periods_json)
migrations/versions/<rev>_report_run_periods.py   (нова)
app/core/config.py             (адитивно: reports_history_months)
frontend/js/reports-run.js     (прогрес із даних, бейджі дат, історична таблиця, files)
frontend/js/reports-wizard.js  (matches/others + підтвердження)
frontend/css/reports.css
tests/unit/reports/**, tests/unit/api/test_reports_api.py, tests/e2e/frontend/reports.cjs
```

Перетинів із **task-44** немає (та володіє `app/metrics/portfolio.py`, `app/integrations/reporting_ninja.py`, `frontend/js/trends-*.js`).

---

## 5. Тести

**Unit:**
- `periods_json` — заповнюється **до** першого періоду; переходи `waiting → running → done/failed/cached`; тривалість рахується; скасування лишає решту `skipped`, а не `waiting`;
- `api_source` — GSC `date`-розмірність: 0 рядків → `None`; 31 день → сума; день зі справжнім `0` → `0` (не `None`);
- `history` — групування по місяцях; поточний неповний місяць виключений; місяць без днів → `None` і CTR `None`; CTR при `impressions=0` → `None`; клієнт без GSC → лише conversions;
- **атрибуція:** organic береться з `firstUserDefaultChannelGroup`, а не `sessionMedium` (тест на форму запиту);
- `xlsx` — обидва шляхи створюють файл; `Historical Data` присутній завжди, `Data Sources`/`Cross-check` — лише на PDF; CTR — формула, не число;
- `api/reports` — `PUT ninja-report` відхиляє назву поза кабінетом (`422`) і розбіжний префікс без `confirm_mismatch` (`422`); `matches`/`others` розділені; `openapi_snapshot.json` оновлено.

**e2e (`reports.cjs`):** живий прогон видимо проходить стани періодів; шапка несе дати; історична таблиця рендериться; XLSX завантажується; у майстрі вибір із `others` заблокований без підтвердження.

**Живий гейт:**
1. API-прогін `monthly` для EFFYS: XLSX є, `Historical Data` містить липень 2026 = 2 528 / 31 564 / 8,01 % / **654**, тобто збігається з колонкою `This Month` тієї ж книги.
2. API-прогін `monthly` для 3XE: `Clicks`/`Impressions` за `Month YOY` = **`—`**, а не `0`.
3. Спроба прив'язати `SAPHNY` до `EFFYS-Internal report` блокується без явного підтвердження, а через API — без `confirm_mismatch`.

---

## 6. Definition of Done / Gate

- [ ] На старті прогону список періодів повний: назви + діапазони дат, усі `Waiting` — ще до першої події.
- [ ] Період, що виконується, показує `Running` на **обох** шляхах; завершений — тривалість; `cached` — окремим станом.
- [ ] `derivePeriodRows()` (парсинг тексту логів) видалений; прогрес читається виключно з `ReportRunDTO.periods`.
- [ ] Кожна колонка METRICS підписана діапазоном дат; неповний період має чіп `Partial`.
- [ ] Будь-який успішний прогін (API і PDF) має XLSX у секції Files; файл відкривається.
- [ ] Таблиця `Historical Data` є в UI і в XLSX; 12 повних місяців; CTR — формула; `Conversions` = Organic.
- [ ] Числа історичної таблиці за перекривний місяць **точно** дорівнюють відповідній колонці основної таблиці.
- [ ] Період без даних у GSC дає `—`, а не `0` (перевірено на 3XE / `Month YOY`).
- [ ] Прив'язати клієнтові чужий звіт кабінету неможливо ні через UI, ні через API без явного підтвердження; збережена назва видима в UI.
- [ ] `ruff check .`, `mypy app`, `pytest` зелені; міграція застосовується й відкочується.
- [ ] Чекліст `frontend/AGENTS.md §16` пройдено; скріншоти 390/768/1440 переглянуті.

---

## 7. Порядок робіт

1. **`periods_json` + міграція + DTO + runner** — фундамент прогресу; одразу дає й дати для бейджів.
2. Фронт: прогрес із даних, видалення `derivePeriodRows`, бейджі дат у шапці.
3. `date`-розмірність GSC (§2.5) — маленька зміна з великим ефектом на правдивість; закривається тестами до UI.
4. `history.py` + аркуш XLSX + спільний виклик `build_xlsx` для обох шляхів.
5. Історична таблиця в UI + секція Files.
6. Прив'язка звіту: розділення `matches`/`others`, підтвердження, серверна валідація.
7. Живий гейт §5 і візуальний цикл AGENTS §15.

---

## ✅ Completion report

- **Дата:** 2026-08-17
- **Гілка / комміт:** `task-34-client-lifecycle-status` @ (див. комміт `feat(reports): …round2`)
- **Статус:** done

### Що зроблено

1. **Прогрес — це дані.** Нова колонка `report_runs.periods_json` (міграція `a1c58d7e26b4`)
   заповнюється **вже при постановці прогону в чергу** (`POST /runs` знає періоди — він їх щойно
   побудував для валідації), тож UI показує повний, підписаний датами список періодів у стані
   `Waiting` ще до того, як воркер узяв прогон. Далі стани міняє сам runner:
   `waiting → running → done | cached | failed`, а скасування/аварійний вихід лишає решту
   `skipped`. `running` пишеться **перед** роботою на **обох** шляхах (на API-шляху цього стану
   раніше не існувало взагалі). Кожен рядок несе `started_at`/`finished_at`/`duration_seconds`.
   `derivePeriodRows()` (парсинг тексту логів) видалено з фронту цілком.
2. **Бейджі дат.** Кожна колонка METRICS підписана діапазоном (`Jul 1 – Jul 31, 2026`), неповний
   період має чіп `Partial`. Той самий формат — у списку періодів і в прев'ю майстра (один
   `formatDateRange` у `formatters.js`, щоб не було двох мов дат).
3. **XLSX для обох джерел.** `build_xlsx` переїхав із `_run_pdf` у спільний хвіст `run()`. Будь-який
   прогін, що зібрав ≥1 період, лінкує `report_files(kind="xlsx")`; секція **Files** більше не каже
   «No files were saved», а порожній стан формулює **причину**.
4. **Historical Data.** Новий модуль `app/reports/history.py`: 12 **повних** місяців
   (`Date · Clicks · Impressions · CTR · Conversions`), в UI окремою секцією з `Copy table` і
   окремим аркушем XLSX, де CTR — **жива формула** `=IFERROR(B/C,"")`. `Conversions` — **Organic**,
   через `firstUserDefaultChannelGroup`, а не `sessionMedium` порту AISeoTools.
5. **`None ≠ 0` у GSC.** Усі GSC-запити перейшли на розмірність `date`: 0 рядків → `None`,
   є дні → справжня сума, справжній нульовий день лишається `0`.
6. **Прив'язка звіту кабінету.** `matches`/`others` у відповіді + окремий згорнутий блок і чекбокс
   підтвердження в майстрі + серверна валідація `PUT` (назва мусить бути в кабінеті;
   розбіжність префікса вимагає `confirm_mismatch`). Збережена назва видима в шапці прогону й у
   майстрі.

### Як зроблено (ключові рішення)

- **Періоди пишуться двічі — при `POST /runs` і на старті воркера.** Це навмисно: перший запис дає
  повний список у `queued` (головна претензія §1.1), другий — авторитетний, бо доба могла змінитись
  між постановкою в чергу й стартом. Один спільний `runner.period_rows()` для обох.
- **Оновлення станів — окрема коротка транзакція** (`ReportRepository.update_period`), як
  `update_progress`; помилка запису прогресу ніколи не валить прогін (той самий контракт, що в
  `_emit`). `periods_json` перезаписується цілком — plain-`JSON` не `MutableList`, мутація вкладеного
  dict не флашилась би.
- **`ninja_browser.download_periods` дістав адитивний keyword-only `on_period_state`** — тільки цей
  цикл знає, коли реально почалось ~55-секундне завантаження. Без аргументу поведінка та сама, що до
  task-45.
- **Ліміт рядків GSC рахується з вікна** (`_row_limit`), а не константою: `query()` має
  `limit=100` за замовчуванням, і день-розмірність за рік (365 рядків) мовчки давала б **невірну
  суму**. Ліміт росте разом із `reports_history_months`.
- **`cached` — окремий стан**, не різновид `done`: на PDF-шляху це різниця між 0 і ~60 секундами, і
  в UI він читається як `Reused`.
- **Тривалість — тільки у завершеного періоду.** Другий «живий» лічильник у рядку поруч із загальним
  elapsed у шапці був би шумом; на живому прогоні стан комунікує чіп `Running`.
- **Історія — best-effort.** Прогін із валідною таблицею ніколи не падає через історію; при збої
  аркуш XLSX і секція UI лишаються, але кажуть, що дані зібрати не вдалось.
- Головні файли: `app/reports/history.py` — вікно місяців, групування, CTR, два запити;
  `app/reports/runner.py` — стани періодів, спільний XLSX, історія; `app/reports/api_source.py` —
  `date`-розмірність + `fetch_gsc_daily`/`fetch_ga4_monthly_channel_conversions`;
  `app/api/reports.py` — DTO періодів, `matches`/`others`, валідація `PUT`;
  `frontend/js/reports-run.js` — прогрес із даних, бейджі, історія;
  `frontend/js/reports-wizard.js` — гейт підтвердження.

### Контрактні зміни (Contract changes)

- **Ламка (навмисно, це і є фікс §1.2):** `NinjaReportNameDTO.candidates` → **`matches` + `others`**
  (+ `client_name`). Споживач один — `reports-wizard.js`, оновлений у цьому ж комміті.
- **Адитивні:** `ReportRunDTO.periods: list[ReportRunPeriodDTO]` і `ReportRunDTO.ninja_report_name`
  (заповнює лише single-run ендпоінт — списковий не робить lookup на рядок);
  `ReportTableDTO.history: ReportHistoryDTO | None`;
  `NinjaReportNameUpdateRequest.confirm_mismatch: bool = False`; нові коди помилок `PUT`:
  `report_name_mismatch`, `report_name_unknown`; `report_runs.periods_json` (nullable);
  `Settings.reports_history_months = 12`;
  `ninja_browser.download_periods(..., on_period_state=None)`.
- `openapi_snapshot.json` перегенеровано.
- **Поза формальними owned paths таски** довелось торкнутись (усе адитивно, зона закритих 42/43,
  яку ця таска й доробляє): `app/db/repositories/reports.py` (методи запису `periods_json`),
  `app/reports/ninja_browser.py` (колбек вище), `frontend/js/api.js` (fixture-шар + `confirm_mismatch`),
  `frontend/js/formatters.js` (`formatDateRange`), `frontend/fixtures/reports-run-bundle.json`.

### Як перевірити (verification)

```bash
docker compose exec -T api poetry run alembic upgrade head
docker compose exec -T api poetry run ruff check . && docker compose exec -T api poetry run mypy app
docker compose exec -T api poetry run pytest tests/unit/reports tests/unit/api/test_reports_api.py -q
docker compose exec -T api poetry run pytest -q

NODE_PATH=/tmp/yovth-e2e/node_modules CHROMIUM_PATH=/snap/bin/chromium BASE_URL=http://127.0.0.1:6524 \
  node tests/e2e/frontend/reports.cjs
```

У браузері: `#/reports` → `Generate report` → API → `Start`. Список періодів повний і підписаний
датами **одразу**; кожен період проходить `Waiting → Running → Done` із тривалістю; після
завершення — METRICS із діапазонами в шапці, `HISTORICAL DATA` під нею, `PERIODS` із тривалостями і
`Download XLSX`.

**Живий гейт §5 — пройдено на реальному стеку 2026-08-17:**

1. EFFYS, API `monthly` (15 с): XLSX є (аркуші `Report` / `Historical Data` / `Import Data`),
   `Historical Data` за липень 2026 = **2 528 / 31 564 / 8,01 % / 654** — точно дорівнює колонці
   `This Month` тієї ж книги. Серпень–жовтень 2025 (до появи даних) — `—`, не нулі.
2. 3XE, API `monthly`: `Clicks`/`Impressions` за `Month YOY` і `Quarterly YOY` — **`—`**, при тому
   що `Users` за той самий період реальні (55 816). Тобто виправлено саме GSC-нуль, а не «все».
3. `PUT .../ninja-report` для SAPHNY з `EFFYS-Internal report` → **`422 report_name_mismatch`**;
   з вигаданою назвою + `confirm_mismatch: true` → **`422 report_name_unknown`**; дискаверинг
   реального кабінету (32 звіти) віддає `matches: []`, `others: 32` для SAPHNY.

### Тести

- **Unit (`tests/unit/reports/`):** `test_history.py` — вікно повних місяців (серпень не потрапляє),
  групування по днях, CTR = `clicks/impressions` (звірено з живим 8,01 %), `None` при
  `impressions=0`, місяць без днів → `None`, клієнт без GSC → лише conversions, **форма запиту**
  (`yearMonth` + `firstUserDefaultChannelGroup`, ніколи `sessionMedium`), один GSC-запит на вікно з
  лімітом > 365. `test_api_source.py` — `date`-розмірність: 0 рядків → `None`, дні → сума, справжній
  `0` → `0`, ліміт більший за кількість днів. `test_report_runner.py` — періоди підписані до старту,
  порядок переходів `running`/`done` на API-шляху, `failed` із причиною, скасування → `skipped`,
  `cached` як окремий стан, XLSX для API-прогону, відсутність XLSX коли не зібрано жодного періоду,
  історія в `result_json`. `test_xlsx.py` — аркуш є завжди (навіть коли історію зібрати не вдалось),
  CTR — формула, місяць без даних порожній, нота про відсутній GSC.
- **API (`tests/unit/api/test_reports_api.py`):** `queued`-прогін уже несе підписані періоди;
  `history` у `GET /table` (і `None` для старого результату); розділення `matches`/`others`;
  `PUT` — `422` на чужий префікс без підтвердження (і в БД **нічого не записано**), `200` з
  підтвердженням, `422` на назву поза кабінетом.
- **e2e (`tests/e2e/frontend/reports.cjs`):** queued-прогін показує 6 підписаних періодів у
  `Waiting` без жодної події; шапка METRICS несе дати; `Reused` і тривалості у завершеному прогоні;
  історична таблиця (12 рядків, CTR у %, `—` для місяця без даних); нота + 12 рядків для клієнта без
  GSC; `Download XLSX` для API-прогону; у майстрі — окремий блок чужих звітів, Save заблокований до
  підтвердження і **пере-блокований** при виборі наступного чужого звіту. Скріншоти
  `reports-run-history-1440.png`, `reports-wizard-ninja-confirm-1440.png`.

### Follow-ups / борг

- **`PUT /clients/{id}/ninja-report` тепер відкриває браузер** (перелік кабінету ≈10–20 с), бо це
  єдиний спосіб перевірити, що назва справді існує. Прийнятно для адмін-дії; якщо стане болячим —
  кешувати перелік кабінету з TTL (окремий ендпоінт/таблиця), а не послаблювати перевірку.
- **Без облікових даних браузера перевірка «назва є в кабінеті» пропускається** (лишається лише
  правило префікса + підтвердження). Свідомо: інакше середовище без креденшелів не могло б зберегти
  назву взагалі. Задокументовано в docstring `_save_ninja_report_name`.
- **Кабінет віддає дублікати назв** (`19CLV-client` двічі в живому переліку) — це поведінка скрапера
  `list_report_names` із task-42, не чіпалась.
- **`ReportRunDTO.ninja_report_name` — поточна прив'язка клієнта, а не та, з якою прогін реально
  виконувався.** Для історичного прогону, де назву згодом змінили, шапка покаже нову. Точне
  значення на прогін вимагало б ще однієї колонки; поки що провенанс дає `report_files
  .source_report_name`.
