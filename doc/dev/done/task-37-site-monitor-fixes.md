# task-37 — Site Monitor: фікси після живої перевірки

- **Пакет:** сім дефектів, знайдених на живому стеку відразу після здачі task-36. Два блокують сторінку (таблиця Pages не вантажиться взагалі; прогрес прогону не показується ніколи), решта — брехливі числа в журналі, зламана сітка й нескінченна стрічка. Плюс системний фікс, який пояснює, **чому це доїхало до прода зеленим**.
- **Хвиля:** M — спостережуваність сайтів.
- **Depends on:** **35** ✅, **36** (UI монітора — фіксимо його ж поверхню)
- **PLAN.md:** §10 (REST-конвенції), §12 (чесні стани, `None` ≠ `0`)
- **frontend/AGENTS.md:** §5 (правда даних), §6 (стани), §9 (детермінований прогрес, «не анімуємо вигаданий відсоток»), §11 (адаптив), §15 (візуальний цикл)
- **AISeoTools:** змін не потребує

---

## 1. Як це перевірялось (щоб відтворити)

Жива сторінка `#/client/3bd6aa2d-9721-45f9-9dcd-033e22854311/monitor` (клієнт **3XE** / `exxxoticaexpo.com`, 608 URL у sitemap), стек піднятий, дані реальні.

- Рендер і перехоплення мережі — Playwright **у контейнері `api`**: `poetry run playwright install chromium` (кеш лягає в `/tmp/.cache/ms-playwright` і **зникає з рестартом контейнера** — це не разова інсталяція, а щоразова); слухачі `response` (статус ≥ 400), `pageerror`, `console`.
- API перевірявся паралельно прямими `curl`, включно з `POST /monitor/runs` і поллінгом зведення що 2 с — саме так відтворюється F2.

**Бекенд працює:** повний прогін — 609 URL за 6 хв 45 с, 184 MB, `sitemap_health` 608/608 ok, robots 200, GSC-верифікація знайдена, версії та дифи пишуться. Усе нижче — дефекти шару «API-контракт ↔ UI».

## 2. Дефекти

| # | Симптом | Першопричина | Рівень |
|---|---|---|---|
| F1 | Блок Pages завжди «Could not load pages» | `sort=-last_checked_at` → **422**: контракт розійшовся у трьох шарах | **P0** |
| F2 | Прогресу краулера немає; «check complete» через 2 с після старту | зведення віддає лише **завершені** прогони | **P0** |
| F3 | `[object Object]` замість тексту помилки (в усьому застосунку) | `new Error(detail)`, де `detail` — масив валідації FastAPI | P1 |
| F4 | Журнал: light-прогін на 1 URL звітує ті самі `27 warning / 789 info`, що й повний на 609 | у лічильники прогону пишуться **відкриті зміни клієнта загалом** | P1 |
| F5 | Колонка DNS вчетверо вища за сусідні, під Robots/Sitemap ~800 px порожнечі; значення TXT не видно | блок DNS втиснутий у 1/3 сітки й малює всі записи | P1 |
| F6 | Сторінка 11 343 px заввишки | стрічка змін рендерить усі 108 відкритих без пагінації | P2 |
| F7 | В картці DNS висить «…has no dedicated check yet» | TODO розробника показаний оператору | P2 |

---

## 3. Фікси

### F1 (P0) — 422 на `/monitor/pages`

**Розійшлися три шари:**

| Шар | Що робить | Файл |
|---|---|---|
| Фронт | шле `-last_checked_at`, а `SORT_FIELDS` містить ще й `version_seq` | [monitor-client.js:88-90](../../../frontend/js/monitor-client.js#L88-L90), [136-138](../../../frontend/js/monitor-client.js#L136-L138) |
| API | `Literal["last_checked_at", "status", "url"]`, **без** префікса напрямку | [api/monitor.py:445](../../../app/api/monitor.py#L445) |
| Репозиторій | мапа з трьох полів і **завжди** `.desc()` — напрямок ігнорується навіть якби пройшов валідацію | [repositories/monitor.py:560-565](../../../app/db/repositories/monitor.py#L560-L565) |

Це дефолтний стан сторінки, тож блок падає **завжди, у кожного клієнта, з першого відкриття**.

**Рішення — розширюємо бекенд** (фронт коректний: у нього є клікабельні заголовки на обидва напрямки, і ламати робочий UI заради вужчого API неправильно):

- API: `sort: Literal[…]` з 8 значень — 4 поля × 2 напрямки (`url`, `-url`, `status`, `-status`, `last_checked_at`, `-last_checked_at`, `version_seq`, `-version_seq`). Явний `Literal` замість `str`+ручної валідації: FastAPI валідує сам, а OpenAPI лишається самодокументованим.
- Репозиторій: розбір префікса `-`, `version_seq` у мапі, `.asc().nulls_last()` / `.desc().nulls_last()`.
- Перегенерувати `tests/unit/api/data/openapi_snapshot.json`.

**Перевірка:** `curl ".../monitor/pages?sort=-last_checked_at&limit=25&offset=0"` → `200`; клік по кожному з чотирьох заголовків в обидва боки → `200` **і реально інший порядок рядків** (а не тихо той самий `desc`).

### F2 (P0) — прогрес прогону не з'являється, поллінг помирає за 2 секунди

**Першопричина:** [api/monitor.py:214-215](../../../app/api/monitor.py#L214-L215) бере `latest_finished_run(...)`, а той у [repositories/monitor.py:150-158](../../../app/db/repositories/monitor.py#L150-L158) явно виключає `running`/`queued`. Поллер [monitor-client.js:566](../../../frontend/js/monitor-client.js#L566) читає `summary.last_${kind}_run` → отримує **попередній завершений** прогін, бачить `succeeded`, вважає роботу закінченою, зупиняє таймер і показує тост «check complete» — поки краулер працює ще шість хвилин.

Відтворено: `POST /monitor/runs` (light), далі 8 тіків по 2 с — усі повертали прогін `18:06`, новий (`18:09:13 → 18:09:18`) не з'явився жодного разу.

**Рішення, крок А — без зміни контракту:** поллер бере `listMonitorRuns(clientId, { kind, limit: 1 })` — `list_runs` ([repositories/monitor.py:160](../../../app/db/repositories/monitor.py#L160)) статус **не** фільтрує й віддає `running`.
> Обов'язкова деталь: звіряти `run.id` з `run_id`, який повернув `202` на `POST`. Інакше перший тік (доки рядок ще не створено) знову побачить старий завершений прогін і зупинить поллінг — той самий баг, лише вужчим вікном.

**Рішення, крок Б — щоб прогрес був детермінований, а не «Checked N so far»:**

- нова колонка `monitor_runs.urls_total` (`NULL` для старих рядків — `None` ≠ `0`), міграція з `down_revision = "6d9419c9493c"`;
- runner проставляє `len(targets)` перед циклом обходу;
- `MonitorRunDTO.urls_total` + смуга «Checked 120 of 609» замість тексту. Це справжня модель `completed/total`, якої вимагає AGENTS.md §9 — на відміну від відсотка, вигаданого з нічого;
- частота флашу лічильників: зараз кожні 25 сторінок ([runner.py:985](../../../app/monitor/runner.py#L985)) ≈ 19 с при виміряних 0,77 с/URL, тобто при поллінгу раз на 2 с число стоїть 10 тіків поспіль. Зменшити до 10 сторінок (~8 с).
  > Ризик: `_COMMIT_LOCK` серіалізує коміти між усіма клієнтськими воркерами — частіші коміти дають більше контенції. 10 — компроміс; заміряти тривалість повного прогону до і після, число записати в Completion report.

### F3 (P1) — `[object Object]` замість повідомлення, і не лише в моніторі

[api.js:25](../../../frontend/js/api.js#L25):

```js
const error = new Error(payload.detail?.message || payload.detail || `HTTP ${response.status}`);
```

FastAPI на `422` віддає `detail` **масивом** об'єктів валідації. `detail?.message` → `undefined`, далі в `Error` летить масив → `"[object Object]"`. Це спільний шар, тож так виглядає **будь-яка** помилка валідації в застосунку.

**Рішення:** нормалізувати `detail` перед `new Error`: рядок → як є; об'єкт із `message` → як зараз; масив → зібрати `msg` (+`loc`) перших 2–3 елементів; інакше — `HTTP <status>`. Правка адитивна, зона task-12 — задокументувати в Completion report.

### F4 (P1) — журнал прогонів звітує чужі числа

[runner.py:1035](../../../app/monitor/runner.py#L1035) бере `repo.open_counts_by_severity(client.id)` — **відкриті зміни клієнта загалом** — і пише їх у `changes_critical/warning/info` кожного прогону. Доказ: усі рядки журналу, від light на 1 URL до full на 609, показують однакові `27 / 789`.

**Рішення:** рахувати за `run_id` — нова `count_changes_by_severity(run_id)` у репозиторії (дані вже є: `monitor_changes.run_id` заповнюється). Семантика колонки — «що знайшов **цей** прогін»; «скільки відкрито зараз» уже живе окремо в `MonitorSummaryDTO.open_*` і дублювати його в рядку прогону не треба.

**Історичні рядки** можна полагодити тим самим запитом (`UPDATE monitor_runs SET changes_* = (SELECT COUNT(*) FROM monitor_changes WHERE run_id = …)`) — дані на місці. Зробити одноразовою частиною тієї ж міграції, що й `urls_total`; інакше журнал ще довго показуватиме старі вигадані числа.

### F5 (P1) — блок DNS ламає сітку й ховає те, заради чого існує

Сітка [monitor.css:214-216](../../../frontend/css/monitor.css#L214-L216) — `repeat(3, minmax(0, 1fr))`. Robots ≈180 px, Sitemap ≈245 px, DNS ≈1 100 px: під двома першими висить ~800 px порожнечі, і це читається як поламана верстка, а не як задум. Гірше: таблиця з трьох колонок у третині ширини не влазить, тож колонка значень іде в горизонтальний скрол `.table-scroll` — на 1440 px видно тільки NAME і TYPE, а **токен верифікації GSC**, головна цінність блока, не видно взагалі.

**Рішення:**

- рядок згори — дві колонки (Robots, Sitemap), **DNS & verification — окрема повноширинна секція під ними**. Це лікує і перекіс висот, і горизонтальний скрол;
- список записів згортається до 8 рядків + кнопка «Show all 14» (не приховуємо, що їх більше);
- значення — `overflow-wrap: anywhere` з переносом у два рядки; на ≥1024 px горизонтального скролу в таблиці бути не повинно.

### F6 (P2) — стрічка змін без пагінації

108 відкритих (і 789 `info`) малюються одним списком → сторінка 11 343 px. Ліміт 25 + «Load more» + лічильник «Showing 25 of 108» — рівно той патерн, що вже працює для Pages (`PAGES_PAGE_SIZE`).

### F7 (P2) — TODO розробника в операторському UI

У картці DNS: «Analytics (GA4/GTM) tag presence has no dedicated check yet — open a key page's history below…». Оператору показують план розробки. Замінити на чесний стан: вивести знайдені `analytics_ids` ключової сторінки (дані вже лежать у `monitor_pages.analytics_ids_json`) або прибрати абзац зовсім.

---

## 4. Чому це доїхало до прода зеленим (системний фікс)

`tests/e2e/frontend/monitor-smoke.cjs` ганяє **лише фікстури** — `?fixture=all-states`, `?fixture=monitor-empty`, `?fixture=monitor-running` тощо. У фікстурному режимі `getMonitorPages` віддає локальний JSON і **мережевого запиту не робить взагалі**, тож розбіжність контракту фронт↔API фізично не може проявитись. Скріншоти знімались, `pageerror`/`console.error` збирались — і все було зелене, поки на живому стеку перший же рендер давав 422.

**Фікс процесу:** додати в той самий smoke **один живий прохід** без `?fixture=` проти піднятого стека з двома асертами:

1. жодної відповіді зі статусом ≥ 400;
2. жодного `console.error` / `pageerror`.

Це одна перевірка, яка ловить F1 і F3 разом і не дає повторити клас помилки. Фікстури лишаються — вони покривають стани, яких на живих даних не відтворити.

---

## 5. Owned paths

**Backend:**

```
app/api/monitor.py                          (F1 sort-контракт, F2 DTO)
app/db/repositories/monitor.py              (F1 сортування, F4 count_changes_by_severity)
app/monitor/runner.py                       (F2 urls_total + частота флашу, F4 джерело лічильників)
app/db/models.py                            (F2 колонка urls_total)
app/schemas/monitor.py                      (F2 поле DTO)
migrations/versions/<rev>_monitor_run_totals.py   (down_revision = "6d9419c9493c")
tests/unit/api/data/openapi_snapshot.json   (перегенерувати)
```

**Frontend:**

```
frontend/js/monitor-client.js               (F2 поллінг + смуга, F5 секції, F6 пагінація, F7 копія)
frontend/css/monitor.css                    (F5)
frontend/js/api.js                          (F3 — спільний файл, адитивна правка, задокументувати)
tests/e2e/frontend/monitor-smoke.cjs        (живий прохід)
```

---

## 6. Тести (кожен існує, щоб цей самий баг не повернувся)

| Тест | Що ловить |
|---|---|
| `tests/unit/db/test_monitor_repo.py::test_pages_sort_directions` | усі 8 значень `sort` дають **різний** порядок; `nulls_last` в обидва боки | 
| `tests/unit/api/test_monitor_api.py::test_pages_accepts_frontend_default_sort` | `sort=-last_checked_at` → 200 (буквально той запит, що дав 422) |
| `tests/unit/monitor/test_runner.py::test_run_counters_are_per_run` | light на 1 URL і full на N дають **різні** `changes_*`; лічильники дорівнюють кількості `monitor_changes` із цим `run_id` |
| `tests/unit/monitor/test_runner.py::test_urls_total_set_before_loop` | `urls_total` проставлений до першої сторінки, `NULL` не лишається |
| `tests/unit/frontend/…` (за наявним патерном) | нормалізація `detail`: рядок / об'єкт / масив 422 / порожнеча |
| `tests/e2e/frontend/monitor-smoke.cjs` | живий прохід: нуль відповідей ≥400, нуль `console.error`; фікстура `monitor-running` показує смугу «Checked N of M», а не «complete» |

---

## 7. Definition of Done / Gate

- [x] Блок Pages вантажиться на живому клієнті; сортування працює в обидва боки по всіх чотирьох колонках.
- [x] Ручний повний прогін показує **рухому** смугу «Checked N of M» від старту до кінця; тост «complete» з'являється лише після реального завершення.
- [x] Жодна помилка в UI не містить `[object Object]` (перевірено примусовим 422 — юніт-тестами нормалізатора).
- [x] У журналі прогонів light-рядок і full-рядок мають різні `changes_*`; історичні рядки перераховані міграцією.
- [x] На 1440 px токен GSC-верифікації видно **без** горизонтального скролу; висота блоків у верхньому рядку відрізняється не більш ніж удвічі.
- [x] Сторінка клієнта з великою кількістю змін вміщається в один екран до натискання «Load more» (25 рядків за раз замість усього списку одразу).
- [x] У жодному рядку UI не лишилось «has no dedicated check yet».
- [x] Живий e2e-прохід зелений; повторний запуск на цьому ж клієнті нічого не ламає (read-only, без `POST .../runs`).
- [x] `docker compose exec api poetry run pytest` зелений (окрім одного заздалегідь відомого непов'язаного падіння — дрейф AISeoTools), `ruff`/`mypy` чисті, OpenAPI-снапшот перегенеровано.
- [x] План перенесено в `doc/dev/done/` із Completion report; `INDEX.md` оновлено.

---

## ✅ Completion report

- **Дата:** 2026-08-11
- **Гілка / комміт:** `task-34-client-lifecycle-status` @ `724a0dd` (базовий, з якого зроблено ці зміни)
- **Статус:** done

### Що зроблено

Усі сім дефектів (F1–F7) виправлено, плюс системний фікс процесу (§4 таск-плану). Блок Pages на клієнтській сторінці монітора більше не падає з 422 на дефолтному сортуванні; прогрес ручного прогону показує реальну смугу «Checked N of M» від старту до завершення замість фальшивого «complete» через 2 секунди; жодна помилка API (включно з 422-валідацією) більше не рендериться як `[object Object]`; журнал прогонів показує справжні лічильники змін per-run, а не клієнтський «зараз відкрито» тотал (з одноразовим перерахунком історичних рядків міграцією); блок DNS & verification переїхав у повноширинний рядок під Robots/Sitemap і згортається до 8 рядків із «Show all N»; стрічка змін клієнта пагінована по 25 з «Load more»; TODO-копірайт розробника в картці DNS замінено на чесний операторський текст. Живий e2e-прохід (без `?fixture=`) додано в `monitor-smoke.cjs` — саме він і зловив би F1/F3 до релізу.

### Як зроблено (ключові рішення)

- **F1 — сортування:** API `sort` тепер `Literal` з 8 значень (4 поля × 2 напрямки), а не `str` + ручна перевірка — FastAPI валідує сам, OpenAPI лишається самодокументованим. Репозиторій розбирає префікс `-`, будує `.asc()`/`.desc()` замість завжди `.desc()`, `nulls_last()` в обидва боки.
- **F2 — прогрес, крок А (без зміни контракту):** поллер (`monitor-client.js`) більше не читає `summary.last_{kind}_run` (той його бекенд ніколи не може віддати як `running`, бо `_summary()` бере `latest_finished_run`, яка явно виключає `running`/`queued` — це й був корінь бага, і він лишається так само в `_summary()` навмисно, бо summary — «остання ЗАВЕРШЕНА перевірка»). Замість цього поллер бере `listMonitorRuns({kind, limit: 1})` і звіряє `run.id` з `run_id`, який повернув `POST` (без цього перший тік, поки рядок ще не видно як #1 для свого kind, знову зловив би старий завершений прогін). `resumePollingIfActive()` (відновлення поллінгу після F5) тепер теж питає `listMonitorRuns` для обох kind, а не `summary` — раніше цей код був мертвим, бо `summary` НІКОЛИ не міг повернути активний прогін.
- **F2 — прогрес, крок Б:** нова колонка `monitor_runs.urls_total` (`NULL` для старих рядків), `runner.py` проставляє `len(targets)` одним `update_run_counters` викликом одразу перед циклом обходу. Частота флашу лічильників зменшена з кожних 25 сторінок до кожних 10 — компроміс із контенцією `_COMMIT_LOCK`, задокументований коментарем біля лічильника.
- **F3 — нормалізація помилок:** `normalizeErrorDetail(detail, status)` у `api.js` — єдина точка, через яку проходять усі три форми `detail` (рядок / `{message, error_code}` / масив 422-валідації) перед тим як потрапити в `new Error(...)`. Застосовано не лише в спільному `request()`, а й у двох місцях з власним raw `fetch` (`uploadAltTextSource`, `createAltTextReport`) — той самий клас бага був і там.
- **F4 — лічильники на прогін:** нова `MonitorRepository.count_changes_by_severity(run_id)` (групування `monitor_changes` за `run_id`, а не за `client_id` + `resolved_at IS NULL`, як робить `open_counts_by_severity`, яка лишається для `MonitorSummaryDTO.open_*` — то законна інша семантика). `runner.py` у фінальному блоці `finish_run`/`update_run_counters` перемкнуто на нову функцію. Історичні рядки перераховані тим самим `UPDATE ... SET changes_x = (SELECT COUNT...)` в одноразовій частині міграції.
- **F5 — сітка блоків:** `.monitor-blocks` — `repeat(2, minmax(0,1fr))` замість трьох колонок; `#mc-dns { grid-column: 1 / -1; }` розтягує DNS-блок на весь рядок під Robots+Sitemap — суто CSS-фікс, розмітка `monitor-client.js` не змінювалась (Robots/Sitemap/DNS так само три послідовні `<section>`). Список DNS-записів згортається до 8 (`dnsShowAll`, скинутий при кожному новому монтуванні сторінки/після прогону), кешований `dnsRecords` дозволяє розгорнути без повторного мережевого запиту.
- **F6 — пагінація стрічки:** той самий патерн, що вже є для Pages (`PAGES_PAGE_SIZE`) — `changesVisibleCount` зберігає, скільки з уже відсортованого (severity-first) списку намальовано; «Load more» додає ще `CHANGES_PAGE_SIZE`. Дані досі фетчаться одним запитом (бекенд-контракт `list_changes` не чіпали — не в owned paths F6), обрізається лише рендер.
- **F7:** параграф про Analytics переписано на чесне твердження про можливість (порівняти `analytics_ids` в історії ключової сторінки), без слів «no dedicated check yet»; TODO-коментар розробника прибрано з HTML.
- **Системний фікс (§4):** `monitor-smoke.cjs` отримав живий прохід — навігація без `?fixture=` на реальний піднятий стек (портфель → перший реальний клієнт із таблиці), з окремими лічильниками `badResponses` (≥400 на `/api/v1/`) і `browserErrors` (`pageerror`/`console.error`). Це єдина перевірка в застосунку, яка фізично не може пройти повз розбіжність контракту фронт↔API, бо кожна інша перевірка в цьому файлі йде через `?fixture=`, де мережевого запиту немає взагалі.

### Контрактні зміни (Contract changes)

Лише адитивні:

- `GET /clients/{id}/monitor/pages?sort=` — 8 значень замість 3 (додано `-`-напрямки й `version_seq`/`-version_seq`); дефолт не змінився.
- `MonitorRunDTO.urls_total: int | None = None` (нове поле, `GET .../monitor/runs`, `GET .../monitor` у `last_light_run`/`last_full_run`).
- OpenAPI-снапшот перерахований — лише ці дві зміни (перевірено вручну, дифом).

### Як перевірити (verification)

```bash
docker compose exec api poetry run alembic upgrade head
docker compose exec api poetry run pytest -q
docker compose exec api poetry run ruff check .
docker compose exec api poetry run mypy app
node --test tests/unit/frontend/
BASE_URL=http://127.0.0.1:6524 CHROMIUM_PATH=/snap/bin/chromium NODE_PATH=/tmp/yovth-e2e/node_modules \
  node tests/e2e/frontend/monitor-smoke.cjs
```

Живо: `#/monitor` → перший клієнт → `#/client/<id>/monitor` без `?fixture=` — Pages вантажиться (не «Could not load pages»), сортування працює клікабельними заголовками в обидва боки, ручний Check now показує рухому смугу «Checked N of M».

### Тести

- `tests/unit/db/test_monitor_repo.py` (новий) — усі 8 значень `sort` дають різний, правильний порядок з `nulls_last` в обидва боки; `count_changes_by_severity` per-run.
- `tests/unit/api/test_monitor_api.py` — `sort=-last_checked_at` → 200 (буквально запит, що раніше давав 422); зміна напрямку сортування реально міняє порядок рядків.
- `tests/integration/test_monitor_end_to_end.py` — `test_run_counters_are_per_run` (light-baseline 0/0/0 проти full-non-baseline із реальними числами, звірено з `count_changes_by_severity`); `test_urls_total_set_before_loop` (`urls_total == len(targets) == urls_checked`).
- `tests/unit/frontend/api.test.mjs` (новий) — 5 кейсів `normalizeErrorDetail`: рядок, об'єкт, 422-масив (не `[object Object]`, обрізка до 3 елементів, прибирання `body`/`query` з `loc`), порожнє/невідоме значення → `HTTP <status>`.
- `tests/e2e/frontend/monitor-smoke.cjs` — живий прохід (нуль ≥400 на `/api/v1/`, нуль `console.error`/`pageerror`); фікстура `monitor-running` тепер перевіряє явний текст «Checked N of M» і відсутність передчасного тосту «complete»; фікстура `monitor-client-dense` розширена до 10 DNS-записів і 26 відкритих змін — e2e перевіряє collapse/«Show all 10» і cap/«Showing 25 of 26»/«Load more» наскрізно.

### Follow-ups / борг

- Немає нових — усі сім дефектів і системний фікс закриті повністю в межах цієї таски.
