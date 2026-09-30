# task-102 — Prompt Launcher: Page Framework і Blog Framework зі стратегії (посилання або шлях)

- **Пакет:** Tools hub → `#/tools/prompt-launcher` (розширення task-94)
- **Depends on:** task-94 ✅ (реєстр шаблонів, тул), task-90 ✅ (Google Drive/Sheets-клієнти),
  task-97 ✅ (`sheet_role`: page/blog/structure), task-71 ✅ (ролі), task-04 ✅ (клієнти)
- **Owned paths:**
  `app/prompts/registry.py` (адитивно: `kind`, два нові записи), `app/prompts/render.py` (адитивно),
  `app/prompts/strategy_source.py` (**новий**: посилання/шлях → рядки аркуша),
  `app/prompts/strategy_rows.py` (**новий**: розбір аркуша сторінок і блогів),
  `app/prompts/strategy_render.py` (**новий**: рядок стратегії → лаунчер),
  `app/api/tools_prompts.py` (адитивно: два ендпоінти), `app/schemas/prompts.py` (адитивно),
  `frontend/js/prompt-launcher-tool.js` (розгалуження за `kind`), `frontend/js/prompt-launcher-strategy.js` (**новий**),
  `frontend/js/api.js` (дві функції — адитивно), `frontend/css/prompt-launcher.css`, `frontend/index.html` (cache-bust),
  `tests/unit/prompts/`, `tests/unit/api/test_tools_prompts.py`, `tests/unit/api/data/openapi_snapshot.json`,
  `tests/e2e/frontend/tools-prompt-launcher-frameworks.cjs`
- **Read-only (лише імпорт):** `app/contentqa/readers/*`, `app/api/tools_strategy_package.py`,
  `app/integrations/google_api/*`, `app/core/security.py`
- **Міграцій БД:** немає. **Нових залежностей:** немає (`openpyxl` уже в образі).

---

## Мета

Сьогодні `Prompt Launcher` вміє один вид шаблону: «клієнт × домен» (TechAudit, Ahrefs Audit, GSC Audit) —
рядок таблиці = клієнт. `PageFramework.md` і `BlogFramework.md` працюють інакше: **одна сторінка (або одна
стаття) на запуск**, а вхідні дані вже написані стратегом у таблиці — Primary/Secondary/Longtail Key, H1,
Title, Description, Breadcrumbs, Comments; для блогу — тема, короткий опис, ключі, Commercial destination,
анкори й внутрішні лінки. Оператор зараз копіює це руками з Google Sheets у шапку промта, поле за полем,
для кожної з 11 сторінок і 12 статей квартального плану.

Тул мусить: **прийняти стратегію (посилання на Google Sheets або шлях до xlsx у AISeoTools), показати таблицю
сторінок/статей із неї й дати `Copy`, що кладе в буфер готовий, повністю заповнений лаунчер для одного рядка.**
Агент після вставки не питає нічого й не «доукладає» дані з таблиці сам.

---

## Дослідження (заміряно 2026-09-30 на живих даних, не з документації)

Джерело — таблиця з запиту (`GRP_SEOStrategy_Q42026_09072026`, shared drive, читається агентським
акаунтом без додаткових налаштувань) плюс 18 xlsx-стратегій у `AISeoTools/out/`.

1. **Обидва документи мають одну й ту саму «шапку-форму» з полями, які оператор заповнює**
   (`PageFramework.md:5-28`, `BlogFramework.md:10-40`). Порожнє поле в них — **легітимний стан**
   («Empty fields → you must discover/construct them»), на відміну від `URL` у TechAudit. Отже інваріант
   task-94 «відсутнє значення блокує рядок» тут **вужчий**: блокує лише те, без чого запуск неможливий
   (`CLIENT`, `INPUT`), а порожні `Title`/`H1`/… лишаються порожніми — **не вигадуються і не підмінюються**.
2. **Аркуш сторінок (`Q42026`) — один рядок = одна сторінка**, заголовки: `Page Status`, `Intent`, `Page Name`,
   `URL`, `New URL`, `Primary/Secondary/Longtail Key`, `H1`, `Title`, `Description`, `Breadcrumbs`, `comment`
   (у інших клієнтів `Comments`), `Framework / Copy Link`. `Page Status` у всьому портфелі: `OPTIMIZE PAGE`
   (74) і `NEW PAGE` (1); решта значень («Red Means Update»…) — рядки легенди кольорів, а не сторінки.
3. **`URL` ≠ `New URL` — це різні речі, а не дубль.** На 75 рядках вони різні в 50 (у більшості `New URL`
   порожній). Для `NEW PAGE` у `URL` стоїть текст `doesn't exist`, цільова адреса — лише в `New URL`;
   для `OPTIMIZE PAGE` із перейменуванням (`/contact/` → `/locations/`) жива сторінка — це `URL`.
   **`readers/tabular.py` мапить обидва заголовки в один тег `url`** (`_ALIASES`) і «останній перемагає» —
   для цього тула це дало б хибний `INPUT`, тому власний розбір, а не повторне використання `ContentItem`.
4. **`ContentItem` втрачає рівно ті поля, що потрібні лаунчеру:** зливає Primary/Secondary/Longtail у безіменний
   `keywords`-кортеж (порожній Primary зсуває Secondary на місце Primary), не знає `Page Status`, `Breadcrumbs`,
   `Comments`, `Commercial destination`, а блог-аркуш читає лише `title`/`description`/`page_name`.
   `tabular.py` — чужий owned path, тому поруч будується вузький власний розбір; спільні ядро/`sheet_role`
   переюзуються, а не копіюються.
5. **Аркуш блогів — одна стаття на кілька рядків** (заміряно: 95 рядків = 12 статей). Рядок із непорожнім
   `Article Topic` відкриває статтю; наступні рядки з порожнім топіком — її додаткові ключові слова
   (`Keyword`/`Volume`/`KD`) і пари `Anchor Text`/`Internal Link URL`. **Дві розкладки в портфелі:**
   GRP — по одній парі анкор/лінк у кожному рядку; GOREPORT/3XE/NYCHA — усі анкори однією клітинкою через
   `\n`. Розбір мусить вміти обидві й склеювати в один упорядкований список. Заголовки теж різняться:
   `Anchor Text`/`Anchors`, `Internal Link URL`/`Internal links`, `Frameworks`/`Framework`, і лише в частині
   клієнтів є колонка `Commercial destination` (у GRP Q4 її немає).
6. **Клітинка «Framework» уже каже, чи фреймворк зроблений** (у GRP Q4 заповнена на 11/11 сторінок і на
   частині статей; чіп-посилання на Google Doc, у порожніх — `—`). Це серверний, а не локальний факт про
   рядок — показуємо його бейджем, тож оператор не перезапускає вже зроблене.
7. **Коментар стратега — багаторядковий і має вигляд полів:** `WHY: …`/`GOAL: …` (сторінки), `WHY: …`/`Publish
   by: …` (блоги). Вставлений у шапку без відступу, цей текст читається як нові поля форми. Тому значення з
   переносами йдуть **окремими рядками з відступом під своєю міткою** (§3.3).
8. **Ролі й HTTP:** ендпоінти Prompt Launcher — GET, для всіх ролей. Читання стратегії — це запит до Google
   із тілом (`source` не вміщається у безпечний URL, а `GET /api/*` через тунель кешується годинами —
   пам'ять «Cloudflare static-asset cache bug»: зміни в таблиці не було б видно). Тому `POST`, а
   `required_role` для не-safe методу = `member` (`required_role` у [policy.py](app/core/auth/policy.py)); `viewer`
   отримає 403, і фронт має пояснити це текстом, а не «щось пішло не так».
9. **Шлях у контейнері ≠ шлях на хості** (та сама пастка, що в task-94 §1.3). Оператор дає шлях як бачить
   його на хості (`/home/elon/dev/seo/AISeoTools/out/…/GRP_BLOG_STRATEGY_Q42026.xlsx`) або відносний
   (`out/…`); бек читає лише через `aiseotools_source_path` (`/seo/AISeoTools`, `:ro`). Поза цим коренем
   файлів у контейнері просто немає — це межа не лише безпеки, а й реальності.

---

## Рішення (одним абзацом)

У реєстр додається другий **вид шаблону** — `kind="strategy"` (наявні три лишаються `kind="client"`, поведінка
і відповіді незмінні). Для нього бек: (1) визначає тип джерела — Drive-посилання/id чи шлях, (2) читає аркуші
(Sheets API grid або openpyxl), (3) власним розбором дістає **рядки сторінок або статті**, (4) визначає клієнта,
(5) **рендерить лаунчер на беку** й віддає готовим рядком, так само як у task-94 — фронт нічого не склеює.
Фронт: у наявному тулі при виборі такого шаблону з'являється поле «Strategy link or path» + `Load`, під ним
таблиця рядків стратегії з `Copy`.

---

## 1. Реєстр — `app/prompts/registry.py`

```python
class TemplateKind(StrEnum):
    client = "client"        # рядок = клієнт (наявні TechAudit / Ahrefs / GSC)
    strategy = "strategy"    # рядок = сторінка/стаття зі стратегії (цей таск)

@dataclass(frozen=True)
class PromptTemplate:
    ...                                   # усе наявне без змін
    kind: TemplateKind = TemplateKind.client        # адитивно, дефолт зберігає старі записи
    strategy_role: SheetRole | None = None          # "page" | "blog" — які аркуші пропонувати
```

Два нові записи (порядок = порядок у селекторі, після трьох наявних):
`page_framework` → `docs/PageFramework.md`, `strategy_role="page"`; `blog_framework` →
`docs/BlogFramework.md`, `strategy_role="blog"`. Поле `fields` у них порожнє: колонки таблиці задає `kind`.

`{{DOC_PATH}}` підставляється так само з `aiseotools_host_path` (хостовий шлях), `/source` і `doc_available`
працюють без змін — обидва файли лежать у `docs/`.

## 2. Джерело — `app/prompts/strategy_source.py`

```python
def load_strategy(source: str, *, settings, clients: GoogleClients | None) -> LoadedStrategy
```

Повертає `LoadedStrategy(name, kind: "gsheet"|"xlsx", ref, modified_time, sheets: list[RawSheet])`,
де `RawSheet = (title, rows: list[tuple[Cell, …]])`, `Cell = (text: str|None, links: tuple[str,…])`.

**Розпізнавання входу (детерміноване, без вгадування):**

| Вхід | Гілка |
|---|---|
| `extract_file_id(source)` знаходить id (URL `docs.google.com/spreadsheets/d/…`, `drive.google.com/…`, голий id) | Drive: `DriveClient.get_file` → mime `spreadsheet` ⇒ `SheetsClient.get_grid`; mime xlsx-blob ⇒ `download_media` + openpyxl; Google Doc ⇒ 400 `unsupported_source` («pick the strategy sheet, not a framework») |
| Інакше — схоже на шлях (починається з `/`, `~`, `out/` або закінчується на `.xlsx`) | Файл: §2.1 |
| Нічого з цього | 400 `unrecognized_source` — не пробуємо «схоже на що-небудь» |

Помилки Drive переюзовують коди task-90 без змін: `drive_not_configured` (503), `file_not_accessible` (404, з
підказкою «Share it with <акаунт>» — через `DriveClient.about_user`), `drive_error` (502).

### 2.1 Шлях

1. Хостовий префікс `aiseotools_host_path` → замінюється на `aiseotools_source_path`; відносний шлях
   резолвиться від `aiseotools_source_path`.
2. `safe_resolve(source_root, relpath)` (`PathTraversalError` → 400 `path_outside_aiseotools`) — та сама
   перевірка, що в `/source` task-94; розкриття симлінків не виводить за корінь.
3. Розширення лише `.xlsx`, розмір ≤ 10 МБ (той самий ліміт, що `MAX_XLSX_BLOB_BYTES`), файл існує —
   інакше 404 `strategy_file_not_found` із **хостовим** шляхом у повідомленні (контейнерний оператору нічого не каже).
4. Читання — `openpyxl(read_only=True, data_only=True)`; посилань у клітинках xlsx-гілка не має
   (`framework_link` = `null`, лише текст клітинки — це чесна межа, а не дефект).

### 2.2 `GET /api/v1/tools/prompts/strategy/local` — підказки для шляху

Список `out/*/*.xlsx` у корені AISeoTools за маскою `*STRATEGY*` (без `CHECK`/`MATRIX`/`before_*`/`reviewed`),
найновіші першими, ліміт 50: `{path_host, name, modified_at, size}`. Живлять `<datalist>` у полі вводу —
оператор не друкує 90-символьний шлях. **Не обов'язково для DoD**, але без цього «шлях» на практиці не
використовується (у `out/` 18 файлів з іменами на кшталт `GRP_PAGE_STRATEGY_18082026.xlsx` у різних папках).

## 3. Розбір і рендер

### 3.1 Аркуші — `strategy_rows.py`

Роль аркуша — `sheet_role(name, columns)` з task-97 (переюз). Пропонований аркуш для шаблону =
перший аркуш `strategy_role` цього шаблону, у якого знайдено заголовок рядка з ≥ 3 впізнаними колонками
включно з ключовою (`Page Name`/`Article Topic`). `Structure` і аркуші без розпізнаних колонок у список
вибору не потрапляють (вони є в `sheets[]` зі станом, а не зникають мовчки — та сама конвенція task-87).
Заголовок шукається по всьому аркушу (task-53 §2.3), допускається кілька блоків заголовків на аркуші.

**Власна таблиця аліасів** (нормалізація як `tabular._norm`: регістр, пробіли, кінцеве `:`):

| Тег | Заголовки |
|---|---|
| `status` | `page status` |
| `page_name` | `page name`, `page`, `pages` |
| `url` | `url`, `page url` |
| `new_url` | `new url` |
| `primary`/`secondary`/`longtail` | `primary key`/`primary keyword`, `secondary key`, `longtail key` |
| `h1`, `title`, `description`, `breadcrumbs` | те саме + `meta title`/`meta description` |
| `comments` | `comment`, `comments` |
| `framework` | `framework / copy link`, `framework link`, `framework`, `frameworks`, `copy link` |
| `topic` | `article topic` |
| `blurb` | `article short description` |
| `keyword` | `keyword`, `keywords` |
| `commercial` | `commercial destination` |
| `anchors` | `anchor text`, `anchors` |
| `internal_links` | `internal link url`, `internal links` |

Невідомі колонки (Volume, KD, Design, Dev, QA Completed, «Why this can win»…) ігноруються, а не хибно
розпізнаються. Нова назва заголовка в майбутній стратегії = один рядок в аліасах + тест.

### 3.2 Що рахується рядком

**Сторінка** (1 рядок аркуша → 1 запис). Рядок відкидається (не показується), якщо `Page Name` порожній
і `URL`/`New URL` порожні, або значення `Page Status` — не `OPTIMIZE PAGE`/`NEW PAGE` **і** решта поля порожні
(рядки легенди кольорів). `_NO_VALUE` (`-`, `n/a`, `no`, `none`, `—`) вважається порожнім — рівно та пастка,
що вже коштувала `0674487` (літерал `-` у клітинці URL).

Режим (`MODE`):
- `Page Status` = `NEW PAGE` → `new`; `OPTIMIZE PAGE` → `existing`;
- статус порожній/невпізнаний → за фактом: `URL` є валідним `http(s)` → `existing`, інакше → `new`.
  Статус завжди виграє, коли він є (фактом на `doesn't exist` не перебивається `NEW PAGE`).

`INPUT`: `existing` → **живий `URL`** (не `New URL`; §Дослідження 3); `existing` без валідного `URL` →
`blocked_reason: "existing page has no live URL"`. `new` → `Page Name`, а якщо порожній — `Primary Key`;
обидва порожні → блок.

**Стаття** (група рядків). Відкривається рядком з непорожнім `Article Topic`; закривається наступним таким
рядком, новим блоком заголовків або кінцем аркуша. Рядок із `Keyword`/анкором **до** першої статті — попередження
`orphan_rows`, а не мовчазна втрата. Ключі — у порядку появи (перший = Primary), дублікати без урахування регістру
злиті. Пари анкор↔URL збираються з обох розкладок (§Дослідження 5) і **зберігають порядок**; неповні пари
(анкор без URL чи навпаки) у лаунчер не йдуть, а лічильник `dropped_link_pairs` показується бейджем
«N incomplete link pairs skipped» — мовчки втрачені лінки були б рівно тим класом помилки, який тул мав прибрати.

Прапор `has_framework`: клітинка `framework` непорожня за `_NO_VALUE`; `framework_link` — перше посилання
клітинки (лише gsheet).

### 3.3 Лаунчер — `strategy_render.py`

Формат береться **дослівно з шапок документів**, порожні поля лишаються порожніми мітками:

```
Read /home/elon/dev/seo/AISeoTools/docs/PageFramework.md and run it end to end.

MODE:    existing
INPUT:   https://grillpointnyc.com/menu/queens/
CLIENT:  GRP
OUT_DIR: out/grp_menu-queens_2026-09-30_framework/
Data:
Primary Key: grill point queens menu

Secondary Key: kosher breakfast nyc

Longtail Key: grill point queens menu prices

H1: Grill Point Queens Menu

Title: Grill Point Queens Menu & Prices | Shawarma & Kebabs

Description: Full Grill Point Queens …

Breadcrumbs: Home > Menu > Queens

Comments:
  WHY: Clicks fell 26% (186 to 138) …
  GOAL: …
```

Блог: `MODE: blog`, `INPUT:` = тема, а `Data:` — `Article Topic`, `Article short description`, `Keywords`
(через кому, Primary першим, лише слова без Volume/KD — документ і так велить підтвердити об'єм в Ahrefs),
`Commercial destination`, `Title`, `Description`, `Anchors`/`Internal links` (**по одному на рядок, в одному
порядку** — не CSV: анкор із комою `Queens menu, prices` зламав би парування, а документ вимагає «paired in
order»), `DO NOT CHANGE:` (порожньо — у стратегіях такої колонки немає), `Comments:`.

Правила рендеру:
- **Значення з переносом рядка** (Comments) — під міткою, кожен рядок з відступом у 2 пробіли (§Дослідження 7).
  Тест-«золото» на реальному GRP-коментарі з `WHY:`/`GOAL:`.
- Керуючі символи (крім `\n`) з клітинок вирізаються; довжину значень не обрізаємо — обрізаний Title тихо
  збрехав би про вхід.
- **`Commercial destination` не вгадується.** Колонки немає (GRP Q4) → порожньо, документ сам вимагає від агента
  визначити money-сторінку (`BlogFramework.md` Step 1b.6). Перший внутрішній лінк «схожий на money» — це
  припущення, а поля цього блоку агент вважає авторитетними.
- **`New URL` для сторінок:** `MODE: existing` без перейменування — нічого; якщо `New URL` непорожній і ≠ `URL`
  або режим `new`, у кінець `Comments:` додається рядок `Target URL: <New URL>` (шапка документа не має
  окремого поля, а без цього цільову адресу агент не отримує взагалі).
- **`OUT_DIR`:** `out/<client_label_lower>_<slug>_<YYYY-MM-DD>_framework/` (сторінка) або `…_blog/`, як у наявних
  прогонах (`shb_gift-cards_2026-07-10_framework`, `shb_kosher-wedding-food-guide_2026-06-26_blog`). Slug:
  для `existing` — усі сегменти шляху URL через `-` (`menu/queens` → `menu-queens`, бо `locations/queens`
  інакше зіткнеться з ним), головна → `home`; для `new`/блогу — ASCII-slug (NFKD-згортання, без транслітерації кирилиці) від `Page Name`/теми, ≤ 60
  знаків. Дата — UTC на беку.
- **Клієнт (`CLIENT`)** — `render._client_label` (переюз: `tech_audit.client_label` → `ahrefs_client_label` → `Client.name`).

### 3.4 Визначення клієнта

Порядок, перше влучання виграє; кожне влучання повертає `resolved_by`, щоб UI не видавав здогадку за факт:
1. `client_id` у тілі запиту (ручний вибір оператора) → `manual`;
2. домен: найчастіший host серед `URL`/`New URL` рядків ↔ `Client.domain` (`www.` зрізаний) через
   `ClientRepository.get_by_domain` → `domain` (у GRP-таблиці — 11 з 11 на `grillpointnyc.com`);
3. перший `_`-токен імені файлу (`GRP_SEOStrategy…`, конвенція task-90 `_client_prefix`) без регістру ↔
   `client_label` будь-якого клієнта → `filename_prefix`;
4. нічого → `none`: `client: null`, **усі рядки `rendered=null`** з `blocked_reason: "client is not resolved — pick one"`,
   у UI — обов'язковий `<select>` клієнтів. Лаунчер без `CLIENT` запустив би агента на чужому/неіснуючому коді.

## 4. REST — `app/api/tools_prompts.py`

### 4.1 `POST /api/v1/tools/prompts/{key}/strategy` (member+)

```json
// запит
{"source": "https://docs.google.com/spreadsheets/d/1UKj…/edit?usp=sharing",
 "sheet": null, "client_id": null}
// відповідь
{"template_key": "page_framework", "generated_at": "…",
 "source": {"name": "GRP_SEOStrategy_Q42026_09072026", "kind": "gsheet",
            "ref": "1UKjvBsyiNxgPDpseDJxPdNjwZ78HYOYtcaeIuT8PmdQ",
            "modified_time": "2026-09-30T12:23:47Z", "web_view_link": "https://…"},
 "client": {"id": "…", "name": "Grill Point", "label": "GRP", "resolved_by": "domain"},
 "sheets": [{"name": "Structure", "role": "structure", "selectable": false, "row_count": 0},
            {"name": "Q42026", "role": "page", "selectable": true, "row_count": 11},
            {"name": "Blogs", "role": "blog", "selectable": false, "row_count": 12}],
 "sheet": "Q42026",
 "rows": [{"row_key": "https://grillpointnyc.com/menu/queens/", "ref": "Q42026!row 2",
           "title": "Queens Menu", "mode": "existing", "url": "https://grillpointnyc.com/menu/queens/",
           "primary_keyword": "grill point queens menu", "prefilled": ["primary","title","description","h1"],
           "has_framework": true, "framework_link": "https://docs.google.com/document/d/…",
           "warnings": [], "rendered": "Read …", "blocked_reason": null}]}
```

Один запит = одне читання Google (без серверного кешу: стратег правив таблицю хвилину тому, і оператор має
бачити правку — кнопка `Reload` у UI). `selectable` = аркуш має роль шаблону; `sheet=null` → пропонований.
Невідомий `sheet` → 400 `unknown_sheet` (не тихий «перший»). `rendered` ⇔ `blocked_reason is None`.

### 4.2 `GET /api/v1/tools/prompts/strategy/local` — §2.2

### 4.3 `PromptTemplateDTO` (адитивно)

`kind: "client" | "strategy"` (дефолт `"client"` — існуючі відповіді бачать нове поле, старий фронт його
ігнорує) і `strategy_role`. `PromptClientsDTO`/`/{key}/clients` для `kind=strategy` → 400 `wrong_template_kind`
(а не порожня таблиця), симетрично `/strategy` для `kind=client`.

Форми відповідей — у `app/schemas/prompts.py`, **оновити `openapi_snapshot.json`** (лише додані рядки —
перевірити `git diff --stat`, як у task-94).

## 5. Фронтенд

Обов'язково за `frontend/AGENTS.md`. Ключове, що тут реально кусає:

- **CSP:** жодних `style=` і `.style.*` — лише класи. **Cache-bust ланцюг:** `?v=` на новому
  `prompt-launcher-strategy.js` і на кожному ребрі до `index.html` (`prompt-launcher-tool.js`, `tools-page.js`,
  `router.js`, `api.js`) — пам'ять «Frontend module cache-bust chain»; тунель кешує `.js` на 4 години.
- **`api.js`:** `request-key` — явні призначення `"prompt-strategy"`, `"prompt-strategy-local"`, не похідні від
  параметрів (пастка, що вже спрацьовувала двічі в task-36).
- Таблиця — у `overflow-x:auto` з `contain: paint`.

### 5.1 Каркас (для `kind=strategy`)

1. Над таблицею — поле **«Strategy link or path»** (`<input list="pl-strategy-local">` + `Load`; у placeholder
   обидва приклади), спадний список локальних xlsx із §2.2. Введення запам'ятовується в `localStorage`
   (per-viewer зручність, у `try/catch`). `Enter` = `Load`.
2. Після завантаження — рядок-підсумок: назва стратегії (посилання на джерело), клієнт + `resolved_by`
   («matched by domain»), селектор аркуша (`Structure` показаний неактивним із причиною), `Reload`.
3. Таблиця: `Page`/`Article` · `Mode` (сторінка: `existing`/`new`) · `URL` · `Primary keyword` · `Prefilled`
   (наприклад `4 of 6` — підказка яких саме) · `Framework` (бейдж `Done` + посилання, або `—`) · `Copied` · `Copy`.
   Фільтр-перемикач `Hide rows that already have a framework` (вимкнений за замовчуванням).
4. `Copy` — той самий механізм, що task-94 §4.2 (`clipboard.writeText(row.rendered)`, fallback-`<textarea>`).
   Рядок із `blocked_reason` — кнопка вимкнена з `title`; попередження рядка — іконка з `title`.
5. `Prompt source` (згорнута панель) працює для нових шаблонів без змін.

### 5.2 Прогрес: **без** тижневого скидання

`localStorage` `yovth.prompt-launcher.<template>.<source_ref>` → `{rows: {<row_key>: <iso>}}`. У task-94 відмітки
скидались на новому ISO-тижні (понеділковий ритуал). Квартальний план стратегії — не понеділковий ритуал:
статті й сторінки робляться впродовж кількох тижнів, автоскидання посеред роботи стерло б відмітки. Скидає
лише `Reset` (по поточному джерелу). Ключ рядка — `row_key` (URL/slug теми), **не номер рядка**: стратег
додає рядки, номери зсуваються.

### 5.3 Стани (жоден не мовчазний)

| Стан | Що бачить оператор |
|---|---|
| `drive_not_configured` | Банер «Google Drive access is not configured» + пояснення |
| `file_not_accessible` | Текст бека з підказкою «Share it with <акаунт>» |
| `unrecognized_source` / `path_outside_aiseotools` / `strategy_file_not_found` | Текст помилки під полем; поле не очищається |
| 403 (`viewer`) | «Loading a strategy needs the member role» — не загальна помилка |
| У файлі немає аркуша потрібної ролі | «No page sheet found — this file has: Structure, Blogs» + селектор іншого шаблону |
| Клієнт не визначений | `<select>` клієнтів, доки не обрано — жоден `Copy` не активний |
| Порожній аркуш | `tools-empty` з поясненням, не порожня таблиця |
| Файл промта відсутній (`doc_available=false`) | Той самий банер, що в task-94 |

## 6. Безпека й інваріанти

- **Тільки Drive-ідентифікатори, ніколи довільні URL:** `extract_file_id` приймає лише Drive/Docs-форми та
  голий id; жодного `fetch(url)` за введеним — SSRF-класу немає за конструкцією.
- Шлях замкнений коренем AISeoTools + `.xlsx` + ліміт розміру (§2.1); клієнт ніколи не дає шлях до
  промта — `doc_relpath` лише з реєстру.
- Вміст клітинок потрапляє в промт агента **дослівно за задумом документів** (`Comments` — авторитетний
  вхід). Читати можна лише те, до чого має доступ агентський акаунт Google або що лежить у AISeoTools, тобто
  зовнішня людина не може підсунути чужу таблицю. Керуючі символи вирізаються, довжини не обрізаються.
- Журнал дій (task-72) фіксує `POST` з `source` — секретів у ньому немає (посилання/шлях), редакція не потрібна.
- `None` ≠ `0`/`""`: відсутнє поле форми лишається порожньою міткою (легітимно для цих шаблонів), а відсутнє
  **обов'язкове** (`CLIENT`, `INPUT`) блокує рядок.
- Збій читання однієї стратегії не впливає ні на що інше (тул stateless, нічого не персистує на сервері).

## 7. Чого ця таска НЕ робить

- **Не запускає** побудову фреймворка й не пише в таблицю (не ставить посилання у колонку `Framework`).
  Читання — `drive.readonly`/`spreadsheets.readonly`, як у task-90.
- **Не міняє** `app/contentqa/*`, `tools_strategy_package.py`, `tabular.py` (їхні аліаси не чіпаємо; §Дослідження 3–4).
- **Не редагує** значення полів перед копіюванням (нема редактора рядка) — правити треба в таблиці й натиснути
  `Reload`. Редактор — Follow-up.
- **Не завантажує** xlsx з браузера (multipart) — вхід лише «посилання або шлях», як просив оператор.
- **Не обробляє Google Docs** (сам фреймворк) як джерело — тільки таблиці.
- **Не створює міграцій** і не зберігає стратегії в БД.

## 8. Тести

Усе на **синтетичних мінімальних аркушах**, зібраних у тесті (не залежить від живих файлів); окремі золоті
кейси — з реальних форм, які зламали б наївний розбір.

- `tests/unit/prompts/test_strategy_rows.py` — заголовки-аліаси всіх заміряних варіантів; `URL`≠`New URL`
  (живий `URL` для `existing`, `doesn't exist` у `NEW PAGE`); літерал `-`/`—` як порожньо; рядки легенди
  кольорів відкидаються; блог: 95 рядків → 12 статей на GRP-фікстурі; **обидві розкладки анкорів** (по рядку
  й через `\n`); неповна пара → `dropped_link_pairs`, а не мовчазна втрата; `orphan_rows`; кілька блоків
  заголовків на аркуші; порожній Primary не зсуває Secondary.
- `tests/unit/prompts/test_strategy_render.py` — точний рядок лаунчера сторінки й статті (золото), відступ
  багаторядкового `Comments` із `WHY:`/`GOAL:`, `Target URL:` для `new`/перейменування, `OUT_DIR` (slug
  `menu-queens` ≠ `locations-queens`, головна → `home`), `Commercial destination` порожньо без колонки,
  `existing` без URL → `blocked_reason`, порожні `Title`/`H1` лишаються порожніми мітками.
- `tests/unit/prompts/test_strategy_source.py` — розпізнавання входу (URL різних форм, голий id, хостовий/
  відносний/контейнерний шлях, сміття → `unrecognized_source`); `../`-вихід і симлінк за корінь →
  `path_outside_aiseotools`; не-`.xlsx`; > 10 МБ. Google — через `FakeGoogle` (`tests/unit/integrations/google_api/fakes.py`).
- `tests/unit/prompts/test_client_resolution.py` — пріоритет `manual` > `domain` > `filename_prefix` > `none`;
  `none` блокує всі рядки.
- `tests/unit/api/test_tools_prompts.py` — форма `/strategy` і `/strategy/local`; `wrong_template_kind` в обидва
  боки; наявні три шаблони й їхні відповіді **байт-у-байт незмінні** (регресія на адитивність); `viewer` → 403,
  `member`/`admin` → 200 (`@pytest.mark.auth`).
- `tests/e2e/frontend/tools-prompt-launcher-frameworks.cjs` (Node Playwright, `.cjs`) — **живо**: вставити
  посилання з цього завдання → 11 рядків сторінок, `Copy` кладе в буфер `rendered`, відмітка переживає reload
  і **не** скидається зміною ISO-тижня; перемикання на `Blog Framework` дає 12 статей; шлях до
  `out/GRP_…_BLOG_STRATEGY_Q32026.xlsx` через datalist. **Пастка:** `127.0.0.1` під localhost-байпасом завжди
  автентифікує адміна — рольові перевірки лише юнітами.

Прогін — тільки в Docker:
`docker compose exec api poetry run pytest tests/unit/prompts tests/unit/api/test_tools_prompts.py tests/unit/api/test_openapi_snapshot.py -q`,
`… ruff check .`, `… mypy app`. Нових залежностей і міграцій немає, тож бекап БД не потрібен.

## 9. Definition of Done / Gate

1. `GET /api/v1/tools/prompts` віддає п'ять шаблонів; перші три — без жодної зміни в наявних полях.
2. `POST …/page_framework/strategy` на посиланні з завдання віддає 11 рядків `Q42026`, усі `mode=existing`,
   `client.resolved_by="domain"`, `has_framework=true`; `…/blog_framework/strategy` — 12 статей із
   впорядкованими ключами й парами анкор↔лінк.
3. Те саме з **локального шляху** (`out/GRP_grillpointnyc.com_2026-08-26_blog_strategy/GRP_BLOG_STRATEGY_Q32026.xlsx`,
   хостовий і відносний запис) і на xlsx із розкладкою анкорів через `\n` (GOREPORT).
4. **Жива вставка в агента:** скопійований лаунчер сторінки й статті **без жодного редагування** запускає
   відповідний фреймворк для одного клієнта (Primary Key/Title/Description із таблиці агент не перепитує і
   не перезаписує — це його власне правило «user-filled fields are authoritative»).
5. Рядок `NEW PAGE` (`19CLV`) дає `MODE: new`, `INPUT` = назва сторінки, `Target URL:` у `Comments`.
6. Нерозпізнаний клієнт не дає скопіювати жодного рядка; `viewer` бачить пояснення 403, а не «помилку».
7. Lint + mypy + `pytest` зелені **в контейнері**; фронт-чекліст `AGENTS.md §15–16` (CSP, теми, 400px, клавіатура).
8. План у `doc/dev/done/` із Completion report; рядок у `INDEX.md` позначено ✅; `task-94` не змінюється.

## 10. Follow-ups

- Редактор полів рядка над `Copy` (правка Title/H1 без виходу в таблицю) — поза обсягом; правильне місце —
  таблиця стратега, а не тул.
- «Copy all pending» — блок лаунчерів усіх рядків без фреймворка (як у task-94 §10).
- Deep-link `#/tools/prompt-launcher?template=…&source=…` із Strategy Package Check, щоб з перевіреної
  стратегії одразу переходити до запуску фреймворків.
- Запис посилання на готовий фреймворк назад у колонку `Framework` — вимагає write-scope Google; наразі
  свідомо read-only.

---

## ✅ Completion report

- **Дата:** 2026-09-30
- **Гілка / комміт:** feat/task-102-prompt-launcher-frameworks @ (див. `git log`)
- **Статус:** done-with-followups — не виконано лише DoD п.4 (жива вставка в агента, див. нижче)

### Що зроблено

`#/tools/prompt-launcher` має два нові шаблони — **Page Framework** і **Blog Framework**. Оператор вставляє
посилання на Google Sheets (або хостовий/відносний шлях до `.xlsx` у AISeoTools, з автопідказкою `out/*STRATEGY*.xlsx`),
бачить таблицю сторінок/статей зі стратегії й кнопкою `Copy` кладе в буфер готовий лаунчер: шапка-форма
документа, заповнена з таблиці. Клієнт визначається сам (домен рядків → префікс імені файлу → ручний вибір).
Перевірено живо на таблиці з завдання: 11 сторінок (усі `existing`, 11 з фреймворком) і 12 статей із 95 рядків;
на GRP/GOREPORT xlsx (обидві розкладки анкорів) і на `NEW PAGE` (19CLV).

### Як зроблено (ключові рішення)

- **`kind=strategy` у реєстрі** (`app/prompts/registry.py`): адитивне поле з дефолтом `client` — три наявні шаблони
  без змін. `launcher`/`fields` у нових порожні: шапка будується з рядка таблиці (`strategy_render.py`).
- **Власний розбір, не `ContentItem`** (`app/prompts/strategy_rows.py`): `tabular.py` мапить `URL` і `New URL` в один
  тег і втрачає `Page Status`/`Breadcrumbs`/`Comments`/`Commercial destination`. Спільним лишився `sheet_role`.
  Стаття блогу = група рядків; пари анкор↔URL збираються попарно **в межах рядка** (так однаково працюють GRP — по
  парі в рядку — і GOREPORT — усе через `\n` в одній клітинці); неповна пара не йде в лаунчер, але видима бейджем.
- **Джерело** (`strategy_source.py`): `http(s)://` → Drive (лише `extract_file_id`, ніякого fetch довільного URL),
  шлях → `safe_resolve` під `aiseotools_source_path`, лише `.xlsx`, ≤ 10 МБ; хостовий префікс перекладається в
  контейнерний. Помилки Drive — ті самі коди, що в task-90 (`drive_not_configured`, `file_not_accessible` з
  підказкою «Share it with …», `drive_error`).
- **Клієнт** (`render.py::resolve_client`) повертає `resolved_by`; без клієнта всі рядки `rendered=null` — лаунчер без
  `CLIENT` не копіюється.
- **`POST /{key}/strategy`** (member+; `viewer` → 403 із поясненням у UI): `GET /api/*` кешується тунелем.
- **Фронт:** окремий `prompt-launcher-strategy.js`, наявний тул лише розгалужується за `kind`. Відмітки `localStorage`
  без тижневого скидання, ключ рядка — `row_key`, не номер рядка.
- **Відхилення від плану:** (1) `recommended url` не в аліасах (потрібен був би лише `Structure`, яку й так не читаємо);
  (2) список клієнтів для `<select>` береться з наявного `GET …/techaudit/clients`, а не окремим ендпоінтом;
  (3) slug — ASCII-згортання без транслітерації (нова залежність не виправдана).
- **Знахідка живих даних:** клітинка `Primary Key` у 19CLV містить друге, службове, рядок `Vol 500 · KD 7` — лаунчер
  передає її дослівно (багаторядкове значення під міткою з відступом). Чи чистити такі рядки — рішення стратегів.

### Контрактні зміни (Contract changes)

Суто адитивні: `PromptTemplateDTO.kind`/`strategy_role`; нові `POST /api/v1/tools/prompts/{key}/strategy`,
`GET /api/v1/tools/prompts/strategy/local`; 400 `wrong_template_kind` на `/{key}/clients` для strategy-шаблонів.
**`openapi_snapshot.json` регенеровано повністю** — він відстав від task-100 (не було 14 маршрутів `/tools/indexing/*` і
їхніх DTO); семантичний diff — лише додавання, нічого не видалено, але textual diff великий.

### Як перевірити (verification)

```bash
docker compose exec api poetry run pytest tests/unit/prompts tests/unit/api/test_tools_prompts.py tests/unit/api/test_openapi_snapshot.py -q
docker compose exec api poetry run ruff check app tests/unit/prompts tests/unit/api/test_tools_prompts.py
docker compose exec api poetry run mypy app        # лишаються 4 помилки в app/cli.py (task-100), не з цієї таски
docker compose restart api                          # dev-контейнер без --reload: новий роут з'явиться лише після рестарту
NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 CHROMIUM_PATH=/snap/bin/chromium \
  node tests/e2e/frontend/tools-prompt-launcher-frameworks.cjs
```

### Тести

`tests/unit/prompts/` — `test_strategy_rows.py` (10: `URL`≠`New URL`, `—`/`-` як порожнє, легенда кольорів, обидві
розкладки анкорів, orphan-рядки, кілька блоків заголовків, Primary не зсуває Secondary), `test_strategy_render.py`
(золотий текст лаунчерів сторінки й статті, відступ `WHY:`/`GOAL:`, `Target URL:`, `OUT_DIR`), `test_strategy_source.py`
(розпізнавання входу, `../` і симлінк за корінь, не-xlsx, розмір, Drive-помилки), `test_client_resolution.py` (пріоритети);
`tests/unit/api/test_tools_prompts.py` (+8: форма відповідей, `wrong_template_kind` в обидва боки, коди помилок, ролі
403/200) і `tests/e2e/frontend/tools-prompt-launcher-frameworks.cjs` (живий браузерний прогін: копіювання в буфер,
переживання reload, Reset, блог, шлях, помилка на сміттєвому вводі, 390px). Тест task-94 (`tools-prompt-launcher.cjs`) лишається зеленим.

### Follow-ups / борг

- **DoD п.4 не виконано:** жива вставка скопійованого лаунчера в агента (запуск фреймворка «до кінця») не перевірялась —
  я перевірив лише, що в буфер потрапляє рівно `rendered` і що він відповідає формі шапки документа.
- Хвороба оболонки, не цієї таски: `.header-start` ширший за 390px viewport на **кожному** маршруті (виміряно на
  недоторканому TechAudit-вигляді) — тест меж мобільної ширини тому міряє лише піддерево тулу.
- Редактор полів рядка, «Copy all pending», deep-link зі Strategy Package Check, запис посилання назад у таблицю —
  див. §10.
