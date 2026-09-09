# task-90 — Strategy Package Check: Google Drive як джерело + QA Strategy / Frame / Copy + три звірки

- **Пакет:** третій тул у лейні Content QA. Оператор дає **одне** посилання на Стратегію (Google Sheet) або обирає файл на Диску — тул сам знаходить у стратегії посилання на док-файли фреймворків, читає в кожному вкладки **Frame** і **Copy**, і повертає **один звіт-пакет** із п'яти частин: QA Стратегії, QA Frame, QA Copy, звірка **Frame ↔ Strategy**, звірка **Frame ↔ живий сайт** і **Copy ↔ живий сайт**.
- **Хвиля:** P — Content QA (третій тул після task-53 Strategy Check / Framework Check, розширених task-87).
- **Depends on:** **53** ✅ (рушій `app/contentqa/**`), **87** ✅ (локалі, вибір аркуша, findings-панель), **83** ✅ (`GoogleAuth`/`GoogleApiSession` + bootstrap-скрипт), **61** ✅ (`inspector_key()`, `run_live_check`), **57** ✅ (`extract_signals`), **71/72** ✅ (роли, журнал)
- ⚠️ **М'яка залежність від task-88/89 (в роботі паралельною сесією, перевірено 2026-09-09):** `GoogleApiSession.get_json()` **уже написаний** там (`auth.py` рефакторнутий на спільний `_request(method, …)`, незакоммічено). Ця таска його **не пише вдруге** — лише споживає, і додає окремо тільки `get_bytes()` (медіа-завантаження `files.get?alt=media` віддає байти, а `_request` парсить JSON). Якщо task-88/89 ще не влиті — почати з ізольованої частини (§4.2–§4.6) і вертатись до `auth.py`, коли його `git status` чистий.
- **Owned paths:** `app/contentqa/package.py`, `app/contentqa/crosscheck.py`, `app/contentqa/site.py`, `app/contentqa/readers/{gdoc,gsheet,tabular}.py`, `app/integrations/google_api/{drive_client,docs_client,sheets_client}.py`, `app/api/tools_strategy_package.py`, `app/schemas/strategy_package.py`, `app/db/repositories/content_qa_packages.py`, `migrations/versions/*_content_qa_packages_task_88.py`, `frontend/js/strategy-package-tool.js`, `frontend/css/strategy-package.css`, `tests/unit/contentqa/test_{package,crosscheck,site,readers_gdoc,readers_gsheet}.py`, `tests/unit/integrations/google_api/test_{drive,docs,sheets}_client.py`, `tests/unit/api/test_tools_strategy_package.py`
- **Адитивні дотики (узгодити):** `app/integrations/google_api/auth.py` (**лише** +`get_bytes`; `get_json` приходить із task-88/89), `app/contentqa/readers/{xlsx,docx}.py` (винесення спільного ядра, поводження не змінюється), `app/contentqa/{model,rules,engine}.py` (+профіль `copy`, +`body` для Copy), `scripts/google_oauth_bootstrap.py` (+1 scope), `app/core/config.py`, `app/api/tools.py` (+1 запис у `_TOOLS`), `app/audit/actions.py` (+4), `app/db/models.py` (+1 модель, +1 колонка), `frontend/js/{tools-page,api,router}.js`, `frontend/index.html`, `tests/unit/api/data/openapi_snapshot.json`
- **PLAN.md:** §7 (секрети), §9.1 (ізоляція клітинки — тут «клітинка» = один фреймворк), §12 (чесні стани: `not_checked` ≠ «чисто»), §14
- **frontend/AGENTS.md:** §5 (правда даних), §6 (обов'язкові стани), §11 (resilience), §12 (vanilla + CSP), §14 (копірайт англійською), §15–16 (візуальний loop)
- **AISeoTools:** змін **не потребує** — `seo_audit.policy` як і раніше read-only консьюмер через `policy_bridge`
- **Нових Python-залежностей — нуль.** `httpx` уже пряма залежність (`app/integrations/google_api/auth.py`), `openpyxl` уже є, решта — stdlib. `pyproject.toml` не чіпається, запит до task-01 не потрібен.

---

## Мета (робота оператора, яку закриваємо)

Сьогодні, щоб перевірити один пакет «стратегія + фреймворки», оператор робить це руками:

1. Відкриває стратегію в Google Sheets, вручну скачує як `.xlsx`, кидає у Strategy Check.
2. Іде по кожному посиланню на фреймворк у стратегії, відкриває Google Doc, качає як `.docx`, кидає у Framework Check — **окремим прогоном на кожен документ**.
3. Вкладку **Copy** не перевіряє **ніяк** — чинний `read_framework_docx` свідомо ставить `body=None`, бо DOCX-експорт віддає лише першу вкладку (бриф), а готової прози в ньому немає.
4. Звірку «title у фреймворку = title у стратегії» і «те, що на сайті = те, що у фреймворку» робить очима, по одному полю.

Лейн H (`sync-title-drift`/`sync-h1-drift`/`sync-keyword-drift`) був **оголошений** у task-53 §3 і **не реалізований** — `grep -rn "sync-" app/contentqa/` дає нуль влучань. Тобто найдорожча частина перевірки — розходження між трьома артефактами — досі повністю на людині.

Ціль: одне посилання → один звіт, у якому машина за десятки секунд закриває всі чотири кроки, і жоден із них не має права тихо сказати «чисто», якщо він насправді не виконався.

---

## 1. Дослідження (факти, на яких стоїть план)

### 1.1 Доступ до Диска підключено й перевірено живо

Початковий замір (2026-09-09, `api`-контейнер): чинний токен мав лише `analytics.readonly` + `webmasters.readonly`, усі три виклики Drive v3 → 403 `insufficientPermissions`. Тобто інфраструктура OAuth уже працювала, бракувало тільки consent.

Виконано (§4.0a): увімкнено Drive/Docs/Sheets API, підтверджено audience **Internal**, у Workspace Admin дозволено внутрішнім застосункам restricted API, у `scripts/google_oauth_bootstrap.py:35` додано `drive.readonly`, перевидано refresh token. Замір **після**:

```
tokeninfo: 200
  scopes: analytics.readonly  drive.readonly  webmasters.readonly
drive/v3/about:       200  user.emailAddress = seo@theyovth.com   (квота 930 GB)
drive/v3/files:       200
drive/v3/drives:      200  один спільний диск: "THE YOVTH DRIVE" (0AGC3U5cQtZujUk9PVA)
```

**`seo@theyovth.com`** — та сама адреса, яку тул показує в тексті ремедіації для недоступних файлів (§1.3). Новий секрет у `.env` не з'явився: `client_id`/`secret` ті самі, перезаписано лише `GOOGLE_API_REFRESH_TOKEN`.

Заміряний масштаб корпусу: **3860** фреймворк-документів, **90** стратегій-таблиць. Файли лежать і на спільному диску, і в My Drive (`EFFYS_SEO_STRATEGY_061126` — my-drive, решта вибірки — shared) — тобто §1.6 (`corpora=allDrives`) не теоретичний, без нього частина файлів просто не знайдеться.

### 1.2 Один scope закриває всі три API

Перевірено по reference-сторінках методів: `drive.readonly` приймається і `documents.get`, і `spreadsheets.get`, і, звісно, Drive.

| Метод | Приймає `drive.readonly`? | Вужча альтернатива |
|---|---|---|
| `drive/v3/files.list`, `files.get`, `files.export` | так | `drive.metadata.readonly` (теж restricted) |
| `docs/v1/documents.get` | так | `documents.readonly` (sensitive) |
| `sheets/v4/spreadsheets.get` | так | `spreadsheets.readonly` (sensitive) |

**Рішення: додаємо рівно один scope — `https://www.googleapis.com/auth/drive.readonly`.** Причина: користувач обрав і вставку посилання, і **перегляд Диска в нашому UI**. Перегляд/пошук — це `files.list`, а він не працює під `documents.readonly`/`spreadsheets.readonly` і не працює під `drive.file`. Отже restricted scope все одно потрібен, і три вужчих поверх нього — це не менше прав, а лише довший рядок consent.

`drive.readonly` — **restricted** scope у класифікації Google. Для нашого випадку це безкоштовно: у застосунку з аудиторією **Internal** (одна Workspace-організація) scopes навіть не показуються на екрані consent і використання restricted/sensitive scope **не вимагає жодного review з боку Google**. Це той самий факт, на якому вже стоїть task-83: чинний refresh token не протухає саме тому, що застосунок Internal, а не «Testing».

### 1.3 Особистий Диск: три шляхи, два з них не працюють — і це доказово

Користувач сказав: файли лежать **змішано**, і корпоративні, і особисті. Розібрано всі варіанти.

| Шлях | Чи працює | Чому |
|---|---|---|
| **Поширити особистий файл (або теку) на агенційний акаунт** | ✅ **так** | Файл потрапляє у `sharedWithMe` і знаходиться через `corpora=allDrives`. **Нуль коду, нуль нових прав.** Це базовий шлях для особистих файлів. |
| **Ручний upload файлу (як сьогодні)** | ✅ **так** | Уже працює в Strategy Check/Framework Check і лишається як завжди доступний fallback. Новий тул теж приймає upload стратегії. |
| Другий OAuth-клієнт (External) для @gmail | ❌ **ні** | External + `drive.readonly` без верифікації = статус **Testing**, а там **refresh token протухає за 7 днів**. Для сервера без нагляду це означає «тул мертвий щопонеділка». Пройти верифікацію = CASA-аудит на restricted scope, тижні. |
| Google Picker + `drive.file` у браузері | ❌ **ні (для v1)** | Три незалежні блокери: (1) `drive.file` дає доступ **лише до файлів, які людина сама обрала** — а посилання на фреймворки ми знаходимо **всередині** стратегії, їх ніхто не обирав, тобто автопрохід за посиланнями структурно неможливий; (2) наш CSP — `default-src 'self'` (`app/core/security.py:30`), Picker тягне JS з `apis.google.com` і GIS з `accounts.google.com`, тобто треба послабити CSP усього застосунку; (3) потрібен окремий OAuth-клієнт типу **Web** (чинний — Desktop із loopback-редіректом), плюс персональний Google-логін кожного оператора поверх нашої власної трирольової авторизації (task-71). |

**Четвертий чесний стан замість обходу.** Якщо посилання веде на файл, якого агенційний акаунт не бачить (404/403 від Drive) — це `not_accessible` із точним текстом ремедіації, що містить адресу агенційного акаунта (беремо з `drive/v3/about?fields=user(emailAddress)`, один виклик, кешується на процес). Ніколи не «перевірено, чисто». Прогін решти фреймворків при цьому **не падає** (PLAN §9.1).

### 1.4 Вкладки Google Docs читаються ЛИШЕ через Docs API — експорт у DOCX не варіант

Це найважливіша знахідка дослідження, бо вона скасовує найдешевший на вигляд шлях («експортуй у .docx і згодуй наявному `read_framework_docx`»).

- `documents.get` має параметр **`includeTabsContent`**. Якщо `true` — вміст усіх вкладок приходить у `document.tabs[]`, а прямі текстові поля (`document.body`) лишаються **порожні**. Якщо параметр не передано — заповнюється `document.body` вмістом **лише першої вкладки**, а `document.tabs` порожній, і решта вкладок **не повертається взагалі**.
- Структура: `tabs[].tabProperties.{tabId,title,index}` + `tabs[].documentTab.body`, вкладені вкладки — `tabs[].childTabs[]` (дерево, треба обхід).
- **Drive `files.export` не вміє експортувати окрему вкладку**, і задокументованої гарантії «експорт містить усі вкладки» немає. Всередині документа `File → Download` віддає лише поточну вкладку; варіант «усі вкладки» пакує їх у ZIP окремими файлами. Ходовий обхід — недокументований `docs.google.com/document/d/{id}/export?format=pdf&tab={tabId}`, тобто рівно те, на що спиратися не можна.

**Наслідок для архітектури:** для фреймворк-документів **обов'язковий** Docs API з `includeTabsContent=true`. Побічний виграш, а не лише витрата: Docs API віддає `paragraph.paragraphStyle.namedStyleType` (`HEADING_1/2/3`), тобто справжню структуру заголовків — тоді як чинний DOCX-ридер мусить вигрібати рівень регуляркою з тексту `"H2: ..."` (`_HEADING_LINE_RE` в `readers/docx.py:24`). Для вкладки **Copy** це критично: там заголовки оформлені стилями, а не префіксом `H2:`.

#### 1.4a Заміряний зріз: 40 живих фреймворк-документів

Прогнано `documents.get?includeTabsContent=true` по 40 найновіших із 3860. Документація підтвердилась дослівно: при `false` → `tabs=0`, `body.paragraphs=100`; при `true` → `body.paragraphs=0`, увесь вміст у `tabs[]`.

**Вкладки називаються `Framework` і `Content`, а не `Frame`/`Copy`.** Слова з постановки задачі — розмовні; у файлах їх немає. Розподіл:

| Що знайдено | Скільки з 40 | Приклад |
|---|---|---|
| `'Framework'` + `'Content'` | **18** | `HWELL_PAGE_FRAMEWORK_...` → `'Framework'(5379ch) + 'Content'(4438ch)` |
| Одна вкладка `'Framework'` | 11 | `SHB_FRAMEWORK_MENU_PAGE_082726` |
| Одна вкладка `'Tab 1'` (дефолтна, не перейменована) | 11 | усі свіжі `SAPHNY_BLOG_FRAMEWORK_*` |

**Два різні шаблони брифу**, не один: у більшості `'Framework'` ≈ 5300 символів, але в `SDS_SEO_BLOG_FRAMEWORK_*` і `PJC_BLOG_FRAMEWORK_*` — **≈1420 символів** при `'Content'` на 9–11 тис. Тобто список підписів секцій із `readers/docx.py:_LABELS` не можна вважати універсальним — коротший шаблон треба перевірити окремо.

**`'Content'` буває порожньою — і це головна вимога чесності.** Заміряні розміри: `1ch` (лише перенос рядка) у `ALPHA_BLOG_..._CREWNECK_VS_HOODIE` і `3XE_FRAMEWORK_HOME_PAGE`; `322ch` у `SHB_FRAMEWORK_HOME_PAGE`; далі 1446 / 2977 / 3614 / 5674; справжня блогова проза — **8038–11256ch**. Прогнати readability/answer-first по вкладці на 1 символ означає віддати «0 порушень» там, де копії просто **ще немає**. Тому Copy QA має **поріг мінімальної довжини**, нижче якого повертає `not_checked` із причиною, а не порожній чистий звіт (PLAN §12). Поріг калібрується на цьому ж розподілі (DoD 4a).

Усі три HEADING-рівні (`HEADING_2/3/4`) у вкладці `Framework` присутні як справжні `namedStyleType` — тобто §1.4 щодо кращої структури підтверджено на живих даних.

### 1.5 Посилання у Google Sheets надійно читаються лише через Sheets API

Посилання на фреймворки в стратегії можуть бути оформлені щонайменше чотирма способами: звичайний текстовий URL у клітинці, клітинка-гіперпосилання, формула `=HYPERLINK()`, і **rich-text посилання на частині тексту клітинки** (наприклад лінк, навішений на назву сторінки).

- `spreadsheets.get?includeGridData=true` віддає для кожної клітинки `formattedValue`, `hyperlink` **і** `textFormatRuns[].format.link.uri` — тобто покриває всі чотири випадки, включно з кількома різними посиланнями в одній клітинці.
- Шлях «`files.export` у xlsx → наявний `read_strategy_xlsx`» відпадає для посилань: `read_strategy_xlsx` відкриває воркбук у `read_only=True` (`readers/xlsx.py`), а в цьому режимі openpyxl **не заповнює `cell.hyperlink`** узагалі; rich-text посилання на частині клітинки xlsx-експорт до того ж сплощує.
- Ліміт `files.export` — **10 МБ**; `spreadsheets.get` цього ліміту не має.
- Квоти Sheets API: **300 read-запитів/хв на проєкт**, 60/хв на користувача. Наше навантаження — 1 запит на прогін, тобто на три порядки нижче.

**Рішення:** нативний Google Sheet читається через **Sheets API**; справжній `.xlsx`-блоб, що лежить на Диску, читається через `files.get?alt=media` і йде в **наявний** openpyxl-ридер. Обидва шляхи зводяться до одного спільного ядра розбору таблиці (§2.2).

#### 1.5a Заміряно: пастка на 136 МБ і реальні форми посилань

**`includeGridData=true` БЕЗ `fields`-маски віддає 136 МБ на одну стратегію** (заміряно на `SHB_SEO_STRATEGY_082626`). З маскою — 272–624 КБ, тобто різниця в ~250 разів. Маска обов'язкова, не оптимізація.

Синтаксис маски має обмеження, на якому перший підхід упав `400 INVALID_ARGUMENT`: **вкладені підшляхи виду `format/link/uri` всередині `values(...)` не приймаються**. Робоча маска — пласкі імена полів, фільтрація вже в Python:

```
sheets(properties(title),data(rowData(values(formattedValue,hyperlink,textFormatRuns,userEnteredValue))))
```

Для скану лише посилань (без тексту) вистачає `values(hyperlink,textFormatRuns)` — 2–86 КБ на файл.

**Основна форма посилання на фреймворк — смарт-чип, а не гіперпосилання.** У колонці «Framework / Copy Link» лежить не URL, а вставлений `@`-згадкою чип файлу Диска. У Sheets API він живе в окремому полі `chipRuns[].chip.richLinkProperties.{uri,mimeType}`, якого **немає** ні в `hyperlink`, ні в `textFormatRuns`. Заміряний розподіл по 25 стратегіях: **`chip` — 148**, `hyperlink` — 61, `textFormatRun` — 0 (для Docs-цілей).

Практичний наслідок: `mimeType` чипа (`application/vnd.google-apps.kix` = Google Doc, `…spreadsheet`, `…presentation`) дозволяє відфільтрувати документи **без жодного звернення до Drive** — тобто крок `/inspect` коштує один виклик Sheets на весь воркбук.

Тому маска **обов'язково** містить `chipRuns`. Для дешевого пошуку-кандидатів (без тексту клітинок) достатньо:

```
sheets(properties(title),data(rowData(values(hyperlink,textFormatRuns,chipRuns))))
```

— заміряно 2–210 КБ на стратегію (SHB: 119 КБ, 44 чипи, 41 унікальний документ).

Решта форм (`plain text` URL у клітинці, `=HYPERLINK()`) лишаються в обробці як дешеві крайові випадки.

### 1.6 Shared Drives не знаходяться за замовчуванням

Для корпоративних файлів на спільних дисках:

- у **кожен** виклик (`files.get`, `files.list`, `files.export`) треба `supportsAllDrives=true`;
- для пошуку/переліку додатково `includeItemsFromAllDrives=true` **і** `corpora=allDrives` — без `corpora` дефолт `user`, тобто **лише My Drive**, і файли зі спільних дисків тихо не знаходяться (класична пастка: перегляд «працює», але половини файлів у списку немає).

### 1.7 Чого немає в наявному коді (заміряні дірки)

| Що потрібно | Що є сьогодні | Дірка |
|---|---|---|
| GET-запити до Google API | task-88/89 (паралельна сесія) уже додали `get_json()` через спільний `_request(method, …)`; до неї було **лише** `post_json` | лишається тільки +`get_bytes()` для `files.get?alt=media` — `_request` віддає розпарсений JSON, медіа треба сирими байтами |
| Лейн H (`sync-*`) | оголошений у task-53 §3 | **не реалізований** (нуль влучань `grep -rn "sync-"`) |
| Порівняння з живим сайтом | `run_live_check` + `extract_signals` дають title/description/h1/canonical/robots | **немає H2/H3/H4 і немає прози по секціях** — `PageSignals` має лише `h1` і плоский `visible_text` |
| QA готової прози (Copy) | `read_framework_docx` ставить `body=None` **свідомо** | вкладка Copy — це і є готова проза, для неї `body` треба заповнити, а профіль — окремий |
| Пакет із N документів | `content_qa_reports` = один рядок = **один** документ | потрібна батьківська сутність + місце для крос-findings, що не належать жодному документу |

`app/monitor/**` при цьому лишається **read-only**: `PageSignals` живить фінгерпринтинг і діфи Site Monitor, і додавання туди заголовків — це зміна в чужій owned-зоні з ризиком зсунути фінгерпринти. Свій екстрактор заголовків/секцій живе в `app/contentqa/site.py` і перевикористовує `fetch()` (SSRF-guard + `HostThrottle`) та `extract_signals()` для title/desc/h1 — щоб дві панелі ніколи не розійшлися в тому, що вважати title сторінки.

---

### 1.8 Посилання зі стратегії — основне джерело; пошук по імені — резерв

> **Виправлення.** Перша редакція цього розділу стверджувала, що посилань немає у 21 з 25 стратегій. Це був артефакт неповної `fields`-маски: скан не запитував `chipRuns`, а саме чипами посилання й оформлені (§1.5a). Числа нижче — після перескану з правильною маскою.

Проскановано 25 найновіших стратегій із 90. **Посилання на фреймворк-документи мають 9 із 25**, і це саме **актуальні** файли, а не архівні:

| Стратегія | Унік. документів | Форми |
|---|---|---|
| `SHB_SEO_STRATEGY_082626` | **41** | chip 44 |
| `SAPHNY_SEO_STRATEGY_032326` | 54 | hyperlink 40 + chip 15 |
| `EXXXOTICA_PAGE_STRATEGY_Q3_2026` | 27 | chip 27 |
| `HWELL_SEO_STRATEGY_060426` | 16 | chip 16 |
| `SAPHNY_SEO_STRATEGY_040826` | 16 | chip 10 + hyperlink 6 |
| `EFFYS_SEO_STRATEGY_061126` | 15 | chip 15 |
| `SDS_SEO_STRATEGY_040826` | 14 | chip 9 + hyperlink 8 |
| `LAXTON_SEO_STRATEGY_04142926` | 12 | chip 12 |
| `TYV_SEO_INTERNAL_CONTENT_PLAN` | 7 | hyperlink 7 |

Ті 16, де посилань немає, розпадаються на три зрозумілі класи, і **жоден із них не є контрприкладом**:

- **вихідні файли нашого ж тула** — `*_STRATEGY_CHECK_*`, `GOREPORT_STRUCTURE_CHECK_*` (це звіти Strategy Check, не стратегії; їх треба відсіювати за іменем);
- **архівні стратегії** — `SHB_SEO_STRATEGY_012726`, `SDS_SEO_STRATEGY_032026`, `3XE_SEO_STRATEGY_031926`, `GRP`, `19CLV`, `NYCHA`, `ANITA`, `PJC`: попередні редакції тих самих клієнтів, у яких колонки з фреймворком ще не було. Тобто практика **новіша**, а не занедбана;
- **заготовки** — `ALPHA_SEO_STRATEGY_V2_072826` (2 КБ, 12 рядків).

**Колонка називається «Framework / Copy Link»** і містить один чип на рядок сторінки — тобто прив'язка «фреймворк ↔ конкретний рядок стратегії» приходить задарма і одразу вмикає лейн H без жодного join'у.

Індекс колонки нестабільний **навіть у межах одного воркбука**: у `SHB_SEO_STRATEGY_082626` це колонка 12 на аркуші `Q3-Q4 2026 Strategy`, 14 на `Blogs Q3-Q4` і 15 на `Blogs`; у `SDS` — 17 і 12; у `SAPHNY` — 4. Це знімає будь-яку спокусу зашити позицію: до `_ALIASES` додаються `framework / copy link`, `framework link`, `framework doc`, `framework`, `copy link`.

**Роль другого джерела.** Пошук по Диску за конвенцією `<CLIENT_PREFIX>_*FRAMEWORK*` серед 3860 документів лишається в плані, але як **резерв і доповнення**, а не як рівноправний шлях:

1. покриває стратегії без колонки (`ALPHA`) і старі редакції;
2. ловить фреймворки, які **створили, але ще не вписали** в стратегію — а це сам собою корисний finding (`sync-frame-not-in-strategy`, лейн H);
3. дає крос-перевірку: документ, на який стратегія посилається, але якого пошук за іменем не знаходить, — сигнал, що файл перейменували або він поза конвенцією.

У списку кандидатів кожен запис показує походження (`linked from Q3-Q4 2026 Strategy!row 2, col «Framework / Copy Link»` / `found on Drive by name`), і за замовчуванням обрані ті, що прийшли з посилань.

---

## 2. Рішення (архітектура)

```
 посилання / файл з Диска / upload
              │
   ┌──────────▼──────────────────────────────────┐
   │ google_api/drive_client.py                  │
   │  files.get(fileId, supportsAllDrives)       │  → name, mimeType, owners, webViewLink
   │  files.list(corpora=allDrives, q=…)         │  → перегляд/пошук
   └──────────┬──────────────────────────────────┘
     mimeType розводить на три шляхи
              │
  ┌───────────┴────────────┬──────────────────────┐
  │ spreadsheet            │ .xlsx блоб            │ (документ як стратегія — 400)
  │ sheets_client.get      │ files.get?alt=media   │
  │  includeGridData       │                       │
  └───────────┬────────────┴───────────┬───────────┘
              │                        │
     readers/gsheet.py          readers/xlsx.py (наявний)
              └──────────┬─────────────┘
                         │
              readers/tabular.py  ← спільне ядро блоків/колонок (винесене)
                         │
                  ContentDoc(strategy)  ──► engine.run_checks ──► QA Strategy
                         │
              знайдені посилання на Google Docs
                         │   (rich-text + hyperlink + текстові URL, §1.5)
                  bounded fan-out (4 воркери, cap 25)
                         │
      docs_client.get(docId, includeTabsContent=true)
                         │
            вкладки за назвою (нормалізовано)
              ┌──────────┴──────────┐
        tab "Frame"            tab "Copy"
      readers/gdoc.py         readers/gdoc.py
      (секції брифу,           (проза, body=текст,
       body=None)               заголовки зі стилів)
              │                      │
    run_checks(frame)        run_checks(copy)  ──► QA Frame / QA Copy
              │                      │
              └──────────┬───────────┘
                         │
             contentqa/crosscheck.py + contentqa/site.py
              ├─ H: Frame ↔ Strategy      (join за inspector_key(URL))
              ├─ I: Frame ↔ живий сайт    (fetch + власний екстрактор)
              └─ J: Copy  ↔ живий сайт    (секція-в-секцію, SequenceMatcher)
                         │
                  PackageResult ──► content_qa_packages (+ N рядків content_qa_reports)
```

### 2.1 Рушій не дублюється

`app/contentqa/engine.py:run_checks(doc, profile)` викликається **як є** для стратегії, Frame і Copy. Усе, що додається до рушія, — новий `Profile.kind = "copy"` і те, що для Copy `ContentItem.body` уперше не `None`, тобто вже написані лейни D/E/F (slop, brand voice, readability, answer-first, section-length) починають працювати на прозі **без жодного нового правила**. Це і є причина, чому Copy QA дешевий: правила є, їм просто ніколи не давали текст.

### 2.2 Спільне ядро табличного розбору

Логіка «знайти рядок-заголовок із ≥2 впізнаними колонками, порізати аркуш на блоки, зібрати `ContentItem`» живе у `readers/xlsx.py` і зав'язана на openpyxl-рядки. Виноситься в `readers/tabular.py` із входом `list[tuple[object, ...]]` + назва аркуша; `xlsx.py` і `gsheet.py` стають двома тонкими адаптерами до нього.

Це **не переписування**: `_ALIASES`, `_HEADER_MIN_SCORE`, `_LOAD_BEARING`, `_SheetScan`, три стани аркуша з task-87 переносяться дослівно. Гейт на це — наявні `tests/unit/contentqa/test_readers_xlsx.py` мають лишитися зеленими **без правок**; якщо тест довелося змінити, значить поводження зсунулось і рефактор неправильний.

### 2.3 Вкладки за назвою, а не за індексом — назви заміряні, не вгадані

Живі назви (§1.4a) — **`Framework`** і **`Content`**. Слова «Frame»/«Copy» з постановки в файлах не трапляються жодного разу, тому вони йдуть у список синонімів, а не в основу зіставлення:

- Frame ← `framework`, `frame`, `page framework`, `brief`
- Copy ← `content`, `copy`, `copywriting`

Зіставлення за нормалізованою назвою (`strip().lower()`, згорнуті пробіли), **не за індексом**: порядок вкладок — питання зручності автора, і зав'язка «нульова = Frame» зламається на першому ж документі з вкладкою попереду.

Три заміряні стани, кожен зі своєю поведінкою:

| Стан документа | Частка (з 40) | Поведінка |
|---|---|---|
| `Framework` + `Content` | 18 | обидва QA виконуються |
| лише `Framework` | 11 | Frame QA виконується, Copy → `not_checked` («no Content tab») |
| лише `Tab 1` (дефолтна назва) | 11 | читається як Frame + `info`-finding «tab is unnamed»; Copy → `not_checked` |

**Порожня вкладка `Content` — окремий стан, не «чисто».** Заміряний розподіл довжин (1 / 322 / 1446 / 2977 / 3614 / 5674 / 8038…11256 символів) показує, що `1ch` і сотні символів — це «копію ще не написали», а не «копія без помилок». Нижче порогу Copy QA повертає `not_checked` із причиною і фактичною довжиною.

Дерево `childTabs` обходиться в глибину; підвкладки з тими самими іменами враховуються (`ref` містить шлях: `MyDoc › Page A › Content`).

### 2.4 Три звірки: join і чесність

**Ключ join'у — `inspector_key(url)`** з `app/urlinspector/urlkey.py`, не сирий рядок URL. Причина заміряна раніше на цьому ж проєкті (`per-url-data-join-gotchas`): відсутній/наявний кінцевий слеш губить до 96% зіставлень. Резервний ключ — нормалізована назва сторінки, і **лише** якщо URL відсутній хоч на одному боці; якщо не зійшлось ні за URL, ні за назвою — це `sync-url-unresolved` (warn) і **пара не перевіряється**, а не «збігається».

Кожна звірка має власний тристан `CrossCheckInfo(name, state, reason)` — `checked` / `not_checked` / `not_applicable` — і рендериться окремим блоком **Scope** у звіті, тим самим патерном, що `sheet_states` із task-87. Порожній список findings у звірці зі станом `not_checked` **ніколи** не малюється як зелена галочка.

### 2.5 Ізоляція фреймворка

Один фреймворк = одна «клітинка» в сенсі PLAN §9.1. Його збій (недоступний файл, таймаут Docs API, антибот на сайті, зламана структура) дає `not_accessible`/`not_checked` для цього фреймворка і **не валить** ні інші фреймворки, ні QA стратегії, ні прогін. Між фреймворками — паралельно (`contentqa_package_workers`, дефолт 4); всередині одного фреймворка — послідовно (`documents.get` → live fetch), бо це та сама доріжка.

Живі фетчі всіх фреймворків одного клієнта йдуть в **один хост**, тому `HostThrottle` тут не формальність: без нього 10 паралельних запитів у сайт клієнта виглядають як мінімальний скан.

### 2.6 Раннер — чиста функція, не хендлер

`app/contentqa/package.py:run_package(...) -> PackageResult` не знає про FastAPI, `Request` і сесію БД: на вході — резолвлені джерела й профіль, на виході — результат. Причина конкретна: повний прогін на 10 фреймворків — це ~1 виклик Sheets + 10 Docs + до 20 live-фетчів, і **чи влазить це в синхронний запит — питання заміру, а не здогадки** (§4.7 / DoD 9). Якщо замір скаже «не влазить», перенесення в job-рушій буде тонким адаптером навколо тієї самої функції, а не переписуванням.

Перший інкремент — синхронно (як усі наявні тули, через `run_in_threadpool`), із двофазним UX `/inspect` → `/check` (той самий патерн, що task-87 і `tools_alt_text.py`), жорстким `cap` і per-framework дедлайном.

---

## 3. Каталог нових правил

Severity як у task-53: **`error`** — назовні віддавати не можна; **`warn`** — потрібне рішення людини; **`info`** — nudge.

### Лейн H — Frame ↔ Strategy (`sync-*`)

| ID | Правило | Sev |
|---|---|---|
| `sync-title-drift` | Title у Frame ≠ title у рядку стратегії для того самого URL | **error** |
| `sync-desc-drift` | Те саме для meta description | **error** |
| `sync-h1-drift` | Те саме для H1 | **error** |
| `sync-keyword-drift` | Primary keyword розійшовся | **error** |
| `sync-intent-drift` | Search intent розійшовся | warn |
| `sync-frame-not-in-strategy` | URL фреймворка не має рядка в стратегії | warn |
| `sync-strategy-row-no-frame` | Рядок стратегії не має посилання на фреймворк | info |
| `sync-url-unresolved` | Пара не зійшлась ні за URL, ні за назвою → **не перевірено** | warn |

### Лейн I — Frame ↔ живий сайт (`site-*`)

| ID | Правило | Sev |
|---|---|---|
| `site-unreachable` | Фетч не вдався / не 2xx → **`not_checked`**, не дефект контенту | info |
| `site-blocked` | Антибот/403 → **`not_checked`** | info |
| `site-title-drift` | `<title>` сторінки ≠ title у Frame | **error** |
| `site-desc-drift` | `<meta description>` ≠ Frame | **error** |
| `site-h1-drift` | H1 сторінки ≠ H1 у Frame | **error** |
| `site-heading-missing` | Заголовок H2/H3 із Frame на сторінці відсутній | **error** |
| `site-heading-extra` | Заголовок на сторінці, якого нема у Frame | info |
| `site-heading-order` | Набір той самий, порядок інший | warn |
| `site-internal-link-missing` | Цільовий внутрішній лінк із Frame зі сторінки не проставлений | warn |
| `site-noindex` | Сторінка `noindex`, хоча стратегія веде її як живу | **error** |
| `site-canonical-mismatch` | Canonical ≠ URL фреймворка | warn |

### Лейн J — Copy ↔ живий сайт (`published-*`)

| ID | Правило | Sev |
|---|---|---|
| `published-section-missing` | Заголовок секції з Copy на сторінці не знайдено | **error** |
| `published-copy-drift` | Схожість тексту секції нижче порогу (у повідомленні — %, уривок обох боків) | warn |
| `published-copy-extra` | Секція на сторінці без відповідника в Copy | info |
| `published-not-published` | Жоден заголовок із Copy на сторінці не знайдено → скоріш за все опубліковано стару версію | warn |

Порівняння секції — `difflib.SequenceMatcher` по нормалізованому тексту (згорнуті пробіли, зняті нерозривні пробіли/soft hyphen тим самим `_INVISIBLE_RE`-підходом, що `pageinventory/extract.py`). **Порогу не вигадуємо**: початкове значення 0.90, і воно калібрується на ≥3 живих опублікованих сторінках, а вибране число записується в Completion report (DoD 7). Причина — пряма аналогія з task-87, де банд читабельності LIX 30–45 був відкалібрований на 141 живому описі, а не взятий із голови.

Літерального diff усієї сторінки **не робимо** — навігація, футер, cookie-банер і блоки перелінковки дадуть шум, який поховає справжній дрейф. Порівняння тільки секція-в-секцію, зіставлену за заголовком.

### Copy QA — новий профіль, нуль нових правил

`Profile.kind="copy"` + заповнений `ContentItem.body` вмикає наявні лейни: `readability-grade` (Flesch–Kincaid або LIX за локаллю, task-87), `answer-first-missing` (40–80 слів), `section-over-300-words`, `eeat-signal-missing`, `title-case-inconsistent`, `voice-em-dash`/`voice-locale-mismatch`/`voice-word-swap`, `slop-*`, `internal-links-*`.

⚠️ **Anti-rules із task-53 §3.F лишаються забороненими** і на Copy: жодних keyword-density таргетів, transition-word percentage, passive-voice gates, tone-analyzer. Це вимога документа-джерела, не пропуск.

⚠️ **Пастка локалі (жива, з task-87).** Шведський воркбук дав 136 хибних `voice-em-dash` на коректному tankstreck. Copy — це довгий текст, тобто на ньому та сама помилка коштує вже не 136, а тисячі findings. Локаль на Copy береться з того самого профілю, і DoD 4 явно вимагає перевірити відсутність хибних locale-findings.

---

## 4. Етапи

| # | Що | Вихід / гейт |
|---|---|---|
| **4.0** | ✅ **Зроблено 2026-09-09.** Передумови поза кодом (§4.0a) виконані, `drive.readonly` доданий у `scripts/google_oauth_bootstrap.py:35`, refresh token перевиданий | Проба показує три scope; `drive/v3/about` → **200**, `seo@theyovth.com`; `files`/`drives` → 200. **DoD 1 закритий** |
| **4.1** | `GoogleApiSession.get_bytes()` — адитивно, та сама політика 401→refresh→retry, 429/5xx backoff, редагування повідомлень (`get_json` уже є з task-88/89, переписувати заборонено). `drive_client.py` (`get_file`, `list_files`, `download_media`, `about_user`), `docs_client.py` (`get_document(include_tabs=True)` + обхід дерева вкладок), `sheets_client.py` (`get_grid` із `fields`-маскою на `formattedValue`/`hyperlink`/`textFormatRuns`) | Юніти на фейкових `httpx.MockTransport`; тест, що `supportsAllDrives`/`corpora=allDrives` реально йдуть у query; **тест, що Sheets-виклик завжди несе `fields`-маску** — без неї заміряні 136 МБ на файл (§1.5a) |
| **4.2** | `readers/tabular.py` — виніс спільного ядра. `readers/gsheet.py` — Sheets-грід → ті самі блоки + витяг **усіх** посилань клітинки (hyperlink, textFormatRuns, `=HYPERLINK()`, текстовий URL) | `tests/unit/contentqa/test_readers_xlsx.py` зелений **без правок**; новий тест на всі форми посилання, **включно з `chipRuns`** (заміряно: чипи — 148 із 209, основна форма); alias-колонка «Framework / Copy Link» знаходиться на індексах 12/14/15 у трьох аркушах ОДНОГО воркбука SHB і 17/12/4 в інших — позицію зашивати не можна |
| **4.3** | `readers/gdoc.py` — Docs JSON → параграфи + `namedStyleType`; секційний розбір спільний із `docx.py`; два режими: `frame` (секції брифу, `body=None`) і `copy` (проза, `body` заповнений, заголовки зі стилів) | Фікстури зі **справжніх** документів трьох заміряних станів (§2.3): `Framework`+`Content`, лише `Framework`, лише `Tab 1`; плюс обидва шаблони брифу (≈5300 і ≈1420 символів) — короткий шаблон перевіряється окремо, `_LABELS` із `docx.py` на ньому не гарантований |
| **4.4** | `contentqa/site.py` — live-фетч через наявні `fetch()`+`HostThrottle`+`assert_public_http_url`, `extract_signals()` для title/desc/h1/canonical/robots, **власний** екстрактор H2–H4 + прози по секціях | Юніти на збереженому HTML; тест, що недоступна сторінка дає `not_checked`, а не порожньо-чисто |
| **4.5** | `contentqa/crosscheck.py` — лейни H/I/J + `CrossCheckInfo` тристани | Юніти на кожне правило, включно з «не зійшлось за ключем» |
| **4.6** | `contentqa/package.py` — `run_package()`: резолв джерела, QA стратегії, **пошук фреймворків із двох джерел** (посилання зі стратегії + пошук по Диску за префіксом клієнта, §1.8), bounded fan-out, звірки, збірка `PackageResult`; ізоляція збою одного фреймворка | Юніт: із 3 фреймворків один падає — прогін завершується, два перевірені, один `not_accessible` |
| **4.7** | БД (§6) + `app/api/tools_strategy_package.py` + `app/schemas/strategy_package.py` + `_TOOLS` + audit-actions. **Замір повного прогону** на реальній стратегії користувача | Час прогону записаний; рішення «синхронно / у job» ухвалене на замірі |
| **4.8** | Фронт: `strategy-package-tool.js` + `strategy-package.css`, findings-панель перевикористовується з `content-qa-render.js` | Візуальний loop AGENTS.md §15–16 на 1440/1024/768, усі 5 станів |


### 4.0a Передумови поза кодом — точні лінки (проєкт `449132293218`)

Номер проєкту взято з префікса `google_api_client_id` у `.env` — саме той проєкт, якому вже належить чинний GSC/GA4 refresh token, тобто нового проєкту створювати не треба.

**Крок 1. Увімкнути три API.** Без цього 403 буде `accessNotConfigured` (тобто «API вимкнений»), а не про scope, і це збиває з пантелику.

- Drive API → https://console.cloud.google.com/apis/library/drive.googleapis.com?project=449132293218
- Docs API → https://console.cloud.google.com/apis/library/docs.googleapis.com?project=449132293218
- Sheets API → https://console.cloud.google.com/apis/library/sheets.googleapis.com?project=449132293218
- Перевірка, що всі три в списку → https://console.cloud.google.com/apis/dashboard?project=449132293218

**Крок 2. Перевірити audience = Internal.** На цьому тримається вся економіка §1.2 (restricted scope без review) і те, що токен не протухає за 7 днів.

- https://console.cloud.google.com/auth/audience?project=449132293218

**Крок 3 (для Internal — НЕ обов'язковий).** Екран Data access потрібен лише застосункам, якими користуються люди поза організацією: «for apps used only internally by your Google Workspace organization, scopes aren't listed on the consent screen». Тобто scope можна просто запитати з коду. Лінк — якщо все ж захочеться зафіксувати перелік явно:

- https://console.cloud.google.com/auth/scopes?project=449132293218

**Крок 4. ⚠️ Workspace Admin: довірити внутрішні застосунки.** Це найнеочевидніший крок і єдиний, що робиться **не** в Cloud Console. Drive належить до **restricted Google Workspace services**, і навіть Internal-застосунку організації доступ до нього окремо гейтить адмін домену. Без цього кроку consent на `drive.readonly` не пройде, попри правильні API і правильний scope.

- Admin console → **Security → Access and data control → API controls** → https://admin.google.com/ac/owl
- Потрібно або поставити прапорець **«Trust internal apps»** у Settings, або виставити цьому OAuth-клієнту рівень доступу **Trusted** у списку застосунків: https://admin.google.com/ac/owl/list?tab=configuredApps
- Вимагає прав **адміністратора Google Workspace**, не лише доступу до GCP-проєкту.

**Крок 5. Додати scope у bootstrap** (`scripts/google_oauth_bootstrap.py:35`):

```python
SCOPES = (
    "https://www.googleapis.com/auth/webmasters.readonly "
    "https://www.googleapis.com/auth/analytics.readonly "
    "https://www.googleapis.com/auth/drive.readonly"        # task-90
)
```

**Крок 6. Перевидати refresh token** (скрипт сам перезаписує рядок у `.env`, наявні scope GSC/GA4 зберігаються, бо перелічені разом):

```bash
docker compose exec api poetry run python scripts/google_oauth_bootstrap.py
```

**Крок 7. Перевірити результат** — тією самою пробою, що дала факти §1.1: `tokeninfo` мусить показати три scope, а `drive/v3/about?fields=user(emailAddress)` → **200** з адресою агенційного акаунта (її ж потім показуємо в тексті ремедіації для недоступних файлів).

**Окремо, для особистих файлів (§1.3).** Прав у консолі тут не додають взагалі — доступ дається на боці самого файлу: у Google Drive відкрити **Share** на файлі (або, краще, на теці зі стратегіями) і додати адресу агенційного акаунта з кроку 7 як **Viewer**. Після цього файл знаходиться через `corpora=allDrives` разом із корпоративними.

---

## 5. API-контракт (новий, адитивний)

Префікс `/api/v1/tools/strategy-package`. Роль: мутації — `member`+ (щоденна робота по одному клієнту, не батч); читання звітів — усі роли, включно з `viewer`. Чотири POST'и реєструються в `app/audit/actions.py`.

| Метод | Шлях | Що робить |
|---|---|---|
| `POST` | `/resolve` | `{"url": "..."}` або `{"file_id": "..."}` → метадані файлу (name, mimeType, owner, webViewLink, `kind: spreadsheet\|xlsx_blob\|document\|unsupported`). Витяг id з усіх живих форм URL: `/spreadsheets/d/<id>`, `/document/d/<id>`, `/file/d/<id>`, `?id=<id>`; `#gid=` і `?tab=t.<id>` зберігаються як підсказка вибору |
| `GET` | `/drive/files` | Перегляд/пошук: `?q=&folder_id=&page_token=&page_size=`. Завжди `supportsAllDrives=true&includeItemsFromAllDrives=true&corpora=allDrives`. Віддає теки й файли типів spreadsheet/xlsx/document |
| `POST` | `/inspect` | Читає **лише стратегію**. Віддає: аркуші з трьома станами (як task-87) + **список знайдених посилань на фреймворки** з зазначенням, з якого аркуша/рядка/клітинки кожне взято, і чи файл доступний. Stateless, без персисту |
| `POST` | `/check` | Повний прогін. Вхід: джерело, обрані аркуші, обраний підмножина фреймворків, `cross_sheet`, профіль (client_id, kind, intent, locale, brand_names), прапорці `check_site` / `check_published` |
| `GET` | `/reports` | Пагінований список пакетів |
| `GET` | `/reports/{id}` | Деталі: 5 частин + Scope-блок зі станами аркушів і звірок |

Усі повідомлення про помилки — **англійською** (CLAUDE.md: обмін фронт↔бек англійською), у наявній формі `ErrorDTO(error_code, message)`. Коди: `invalid_drive_url`, `drive_not_configured` (немає scope/refresh token), `file_not_accessible`, `unsupported_mime_type`, `too_many_frameworks`, `sheet_not_found` (наявний `UnknownSheetError`).

`drive_not_configured` — це окремий чесний стан, не 500: якщо scope не доданий, тул каже це прямим текстом і показує команду bootstrap, замість того щоб віддати 403 Google як внутрішню помилку.

---

## 6. БД і міграція

**Нова таблиця `content_qa_packages`:** `id`, `client_id` (nullable FK → `clients.id` `ondelete=SET NULL`, як `ContentQaReport`), `source_kind`, `source_file_id` (nullable), `source_name`, `source_url` (nullable), `profile_json`, `framework_count`, `cross_findings_json`, `cross_states_json`, `severity_counts_json`, `created_at`. Без `updated_at` — звіт не редагується, повторний прогін дає новий рядок (той самий принцип, що `ContentQaReport`).

**Одна нова колонка:** `content_qa_reports.package_id` — nullable FK → `content_qa_packages.id` `ondelete=CASCADE`. Nullable тому, що поодинокі прогони Strategy Check / Framework Check лишаються й далі й пакета не мають.

**Нові значення `doc_type`** (String, не Enum — як і було): `strategy_gsheet`, `framework_gdoc`, `copy_gdoc`. Наявні `strategy_xlsx`/`framework_docx` не чіпаються.

⚠️ **Порядок дій із бойовою БД — обов'язковий.** Прод уже на PostgreSQL 16 (`compose.yaml`, сервіс `db`), тож перед міграцією:

```bash
docker compose exec db pg_dump -U yovth -Fc yovth > app_data/backups/pre-task-88-$(date +%F).dump
```

`batch_alter_table` тут **не потрібен і не використовується** — на Postgres `op.add_column` виконується напряму. Це не стилістика: саме `batch_alter_table` на SQLite перестворює таблицю через `DROP TABLE` і одного разу вже стер `client_integrations`/`metric_values` на проді (CLAUDE.md).

---

## 7. Фронтенд

Новий модуль `frontend/js/strategy-package-tool.js`, роут `#/tools/strategy-package`, шарований звіт `#/tools/strategy-package/<id>`.

**Крок 1 — джерело**, три способи в одному ряду сегментованого контролю: `Paste link` / `Browse Drive` / `Upload file`. Перегляд Диска — простий список із хлібними крихтами тек і пошуком за назвою; не намагаємось відтворити Google Drive UI.

**Крок 2 — обсяг:** аркуші стратегії з чекбоксами (три стани з task-87), нижче — знайдені фреймворки з чекбоксами, кожен рядком: назва документа, звідки взяте посилання (`Q3 2026 Strategy!row 7`), URL сторінки, доступність. Недоступні — вимкнені, з текстом `Share this file with <agency address>`.

**Крок 3 — профіль:** пікер клієнта, kind/intent/locale/brand names, прапорці `Compare with the live site` / `Compare published copy`. Патерн і автолокаль із домену — той самий, що task-87 у `strategy-check-tool.js`.

**Звіт:** вкладки `Overview` / `Strategy` / `Frame` / `Copy` / `Cross-checks` / `Scope`. Findings-панель (чипи за чеком, пошук, group-by/sort, згортання груп) **перевикористовується** з `content-qa-render.js` — task-87 уже зробив її живим view-model'ом, дублювати не треба.

Обов'язкові дотримання:
- **Іконки, не емодзі** (AGENTS.md §3), уся копірайт-частина **англійською** (§14).
- **CSP.** `default-src 'self'`: жодного inline `style=`, жодного JS-присвоєння `.style.top/left`. Обидві пастки вже ловились живо на цьому проєкті.
- **Ланцюжок cache-bust.** Кожен ES-імпорт несе власний `?v=`; додавання рядка в `TOOL_MODULES` (`tools-page.js`) означає бамп `?v=` у **кожній** ланці до `index.html`, включно з простим тегом `<script src>` — інакше тунель Cloudflare віддасть старий файл до 4 годин.
- **П'ять станів** (AGENTS.md §6) для кожного асинхронного блока: loading / empty / partial / error / ok. `not_checked` рендериться **візуально відмінно** від «порушень немає» — це головна вимога PLAN §12 у цьому тулі.

---

## 8. Ризики й пастки

| Ризик | Мітигація |
|---|---|
| `drive.readonly` дає нам доступ до **всіх** файлів агенційного акаунта | Мінімізувати не можна (перегляд Диска вимагає `files.list`), тому мінімізуємо **використання**: тул читає лише той файл, який назвав оператор, і лише документи, на які веде цей файл. Кожен `resolve`/`inspect`/`check` пише рядок у журнал дій (task-72) з file_id — тобто «хто що читав» відновлюється. |
| Токен/секрет протікає в findings, події або UI-помилку | Правило `auth.py` дотримується дослівно: token лише в заголовку, повідомлення помилок збираються з allow-list полів відповіді, ніколи `str(exc)`. Юніт-тест, що жоден finding/error не містить підрядка токена. |
| Документ без вкладок Frame/Copy | Заміряно: **22 з 40** документів не мають вкладки з копією (§1.4a). `not_checked` із причиною + `info`-finding; одновкладковий читається як Frame. Не краш, не порожній звіт. |
| Вкладка `Content` є, але порожня (`1ch`) — Copy QA каже «0 порушень» | Поріг мінімальної довжини, нижче якого `not_checked` із фактичною довжиною. Це не крайовий випадок: у вибірці **2 з 18** документів із двома вкладками мали `Content` на 1 символ. |
| Пропустити посилання, бо вони — смарт-чипи | Заміряно: **148 із 209** посилань на фреймворки оформлені `chipRuns`, а не `hyperlink`/`textFormatRuns`. Маска без `chipRuns` дає хибний нуль на найновіших стратегіях — ця помилка вже сталася при першому скані (§1.8). Юніт-тест фіксує наявність `chipRuns` у масці. |
| Стратегія без колонки посилань | 9 з 25 мають посилання; решта — це вихідні файли нашого ж тула (`*_STRATEGY_CHECK_*`), архівні редакції та заготовки. Резервне джерело — пошук по Диску за префіксом клієнта; `*_CHECK_*` відсіюються за іменем, щоб тул не перевіряв власний вивід. |
| Sheets-виклик без `fields`-маски | Заміряно **136 МБ** на одну стратегію проти 0.3–0.6 МБ із маскою (пошук-кандидатів — 2–210 КБ). Маска — обов'язкова частина клієнта, а не місце виклику; закривається юніт-тестом (§4.2). |
| Хибні locale-findings на довгій прозі Copy | Локаль із профілю, автовизначення з домену клієнта, DoD 4 явно вимагає перевірити нуль хибних `voice-em-dash` на не-англійській локалі. |
| Порівняння з сайтом дає шум | Тільки секція-в-секцію за зіставленим заголовком, ніколи не diff усієї сторінки. Порогова схожість калібрується живо, число записується. |
| Прогін не влазить у синхронний запит | `run_package()` — чиста функція без HTTP-зв'язку; перенесення в job = тонкий адаптер. Рішення ухвалюється на замірі (DoD 9), не наперед. |
| Паралельні live-фетчі виглядають як скан сайту клієнта | `HostThrottle` (уже є) + cap 25 + послідовність усередині фреймворка. |
| Рефактор `readers/xlsx.py` тихо зсуває поводження чинного Strategy Check | Гейт: наявні тести `test_readers_xlsx.py` зелені **без єдиної правки**. Правка тесту = ознака регресії, не ознака прогресу. |
| Тестовий прогін пише в бойову БД | `AuthMiddleware`/`AuditMiddleware` вимкнені за замовчуванням у `tests/conftest.py`; тести API цього тула не ставлять `@pytest.mark.auth` без потреби (жива пастка: тестовий прогін уже дописував рядки в бойову `audit_events`). |
| Паралельна сесія тримає незакомічені правки у спільних файлах | Заміряно 2026-09-09: сусідня сесія тримає незакоммічені `app/integrations/google_api/auth.py`, `doc/dev/new/INDEX.md` і власні task-88/89. Спочатку робиться ізольована частина (`app/contentqa/**`, `app/integrations/google_api/*_client.py`, нові файли); до `auth.py`, `app/api/tools.py`, `frontend/js/{tools-page,api,router}.js`, `frontend/index.html`, `app/db/models.py` вертаємось, коли їхній `git status` чистий (техніка з `multi-agent-collision-avoidance`). Номери 88 і 89 уже зайняті — ця таска **90**. |

---

## 9. Definition of Done / Gate

1. ✅ **Закрито 2026-09-09.** Scope доданий і живий: проба показує `drive.readonly`; `drive/v3/about` → 200, `seo@theyovth.com`; видно спільний диск і My Drive.
2. **`/inspect` на реальній стратегії** перелічує справжні аркуші зі станами і знайдені фреймворки. Еталон — `SHB_SEO_STRATEGY_082626` (id `140t5rT6…`): має знайти **44 чипи → 41 унікальний документ** на трьох аркушах (колонки 12/14/15), перший — `SHB_FRAMEWORK_HOME_PAGE_082726`. Плюс контроль на змішаній формі: `SAPHNY_SEO_STRATEGY_032326` — 40 hyperlink + 15 chip → 54 унікальні. Плюс `ALPHA_SEO_STRATEGY_V2_072826` (нуль посилань) — фреймворки мають прийти з пошуку по Диску за префіксом, із поміткою «found on Drive by name».
3. **Вкладки читаються в усіх трьох заміряних станах** (§2.3): `Framework`+`Content` → обидва QA; лише `Framework` → Copy `not_checked`; лише `Tab 1` → читається як Frame + `info` про безіменну вкладку. Жоден стан не дає крашу.
4. **Copy QA дає ненульовий, вручну звірений результат** на реальній вкладці `Content` із справжньою прозою (наприклад `SWM_BLOG_FRAMEWORK_TURKISH_FOOD_IN_BROOKLYN_062226`, 10691 символ): щонайменше один справжній readability/slop/voice finding **і нуль хибних locale-findings** на своїй локалі.
4a. **Поріг порожньої копії відкалібрований** на заміряному розподілі (1 / 322 / 1446 / 2977 / 3614 / 5674 / 8038…11256 символів) і **записаний** у Completion report. `ALPHA_BLOG_..._CREWNECK_VS_HOODIE` (`Content` = 1 символ) і `3XE_FRAMEWORK_HOME_PAGE` мають дати `not_checked` із причиною, **не** «0 порушень».
5. **Лейн H відтворює реальне розходження** Frame↔Strategy — або доводить нуль розходжень із ручною перевіркою 3 рядків (обидва результати приймаються, підганяти заборонено).
6. **Лейн I на живій опублікованій сторінці:** title/desc/H1/H2-H3 порівняні; свідомо змінений заголовок ловиться; недоступна сторінка дає `not_checked`, а не «чисто».
7. **Лейн J відкалібрований** на ≥3 живих сторінках; обраний порог схожості **записаний** у Completion report разом із заміреними значеннями.
8. **Ізоляція:** прогін, у якому один фреймворк недоступний, завершується успішно — решта перевірена, недоступний позначений `not_accessible` із текстом ремедіації, що містить адресу агенційного акаунта.
9. **Замір продуктивності:** час повного прогону на реальній стратегії записаний; рішення «синхронно / у job-рушій» сформульоване на підставі цього числа.
10. **Якість коду в Docker:** `ruff check .`, `mypy app`, `pytest tests/unit/contentqa tests/unit/integrations/google_api tests/unit/api/test_tools_strategy_package.py` — зелені. Наявні `tests/unit/contentqa/test_readers_xlsx.py` зелені без правок.
11. **OpenAPI-снапшот** (`tests/unit/api/data/openapi_snapshot.json`) оновлений.
12. **Журнал дій:** усі чотири POST'и зареєстровані в `app/audit/actions.py`; рядок реально зʼявляється в `#/admin/audit` після прогону.
13. **Фронт:** візуальний loop AGENTS.md §15–16 на 1440/1024/768; усі 5 станів; `not_checked` візуально відрізняється від «порушень немає»; нуль емодзі; нуль inline-стилів; ланцюжок `?v=` збамплений до `index.html`.
14. **Бекап зроблений до міграції** (`pg_dump -Fc`), файл названий у Completion report.

---

## 10. Follow-ups (свідомо поза обсягом)

- **Google Picker + `drive.file`** для особистих @gmail-файлів без поширення на агенційний акаунт. Заблоковано трьома причинами з §1.3; повертатись лише якщо шлях «поширити файл» на практиці виявиться незручним. Ціна — послаблення CSP усього застосунку, окремий Web OAuth-клієнт, персональний Google-логін поверх наших ролей.
- **Job-рушій для пакета**, якщо замір DoD 9 покаже, що синхронний прогін не тримається.
- **Domain-wide delegation через сервісний акаунт** — альтернатива для корпоративного контуру, що знімає залежність від одного людського refresh token. Вимагає адмін-налаштування Workspace; сьогодні виграшу не дає, бо акаунт один.
- **Запис у Google Docs** (проставити виправлення прямо у документ). Свідомо ні: `drive.readonly` — це в тому числі гарантія, що тул фізично не може зіпсувати клієнтський документ.
- **Кеш прочитаних документів** за `(file_id, modifiedTime)` — щоб повторний прогін того самого пакета не викликав Docs API вдруге. Має сенс лише після заміру реальної частоти повторних прогонів.
