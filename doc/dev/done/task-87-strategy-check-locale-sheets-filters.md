# task-87 — Strategy Check: швецька мова, вибір аркуша, фільтр і сортування за типом помилки

- **Пакет:** три доробки живого інструмента **Strategy Check** (task-53), які виявила бойова експлуатація: (1) **локаль `sv-SE`** (+ мовна інфраструктура під наступні мови) — швецький воркбук LAXTON сьогодні перевіряється англійськими правилами, тобто половина лейнів **тихо не перевіряє нічого**, а частина дає хибні помилки на коректній швецькій типографіці; (2) **вибір аркуша (tab)** — ридер завжди бере **всі** аркуші, тому Q2- і Q3-стратегії одного файлу зводяться в один документ і порівнюються між собою; (3) **фільтр і сортування за типом помилки** — у бойовому звіті 2066 findings, з них 73% один і той самий чек, а в UI є лише три severity-чекбокси. Користувач явно попросив також **пікер клієнта у формі + автолокаль із домену клієнта** (`.se` → `sv-SE`) — додано в скоуп (§1.6), хоч і виходить за межі трьох названих пунктів.
- **Хвиля:** P — Content QA (продовження task-53; той самий рушій `app/contentqa/**`).
- **Depends on:** **53** ✅ (рушій, обидва тули), **06** ✅ (REST), **71/72** ✅ (роль/журнал дій), **75** (прод уже на Postgres — впливає лише на ритуал міграції).
- **Owned paths:** `app/contentqa/**`, `app/api/tools_content_qa.py`, `app/schemas/content_qa.py`, `app/db/repositories/content_qa.py`, `migrations/versions/*_content_qa_sheets_task_87.py` (нова), `frontend/js/{content-qa-render,strategy-check-tool,framework-check-tool}.js`, `frontend/css/content-qa.css`, `tests/unit/contentqa/**`, `tests/unit/api/test_tools_content_qa.py`
- **Адитивні дотики (узгодити):** `app/db/models.py` (+1 nullable колонка `sheets_json`), `app/audit/actions.py` (+1 `ActionSpec` — інакше падає guard `tests/unit/audit/test_actions_cover_routes.py`), `frontend/js/api.js` (+1 функція `inspectStrategyWorkbook`, bump `?v=`), `frontend/index.html` (bump `?v=` у ланцюжку), `app/api/tools.py` (summary тула — опційно). **Read-only, не чіпається:** `frontend/js/alt-text-tool.js` (лише джерело патерну для §1.6, логіка копіюється, не імпортується — див. §1.6).
- **PLAN.md:** §12 (**не перевірили ≠ порушень немає** — центральний інваріант усієї частини про локаль), §14 (секрети не течуть у findings)
- **frontend/AGENTS.md:** §5 (правда даних), §6 (стани), §8 (форми/контроли), §12 (vanilla JS/CSS обмеження, CSP), §13 (перф: делегування, не 2000 слухачів), §14 (UI-копірайт англійською), §15–16 (візуальний loop і чекліст)
- **AISeoTools:** змін **не потребує** (див. §1.1 — рішення про місце швецького лексикону).

---

## 0. Дослідження (усе виміряно на бойових даних 2026-09-08)

### 0.1 Що вже прогнали через тул реально (таблиця `content_qa_reports`, 13 звітів)

| Файл | items | findings | розподіл severity | distinct checks |
|---|---|---|---|---|
| `LAXTON_SEO_STRATEGY_04142926.xlsx` (**швецька**) | 185 | **2066** | 1841 error / 221 warn / 4 info | 12 |
| `SAPHNY_SEO_STRATEGY_040826.xlsx` | 184 | 575 | 299 / 266 / 10 | 15 |
| `SDS_SEOStrategy_Q42026_09072026.xlsx` | 18 | 45 | 39 / 2 / 4 | 7 |
| `NYSAPH_SEOStrategy_Q42026_09072026.xlsx` | 15 | 39 | 19 / 19 / 1 | 7 |
| `GOREPORT_SEO_STRATEGY_07272026 (3).xlsx` | 31 | 53 | 23 / 27 / 3 | 12 |

Тобто інструмент **справді використовують** на портфелі, і два найбільші файли дають звіти на 575 і 2066 рядків — масштаб, під який поточний UI не проєктувався.

### 0.2 Швецька: не шум, а **хибна тишина** (головний аргумент)

Порівняння двох воркбуків **однакового розміру** (LAXTON 185 items / SAPHNY 184 items), один і той самий прогін, `locale=en-US`:

| Лейн | SAPHNY (англ.) | LAXTON (швед.) | Що це означає |
|---|---|---|---|
| `meta-desc-banned-opener` | 31 | **0** | англійський `BANNED_OPENERS` не бачить `Upptäck`/`Boka`/`Köp` |
| `slop-filler-term` | 24 | **0** | `FILLER_TERMS` не бачить `marknadsledande`/`skräddarsydd` |
| `slop-phrase` (AI_SLOP_PHRASES) | 0 | **0** | обидва нулі, але для швецької це нуль **за побудовою** |
| `voice-em-dash` | 22 | **136** | ⬅ хибні помилки, див. нижче |
| `voice-locale-mismatch` | — | 2 | ⬅ хибні помилки, див. нижче |

**55 знахідок про якість копірайту в англійському файлі проти 0 у швецькому** — і не тому, що швецький копірайт чистий: у ньому 1514 дублікатів описів (шаблонні `Se storlekar, material och leveransinfo i LaxTon Shop.`). Звіт віддає «чисто» там, куди ніколи не дивився → пряме порушення PLAN §12.

**Хибні спрацювання, виміряні на живому `laxton.se` (174 сторінки з `monitor_pages`):**

1. **En dash — це коректна швецька пунктуація** (*tankstreck*). У живому копірайті: `em dash 0`, `en dash 12`. У воркбуці — **136 `voice-em-dash` errors**, тобто майже всі 136 — покарання за правильну типографіку. Домашнє правило (`policy._BRAND_VOICE_DASHES` забороняє і em, і en) писалося для **наших англійських** документів.
2. **US/GB-лейн стріляє в швецькі слова.** На живому тексті знайдено токени `program` (шведське слово) і `favorite`; профіль `en-US` вимагає `programme`. Виміряно 2 таких finding у воркбуці. Для `sv-SE` цей лейн не має сенсу взагалі.
3. **Читабельність бреше в «зелений» бік.** Flesch–Kincaid на швецькій прозі дає **9.38** (усередині домашнього банду 7–10, тобто «все добре»), а LIX того самого тексту — **54.8** (важкий, канцелярський). Формула англійська: складові групи й довжина слів у швецькій означають інше.
4. **`keyword_in_title` ламається на швецькій морфології:** ключ `mössor` проти титулу `Mössa …` → `False` → хибний `meta-title-keyword-missing`. (`hoodie grå`, `presentkort`, `repris ekenäs slott` — `True`, тобто дефект точковий, а не тотальний: 4 finding у воркбуці.)
5. **Калібрування LIX на реальному тексті:** 141 живий швецький опис `laxton.se` → медіана **34.8**, p10 **22.0**, p90 **45.0**. Тобто прийнятний швецький веб-копірайт лежить у діапазоні ~30–45 LIX — це і є банд, а не книжкові 30–40.

### 0.3 Аркуші: усе зливається в один документ

`read_strategy_xlsx()` проходить **усі** `wb.sheetnames` і складає items у **один** `ContentDoc`. Наслідки, виміряні на бойових звітах:

- **LAXTON:** findings по аркушах — `Q2 2026 Strategy` **2002**, `Q3 2026 Strategy` 51, `Structure Features Description` 58. **41 finding перетинає межу аркушів**: `url-duplicate` 16, `meta-desc-duplicate` 13, `meta-title-duplicate` 8, `keyword-duplicate-across-pages` 4. Сторінка, що є і в Q2, і в Q3, — це **історія планування**, а не «URL двічі в стратегії»; так само «канібалізація» між кварталами.
- **SAPHNY:** 4 аркуші (`Q2`, `Q3`, `Blogs`, `Structure`), 4 крос-аркушевих finding того ж класу; 504 з 575 findings — з одного аркуша `Q3 2026 Strategy`, тобто оператор, який прийшов ревʼювити Q3, платить увагою за решту.
- **`GOREPORT_BLOG_STRATEGY_Q32026.xlsx`** (прогнано ефемерно під час дослідження): 4 аркуші (`Blog Strategy`, `Keyword Evidence`, `Update Recommendations`, `Sources & Methodology`), несучий **один**. З 6 findings **3 — `sheet-skipped`**, тобто половина звіту — службовий шум про аркуші, які оператор і не збирався перевіряти.
- Профіль `kind` (`page` | `blog`) у Strategy Check UI **не показується взагалі** (завжди `page`), хоча у файлі буквально є аркуш `Blogs` — правильний `kind` можливий тільки після вибору аркуша.

### 0.4 Фільтр/сортування: чому трьох чекбоксів мало

- LAXTON: **1514 з 2066 findings (73%) — один чек** `meta-desc-duplicate`. Аби побачити решту 27%, оператор мусить прокрутити повз 1514 однакових карток: у групі `error` вони лежать разом із `meta-title-length` і `voice-em-dash`.
- `renderList()` рендерить **усі** findings одразу (2066 `<li>` з `<blockquote>` і двома кнопками кожен) — і жодного способу згорнути, обмежити чи знайти.
- Наявний фільтр — рівно `SEVERITY_ORDER` (3 чекбокси) + прихований `hidden` на групі. Сортування — немає (порядок = порядок правил у рушії).
- `content-qa-render.js` **спільний** із Framework Check, отже будь-яке покращення дістається обом тулам (і має бути перевірене на обох).

### 0.5 Наявні seam-и, на які лягає робота (нічого ламати не треба)

| Seam | Де | Чому підходить |
|---|---|---|
| Блоки заголовків по аркушу | `readers/xlsx.py:_iter_header_blocks` | вже вміє «які аркуші несучі» — з цього робиться прев'ю без нового парсера |
| `Profile` | `rules.py:31` | вже носить `locale`; додається похідна `language` |
| Honest-degradation патерн | `engine.py` (`policy-unavailable`) | готовий приклад «лейн не виконано → один info finding, не тиша» |
| `policy_bridge` | `policy_bridge.py` | лейзі-seam до AISeoTools; швецькі лейни просто не ходять через нього |
| Двокроковий upload | `api/tools_alt_text.py` (`/inspect` → `sheets[]` → `/reports` з `body.sheet`) | доведений прецедент вибору аркуша в цьому ж проєкті |
| `ContentQaProfileDTO` | `schemas/content_qa.py` | конфігурація прогону вже персиститься в `profile_json` |

---

## 1. Рішення

### 1.1 Швецький лексикон живе **в цьому репо**, не в AISeoTools

`app/contentqa/lexicon/sv.py` (новий), реєстр `app/contentqa/lexicon/__init__.py` → `{"sv": SvLexicon, "en": None}`.

**Чому не в `seo_audit.policy`** (де живуть англійські списки): англійські правила там тому, що **AISeoTools сам генерує англійські стратегії** й мусить перевіряти власний вивід. Швецьких deliverables він не виробляє — швецькі списки мав би рівно один споживач, цей дашборд. Ціна альтернативи: бамп піна commit-а AISeoTools + прогін усієї чужої test suite (CLAUDE.md §8) заради даних, які там нікому не потрібні. Пам'ятка «правила контент-політики живуть у сусідньому репо» стосується правил, **які там уже є** (їх ми й далі імпортуємо, не переписуємо: meta-стелі, similarity, keyword-хелпери). Якщо AISeoTools колись почне робити швецькі стратегії — лексикон переїде туди адитивно, інтерфейс `Lexicon` для цього й потрібен.

**Наслідок для дизайну:** мова — **не** `if locale == "sv-SE"` по коду. `Profile.language` (похідна: `locale.split("-")[0]`) вибирає лексикон із реєстру; лейн питає лексикон, а не локаль. Додати наступну мову (RU для SAPHNY-документів — уже видно в портфелі) = один файл, нуль правок у правилах.

### 1.2 Мапа лейнів за мовою (це і є контракт швецької частини)

| Лейн | `en-US`/`en-GB` | `sv-SE` | Рішення й чому |
|---|---|---|---|
| meta довжини (title/desc, min/max), brand-padding | як є | **як є** | символи/пікселі мовно-нейтральні |
| дублі title/desc (similarity) | як є | **як є** | `policy._WORD_RE` Unicode-safe (перевірено в `policy.py` комментарем і кодом) |
| `meta-title-keyword-missing` | `pb.keyword_in_title` | **стем-матч** (перші 5 літер токена, casefold+NFKC) | виміряно: `mössor` vs `Mössa` дає хибний finding; стем ловить композити/визначену форму |
| banned opener | `policy.BANNED_OPENERS` | **`BANNED_OPENERS_SV`** | `Upptäck, Utforska, Boka, Köp, Handla, Beställ, Läs, Se, Hitta, Välkommen, Kontakta, Ring, Prova, Spara, Njut, Registrera, Ladda, Besök, Jämför, Välj, Förvandla, Höj, Få, Ta` |
| AI-слоп фрази | `policy.AI_SLOP_PHRASES` | **`AI_SLOP_PHRASES_SV`** | `i dagens digitala värld`, `oavsett om du`, `sök inte längre`, `vi har allt du behöver`, `när det kommer till`, `ett brett utbud av`, `skräddarsydd efter dina behov`, `ta första steget`, `det är viktigt att notera`, `i takt med att`, `spelar en viktig roll`, `hos oss får du` |
| filler-терміни | `policy.FILLER_TERMS` | **`FILLER_TERMS_SV`** | `marknadsledande, branschledande, helhetslösning, kvalitetssäkrad, högkvalitativ, prisvärd, professionell, erfaren, dedikerad, omfattande, toppmodern, banbrytande, sömlös, skräddarsydd, unik, i världsklass, förstklassig, oslagbar` |
| word swaps | `WORD_SWAPS` | **`WORD_SWAPS_SV`** (klarspråk) | `nyttja→använda`, `i syfte att→för att`, `erhålla→få`, `föreligger→finns`, `avseende→om`, `påbörja→börja`, `verkställa→göra`, `medelst→med`, `innehava→ha`, `vidtaga åtgärder→göra` |
| US/GB spelling | як є | **вимкнено** | виміряно: стріляє в швецькі `program`, `favorite`; для `sv` лейну немає відповідника |
| contractions у формальному тексті | англ. regex | **`sv`-варіант** (`nån, nåt, sen, dom, va, sån, mycke`) як `warn` | той самий сенс: розмовна форма у клієнтському документі |
| термінологія (`GMB→GBP`, `Recommended Action`) | як є | **як є** | це наші ідентифікатори, не мова |
| money-leak | `[$£€]`, `usd/gbp/eur` | **+ `kr`, `SEK`, `:-`** | без цього швецький гонорар у копірайті проходить (PLAN §14 — той самий клас, що секрети) |
| em/en dash | обидва `error` | **em `error`, en — не репортується** | виміряно: en dash — стандартний швецький *tankstreck*; 136 з 136 «помилок» саме він. Факт вимкнення лейну фіксується в `locale-rules-adjusted` (нижче) |
| Wikipedia-слоп (negative parallelism, copula avoidance, vague attribution, participle chain, undue significance, collaborative voice, rule of three) | як є | **частково**: `inte bara … utan också`, `utgör en`, `enligt vissa experter`, `det är värt att notera`, `som vi ser` | решта — англійські синтаксичні конструкції; чого немає, потрапляє в `locale-rules-adjusted` |
| placeholder / LLM-артефакти / emoji | як є | **як є** | мовно-нейтральні; `slop-emoji-formatting` уже спрацював 2× на LAXTON — коректно |
| читабельність | Flesch–Kincaid 7–10 | **LIX 30–45** | LIX = слова/речення + 100×довгі(>6)/слова (Björnsson — стандарт для швецької). Банд узятий не з книжки, а **з живого корпусу**: 141 опис `laxton.se`, медіана 34.8, p90 45.0. **NB:** лейн читабельності працює лише коли є `item.body`, тобто для **Framework Check**; у Strategy Check (XLSX) `body` немає — тут це не дає жодного finding і не має продаватись як фіча Strategy Check |
| answer-first 40–80 слів, секція ≤300 слів, речення ≤30 слів | як є | **як є** | підрахунок слів, не мова |
| лінки/анкори | як є | **як є** | окрім `anchor-generic` (`click here`) → додати `läs mer`, `här`, `klicka här` |

**Честність (PLAN §12) — обов'язковий вихід:** прогін з не-англійською локаллю додає **рівно один** `locale-rules-adjusted` (`info`, ref = `doc.source`) з переліком того, що вимкнено або замінено для цієї мови (той самий патерн, що `policy-unavailable`). Це те, що робить «нуль findings у швецькій» читабельним: видно, куди не дивились.

### 1.3 Вибір аркуша: stateless `inspect` + повторна відправка байтів

**Крок 1.** `POST /api/v1/tools/strategy-check/inspect` (сирі байти, `X-Filename`, як `check`) → перелік аркушів із прев'ю. Нічого не персистує, нічого не пише в БД.
**Крок 2.** `POST …/check?sheet=<name>&sheet=<name2>&cross_sheet=false` — фронт **повторно відправляє ті самі байти** (`File` лишається в памʼяті сторінки).

**Чому не сховище чернеткових upload-ів, як у Alt Text** (`_StoredUpload`, TTL 6 год, `artifacts_dir`): там воно виправдане — sha256-дедуп, багатокрокове мапування колонок, ім'я аркуша потрібне і в звіті. Тут рішення **одне** (які аркуші), файл ≤10 МБ, а сам прогін — секунди. Stateless-варіант не додає ні диска, ні TTL, ні прибирання «покинутих» файлів, ні шляху обходу `safe_resolve`. Ціна — друга передача ≤10 МБ по localhost/тунелю; вона того варта.

**Скоуп крос-сторінкових перевірок.** `check_cross_page()` і `check_meta_duplicates()` за замовчуванням працюють **у межах аркуша**, не по всьому документу. Прапорець `cross_sheet=true` (в UI — чекбокс «Also compare across sheets», **вимкнений**) повертає старе поводження свідомо. Це прибирає виміряні 41 (LAXTON) і 4 (SAPHNY) артефакти, не втрачаючи можливості спеціально шукати канібалізацію між кварталами. Реалізація: `ContentItem` отримує **адитивне** поле `sheet: str | None` (ридер XLSX заповнює; DOCX лишає `None` → одна група) — парсити `ref` рядком було б крихко.

**Дефолт вибору:** усі несучі аркуші (title/description) — тобто хто нічого не змінює, отримує сьогоднішнє поводження, окрім скоупу дублікатів. Несучість визначає той самий `_iter_header_blocks`, ніякого другого парсера.

**Честність станів аркуша** (три різні речі, які не можна злити в один `skipped_sheets`):
- `checked` — вибраний і перевірений (з `item_count`);
- `no_recognized_columns` — не має ні title, ні description (сьогоднішній `sheet-skipped`);
- `not_selected` — оператор свідомо не брав. **Не** `info`-finding у списку (це не проблема файлу), а рядок у блоці «Scope» звіту: «Checked: Q3 2026 Strategy (31 rows). Not checked: Q2 2026 Strategy (154 rows, not selected), Structure (no title/description columns)».

**Персист.** Нова nullable колонка `content_qa_reports.sheets_json` (перелік станів аркушів — це **вихід** прогону), а `cross_sheet` і `sheets` (вибір оператора) — у наявний `profile_json` (це **конфігурація** прогону, поряд із `locale`/`kind`/`brand_names`). Старі рядки: `sheets_json IS NULL` → DTO віддає `[]`, а UI показує наявний `skipped_sheets` як раніше (жодного «додумування» за старий звіт).

⚠️ **Ритуал БД (CLAUDE.md):** перед `alembic upgrade` — `docker compose exec api poetry run python scripts/backup_sqlite.py --out-dir /app/app_data/backups --keep 14` (або актуальний PG-еквівалент `pg_dump -Fc` після task-75). Міграція — прямий `op.add_column` з nullable, **без `batch_alter_table`**. Уроком task-84 (`VARCHAR(8)` на живій PG) — тип `JSON`/`JSONB`, не `VARCHAR`.

### 1.4 Фільтр і сортування: один view-model, спільний для обох тулів

`content-qa-render.js` перестає бути «один прохід у HTML» і отримує локальний стан панелі:

```
state = { severities:Set, checks:Set, sheets:Set, query:string,
          groupBy:"severity"|"check"|"sheet"|"none",
          sort:"severity"|"check"|"count"|"document",
          expanded:Set, limitPerGroup:50 }
```

- **Фільтр за типом помилки:** список чеків, які **реально є в цьому звіті**, з кількостями, відсортований за кількістю; клік по чипу — toggle, `Alt`-клік/кнопка «only» — «лише цей», «All»/«None». Плюс фільтр за аркушем (коли їх >1) і текстовий пошук по `ref`/`detail`/`value`.
- **Сортування/групування:** `Group by: Severity | Check | Sheet | Flat`, `Sort: Severity ▾ | Check A→Z | Count ▾ | Document order`. Дефолт лишається `Severity`, щоб наявна звичка не ламалась.
- **Перф (AGENTS.md §13):** група рендериться згорнутою, якщо в ній >`limitPerGroup`; «Show all 1514» довантажує решту в цю ж групу. Один делегований listener на панель, `el.hidden`, ніяких inline-стилів і `.style.*` (CSP — пам'ятки `frontend_verification_tooling`, `task43_reports_ui_done`).
- **Стани (AGENTS.md §6):** «нічого не знайдено фільтром» ≠ «звіт чистий» — два різні порожні стани з різним текстом і кнопкою «Clear filters».
- **Шапка звіту** додає: локаль (`Swedish` для `sv-SE`), блок Scope (§1.3), і `Copy link` як був.
- Оскільку модуль спільний, **Framework Check отримує те саме** і мусить бути перевірений (там аркушів немає → фільтр за аркушем не рендериться).

### 1.6 Пікер клієнта + автолокаль із домену (додано за прямим запитом користувача)

Сьогодні `client_id` API приймає, а форма Strategy Check/Framework Check його не відправляє — жоден звіт не привʼязаний до клієнта, доки оператор не відкриє DevTools. Тим часом `clients.domain` уже містить TLD (`laxton.se`, `nysapphire.com`), і саме з нього бере назву LAXTON у §0.2.

**Прецедент у цьому ж проєкті:** `alt-text-tool.js` уже вирішує рівно цю задачу — `fillClientOptions()` тягне список клієнтів через `getDashboard({period:"week"})`, будує `<select>` і викликає `guessClient(filename, clients)` (експортована, префіксний матч нормалізованого імені файлу проти імені/домену клієнта, найдовший збіг перемагає). **Не імпортувати** цю функцію з `alt-text-tool.js` напряму — файл належить task-27, а завʼязка на чужий owned-модуль пережила б наступний рефакторинг того тула мовчки. Замість цього — та сама логіка (це ~20 рядків чистої функції) переноситься в **спільний** `content-qa-render.js` (owned цією таскою, вже спільний для обох тулів) як `guessContentQaClient(filename, clients)`, з коментарем-посиланням на оригінал у `alt-text-tool.js`, щоб майбутня зміна одного місця нагадала про друге.

**Що саме додається:**

- `<select>` клієнта у формі обох тулів (список — `getDashboard({period:"week"}).clients`, той самий джерело даних, що в Alt Text — жодного нового ендпоінту).
- Автопідбір клієнта з імені файлу через `guessContentQaClient` (той самий UX: `form-field--autofilled` + підказка «Picked from the file name — change it if that is wrong»).
- **Автолокаль із домену вибраного клієнта:** мапа `TLD → locale` у самому тулі (`{ ".se": "sv-SE" }`, дефолт `"en-US"`) — навмисно **не** повноцінний реєстр країн (у портфелі рівно один `.se`-клієнт, будувати генералізовану мапу без другого прикладу — здогад, не рішення). Спрацьовує **тільки** коли оператор ще не чіпав селектор локалі вручну (`form-field--autofilled` на локалі теж, знімається при ручній зміні) — вибір клієнта ніколи не затирає свідомий вибір мови.
- Обраний клієнт більше **не залежить** від вибору аркуша чи локалі й навпаки — три незалежні поля форми, порядок заповнення довільний.

**Чому це не ламає §1.1 (реєстр мов):** мапа `TLD → locale` — суто фронтова UX-підказка (autofill), не частина рушія перевірки; сам `Profile.language` завжди береться з обраної в UI локалі, а не з домену напряму. Якщо клієнт без TLD-збігу (домен `.com`) — просто дефолт `en-US`, як сьогодні.

### 1.7 Поза скоупом task-87 (свідомо **не** робимо)

- Повноцінна мапа `TLD → locale` на більше ніж `.se` — додати новий рядок тривіально, коли зʼявиться другий не-англомовний клієнт.
- Експорт findings у CSV/XLSX; `kind` (page/blog) у формі Strategy Check як окрема продумана фіча (сьогодні дефолт `"page"`, після §1.3 стає видимим при виборі аркуша `Blogs`, але спеціальний UX для цього — не тут); шаринг фільтрованого вигляду через URL.

---

## 2. Контракти (що бачить фронт)

```python
# app/schemas/content_qa.py  (owned task-53 → тепер task-87)
ContentQaLocale = Literal["en-US", "en-GB", "sv-SE"]          # адитивно
ContentQaSheetState = Literal["checked", "not_selected", "no_recognized_columns"]

class ContentQaSheetPreviewDTO(BaseModel):     # відповідь /inspect
    name: str
    index: int
    load_bearing: bool
    columns: list[str]        # ["title","description","url",…] розпізнані теги
    row_count: int            # рядків-кандидатів під заголовком
    blocks: int               # незалежних таблиць на аркуші (§2.3 task-53)

class ContentQaInspectDTO(BaseModel):
    source_filename: str
    sheets: list[ContentQaSheetPreviewDTO]
    suggested_sheets: list[str]        # усі load_bearing

class ContentQaSheetDTO(BaseModel):    # у звіті
    name: str
    state: ContentQaSheetState
    item_count: int

class ContentQaProfileDTO(BaseModel):  # адитивно
    ...
    sheets: list[str] | None = None    # None = «усі» (старі звіти)
    cross_sheet: bool = True           # старі звіти саме так і працювали

class ContentQaReportDTO(ContentQaReportSummaryDTO):
    ...
    sheets: list[ContentQaSheetDTO] = []   # [] для старих рядків
```

```
POST /api/v1/tools/strategy-check/inspect            body: bytes, X-Filename → ContentQaInspectDTO
POST /api/v1/tools/strategy-check/check?locale=sv-SE&sheet=A&sheet=B&cross_sheet=false&… → як було
```

Нові `check`-и (стабільні id, бо потрапляють у фільтр і в збережені звіти): `locale-rules-adjusted` (info). Перейменувань наявних id **немає** — інакше зламається фільтр на вже збережених звітах.

---

## 3. Кроки

1. **Локальний каркас мови.** `Profile.language`; `app/contentqa/lexicon/{__init__,sv}.py`; `locale-rules-adjusted` в `engine.run_checks`. Юніти на реєстр і на те, що `en` поводиться **байт-у-байт** як раніше.
2. **Розвести лейни за мовою** (`rules.py`, `policy_rules.py`, `readability.py`) строго за таблицею §1.2. Кожен пункт таблиці = мінімум один тест, включно з «для `sv` цей лейн НЕ спрацював і це записано в `locale-rules-adjusted`».
3. **LIX + стем-матч ключа.** `readability.py`: диспетч за мовою; банд `sv` = 30–45, з посиланням у докстрінгу на калібрування §0.2. `keyword_in_title` для не-англійської — власний стем-матч у `policy_rules` (не чіпаючи AISeoTools).
4. **`ContentItem.sheet` + фільтрація аркушів у ридері.** `read_strategy_xlsx(content, *, source, sheets=None)`; повертає `ContentDoc` + перелік станів аркушів. `inspect_strategy_xlsx()` — прев'ю на тому ж `_iter_header_blocks`.
5. **Скоуп крос-сторінкових перевірок.** `check_cross_page`/`check_meta_duplicates` групують за `item.sheet`, якщо `cross_sheet=False`. Тест: два аркуші з тим самим URL → 0 findings при `cross_sheet=False`, 1 при `True`.
6. **Міграція + персист.** Бекап → `op.add_column("content_qa_reports", "sheets_json", JSON, nullable=True)` → репозиторій пише/читає, DTO віддає, старий рядок дає `[]`.
7. **API.** `/inspect` (+ `ActionSpec` у `app/audit/actions.py`, інакше guard-тест червоний), нові query-параметри в `/check`, 400 на аркуш, якого немає у файлі (не тиха перевірка «всього»).
8. **Фронт — форма.** `sv-SE` в обох тулах (`strategy-check-tool.js`, `framework-check-tool.js`) + `LOCALE_LABEL`; двокроковий вибір аркуша з прев'ю (один несучий аркуш → без зайвого кроку, лише рядок «Sheet: X»); чекбокс `cross_sheet`.
9. **Фронт — пікер клієнта + автолокаль (§1.6).** `guessContentQaClient()` у `content-qa-render.js`; `<select>` клієнта в обох формах; мапа `TLD → locale`; autofill-стан локалі, що знімається ручною зміною.
10. **Фронт — findings.** View-model, фільтр за чеком/аркушем, пошук, групування, сортування, згортання, «show all», два порожні стани, блок Scope. Bump `?v=` **по всьому ланцюжку імпортів** до `index.html` (пам'ятка `frontend_module_cache_bust_chain`).
11. **Калібрувальний прогін** на реальних файлах (§5) і фіксація «до/після» в Completion report.

---

## 4. Тести

- `tests/unit/contentqa/test_lexicon_sv.py` — кожен швецький список: спрацьовує на швецькому рядку, **не** спрацьовує на англійському, і навпаки.
- `tests/unit/contentqa/test_locale_gating.py` — по одному тесту на рядок таблиці §1.2; окремо: `en`-прогін ідентичний до змін (регресійний якір), `sv`-прогін завжди має рівно один `locale-rules-adjusted`, en dash у `sv` не дає finding, `program` у `sv` не дає `voice-locale-mismatch`.
- `tests/unit/contentqa/test_readability.py` (+) — LIX на швецькому тексті в банді/поза бандом; F-K не викликається для `sv`.
- `tests/unit/contentqa/test_readers_xlsx.py` (+) — `sheets=` фільтрує; `sheet` заповнюється; неснуючий аркуш → помилка; стани аркушів.
- `tests/unit/contentqa/test_rules.py` (+) — скоуп дублікатів/канібалізації за аркушем vs крос-аркушево.
- `tests/unit/api/test_tools_content_qa.py` (+) — `/inspect` форма відповіді, `check` із `sheet`/`cross_sheet`, персист і читання `sheets_json`, **старий звіт без `sheets_json` читається** (backward-compat), 400 на невідомий аркуш.
- `tests/unit/audit/test_actions_cover_routes.py` — має лишитись зеленим (це і є гейт на реєстрацію `/inspect`).
- Фронт: Node-Playwright скрипт (`.cjs`/`require`, пам'ятка `frontend_verification_tooling`) — фільтр за чеком змінює кількість видимих рядків, «only» ізолює один чек, порожній стан фільтра ≠ «clean», сортування змінює порядок груп, вибір клієнта з `.se`-доменом автопідставляє `sv-SE` лише якщо локаль ще не чіпали вручну, CSP-консоль без порушень; той самий скрипт проти Framework Check.

---

## 5. Definition of Done / Gate

1. `docker compose exec api poetry run pytest tests/unit/contentqa tests/unit/api/test_tools_content_qa.py tests/unit/audit -q` — зелено; `ruff check .` і `mypy app` — чисто.
2. **Регресія англійської:** повторний прогін `SAPHNY_SEO_STRATEGY_040826.xlsx` із дефолтами (усі аркуші, `cross_sheet=true`, `en-US`) дає **ті самі 575 findings** із тим самим розподілом. Локальні зміни не мають права зрушити англійський результат.
3. **Швецький прогін (головний гейт):** `LAXTON_SEO_STRATEGY_*.xlsx` із `locale=sv-SE`:
   - `voice-em-dash` падає з 136 до ~0 (лишаються справжні em dash, якщо є);
   - `voice-locale-mismatch` = 0;
   - з'являються **ненульові** швецькі `meta-desc-banned-opener` / `slop-filler-term` / `slop-phrase` (інакше швецький лексикон не працює, а не «файл чистий»);
   - у звіті рівно один `locale-rules-adjusted` із переліком вимкненого;
   - **вручну переглянути 20 випадкових швецьких findings** і підтвердити, що жоден не є хибним (це не автоматизується; порогом «прийнято» є нуль хибних із 20).
   ✅ **Файл підтверджено доступним для верифікації:** `/home/elon/Downloads/chrome/LAXTON_SEO_STRATEGY_04142926.xlsx` (409 КБ, на хості; тул його не персистить, тож для кожного повторного прогону при розробці брати саме цей шлях).
4. **Вибір аркуша живо:** на LAXTON вибрати лише `Q3 2026 Strategy` → `item_count` і findings відповідають лише цьому аркушу, крос-аркушевих `url-duplicate` **нема**; вибір `Q2`+`Q3` із `cross_sheet=false` теж їх не дає, а з `cross_sheet=true` — дає (16, як зараз).
5. **Фільтр/сортування живо** на звіті LAXTON (2066 findings): «only `meta-desc-duplicate`» → 1514 видимих; знявши цей чек → 552; групування за чеком дає 12 груп із кількостями; пошук по підрядку зі швецького титулу знаходить свій рядок; сторінка лишається відзивною (перший рендер без «завислої» вкладки).
6. **Пікер клієнта + автолокаль живо:** завантажити LAXTON-файл без ручного вибору клієнта → ім'я файлу підбирає клієнта `LAXTON` (autofill-підказка видима) → локаль автопідставляється `sv-SE` (теж autofill-підказка); ручна зміна локалі до чи після вибору клієнта **не** перетирається повторним автопідбором; клієнт без TLD-збігу (`.com`) лишає локаль на дефолті без жодної підказки.
7. **Framework Check не зламався:** прогін `GOREPORT_PAGE_FRAMEWORK_*.docx` віддає ті самі 4 findings, панель фільтрів і пікер клієнта працюють, блоку аркушів немає.
8. Візуальний чекліст `frontend/AGENTS.md §16` на 1440/1024/768; консоль без помилок і без CSP-порушень; UI-копірайт **англійською**.
9. Міграція застосована **після** бекапу; `alembic downgrade` перевірено на копії, не на бойовій.

---

## 6. Ризики й пастки (перевірено на власному досвіді проєкту)

| Пастка | Дія |
|---|---|
| **Паралельна сесія** тримає незакоміченими `doc/dev/new/INDEX.md` (і `doc/verification/`, `scripts/verify_saphny_sources.py`) на момент написання плану | рядок в `INDEX.md` додавати лише коли його `git status` чистий (пам'ятка `multi_agent_collision_avoidance`) |
| Ланцюжок cache-bust: кожен ES-import несе свій `?v=` | бампити всі ребра до `index.html`, включно з `<script src>` (`frontend_module_cache_bust_chain`, `cloudflare_static_asset_cache_bug`) |
| CSP ріже inline `style=` **і** JS-присвоєння `.style.*` | тільки класи + `el.hidden` |
| Guard `test_actions_cover_routes` | новий POST без `ActionSpec` = червоний тест |
| `AuthMiddleware`/`AuditMiddleware` глобальні й пишуть у БД | нові API-тести без маркера `@pytest.mark.auth` (`global_test_singleton_db_leak`) |
| Перевірка ролей через `127.0.0.1` завжди дає bypass-адміна | якщо треба перевірити 403 для `viewer` — через тунель або з `AUTH_LOCALHOST_BYPASS=false` (`task73_dev_bypass_done`) |
| `batch_alter_table` на SQLite стирав таблиці | прямий `op.add_column`, nullable, після бекапу |
| Спокуса «підкрутити» англійські списки під швецьку | англійські лейни лишаються недоторканими; регресійний якір DoD §2 це й охороняє |
| 2000 findings у DOM | згортання + `limitPerGroup`, делегування (AGENTS.md §13) |

---

## 7. Follow-ups

- Повноцінна мапа `TLD → locale` (більше клієнтів поза `.se`/`.com`), коли з'явиться другий приклад — `TODO(task-88?)`.
- Наступна мова (RU) — має вийти одним файлом лексикону; якщо ні, реєстр §1.1 спроєктований неправильно.
- Експорт findings (CSV/XLSX) і шаринг фільтрованого вигляду через URL.
- `kind` (page/blog) у формі Strategy Check — осмислений тільки разом із вибором аркуша (`Blogs` = blog).
- Швецький аналог `HEMINGWAY_*` для Framework Check набуде сенсу лише коли з'явиться швецький DOCX-фреймворк; поки що лейн `body` у Strategy Check не задіяний.

---

## ✅ Completion report

- **Дата:** 2026-09-08
- **Гілка / комміт:** feat/task-85-client-report-document-design @ 897edac (план) + ця сесія
- **Статус:** done

### Що зроблено

Усі чотири пункти скоупу (§1.1–§1.6) реалізовані, живо перевірені на реальному швецькому воркбуку користувача (`LAXTON_SEO_STRATEGY_04142926.xlsx`, 185 items, 4 аркуші) і на бойових API-запитах через `AGENT_DEV_TOKEN`:

1. **Мовний реєстр + шведська локаль** (`app/contentqa/lexicon/`): `Profile.language`/`Profile.lexicon`, кожен лейн з §1.2 розведений за мовою. Живо: `voice-em-dash` 136→37 (лишились лише справжні em dash — 119 колишніх "помилок" були коректним шведським en dash/tankstreck), `voice-locale-mismatch` 2→0, зʼявились ненульові шведські `slop-filler-term` (12), `meta-desc-banned-opener` (5), `slop-phrase` (2); рівно один `locale-rules-adjusted` info-finding на прогін.
2. **Вибір аркуша** (`app/contentqa/readers/xlsx.py`, новий ендпоінт `/inspect`): stateless прев'ю → повторна відправка байтів у `/check` з `sheet=`/`cross_sheet=`. Три чесні стани аркуша (`checked`/`not_selected`/`no_recognized_columns`) у блоці Scope звіту. Живо: вибір лише Q3 з LAXTON дає 13 items/23 findings без жодного крос-аркушевого `url-duplicate`; `cross_sheet=false` на Q2+Q3 прибирає всі 11 крос-аркушевих дублів, `cross_sheet=true` повертає їх.
3. **Фільтр/сортування/групування** (`content-qa-render.js`, повністю переписаний view-model): чипи за чеком і аркушем із лічильниками, пошук, `Group by`/`Sort`, згортання великих груп (`limitPerGroup=50`), два різні порожні стани (clean vs no-matches-by-filter). Той самий рушій обслуговує й Framework Check.
4. **Пікер клієнта + автолокаль** (за прямим запитом користувача, §1.6): `guessContentQaClient()`/`guessLocaleForDomain()` у спільному render-модулі (навмисно продубльовано з `alt-text-tool.js`, не імпортовано — той файл поза owned paths цієї таски). Живо: завантаження LAXTON-файлу без ручного вибору підбирає клієнта `LAXTON` і локаль `sv-SE`; ручна зміна локалі до/після вибору клієнта не перетирається повторним автопідбором.

### Як зроблено (ключові рішення)

- **Швецький лексикон живе в цьому репо** (`app/contentqa/lexicon/sv.py`), не в AISeoTools — обґрунтування в §1.1 плану; реєстр (`get_lexicon`) лінькво імпортує модуль мови, тож наступна мова — один новий файл, нуль правок у `rules.py`/`policy_rules.py`.
- **Keyword-in-title для не-англійської — власний стем-матч**, не перенесення AISeoTools' `_related_topic_token` (поріг «5 спільних символів» калібрований на довших коренях і майже дорівнює точному збігу для коротких шведських слів — `mössor`/`Mössa` дають лише 4 спільні символи до розбіжності). Власний поріг (`common>=3`, `common/shorter>=0.6`) відкалібровано на цьому самому прикладі.
- **LIX (Björnsson), не Flesch-Kincaid, для нелатинських мов**: банд 30–45 відкалібровано на 141 живому описі laxton.se (медіана 34.8, p10 22.0, p90 45.0), а не взято з підручника. Під час реалізації знайдено і виправлено реальний баг: перша версія `lix()` перевикористовувала ASCII-only `_WORD_RE` з Flesch-Kincaid-коду, що ріже слова на літерах å/ä/ö (`"är"` → `"r"`) і системно занижує LIX на справжньому шведському тексті — виправлено окремим Unicode-safe `_UNICODE_WORD_RE`, використаним і для LIX, і для загального підрахунку слів у реченні (`sentence-too-long`).
- **Скоуп крос-сторінкових перевірок за аркушем** — нове поле `ContentItem.sheet` (адитивне, `None` для DOCX) замість парсингу `ref` рядком; `_sheet_groups()` у `rules.py` — спільна точка для `check_cross_page`/`check_meta_duplicates`.
- **`/inspect` — stateless, не чернетковий upload-стор**: файл ≤10 МБ, рішення одне (які аркуші), прогін — секунди; друга передача байтів дешевша за диск+TTL+прибирання, яке виправдане в Alt Text (там багатокрокове мапування колонок і дедуп за sha256).
- **Головні файли:** `app/contentqa/lexicon/{__init__,sv}.py` (нові), `app/contentqa/{engine,model,rules,policy_rules,readability}.py`, `app/contentqa/readers/xlsx.py`, `app/api/tools_content_qa.py`, `app/schemas/content_qa.py`, `app/db/{models,repositories/content_qa}.py`, `migrations/versions/c50ce678bee1_content_qa_sheets_task_87.py`, `frontend/js/{content-qa-render,strategy-check-tool,framework-check-tool}.js`, `frontend/css/content-qa.css`.
- **Відхилення від плану:** жодних суттєвих. Точне число "136→~0" `voice-em-dash` у §5.3 виявилось "136→37" на реальному файлі — сам воркбук (плановий, ще не опублікований контент) містить 37 справжніх em dash, яких немає в уже живому копірайті `laxton.se` (звідки бралась початкова оцінка «~0»); це не дефект фікса, а різниця між двома джерелами даних, використаними в дослідженні.

### Контрактні зміни (Contract changes)

- **Схема:** `ContentQaLocale` += `"sv-SE"`; нові `ContentQaSheetState`, `ContentQaSheetDTO`, `ContentQaSheetPreviewDTO`, `ContentQaInspectDTO`; `ContentQaProfileDTO` += `sheets`/`cross_sheet`; `ContentQaReportDTO` += `sheets` (усе адитивно, старі поля не змінені).
- **API:** новий `POST /api/v1/tools/strategy-check/inspect`; `POST …/check` += query-параметри `sheet` (0+) і `cross_sheet` (дефолт `true` — сумісність зі старими викликами без цих параметрів). Framework Check не змінився.
- **БД:** нова nullable колонка `content_qa_reports.sheets_json` (`c50ce678bee1`, застосована після `pg_dump`-бекапу). Старі рядки: `NULL` → DTO віддає `[]`.
- **Аудит:** новий `ActionSpec("tool.strategy_check.inspect")` для `/inspect` (без нього падав guard `test_actions_cover_routes`).
- **OpenAPI-знімок** (`tests/unit/api/data/openapi_snapshot.json`) регенеровано — diff обмежений новим ендпоінтом і новими DTO, нічого стороннього.

### Як перевірити (verification)

```bash
docker compose exec api poetry run pytest tests/unit/contentqa tests/unit/api/test_tools_content_qa.py tests/unit/audit -q   # 142 passed
docker compose exec api poetry run ruff check app/contentqa app/api/tools_content_qa.py app/schemas/content_qa.py app/db/repositories/content_qa.py app/audit/actions.py app/db/models.py tests/unit/contentqa tests/unit/api/test_tools_content_qa.py
docker compose exec api poetry run mypy app
```

Живо (UI, потрібен реальний `*_SEO_STRATEGY_*.xlsx`, бажано швецький домен на кшталт `laxton.se`): відкрити `#/tools/strategy-check`, завантажити файл → клієнт і локаль підбираються з імені файлу/домену, зʼявляється пікер аркушів (якщо ≥2 несучих) → зняти частину аркушів, лишити `cross_sheet` вимкненим → Run check → у звіті блок «Sheets in this run» показує стани `Checked`/`Not selected`; фільтр за чеком (клік по чипу, «All»/«None»), пошук, `Group by: Check type` перегруповують список без перезавантаження сторінки. Node-Playwright сценарій (10 живих перевірок, включно з CSP/console-помилками) прогнано проти реального контейнера — усі пройшли, скріншот форми і звіту зроблено.

### Тести

- **Нові файли:** `tests/unit/contentqa/test_lexicon_sv.py` (реєстр + шведські списки), `tests/unit/contentqa/test_locale_gating.py` (по тесту на кожен рядок мапи лейнів §1.2, регресійний якір для `en-US`).
- **Розширені:** `test_readability.py` (LIX-формула, Unicode-баг з å/ä/ö, F-K не викликається для sv), `test_readers_xlsx.py` (вибір аркуша, `not_selected` vs `no_recognized_columns`, `UnknownSheetError`, `inspect_strategy_xlsx`), `test_rules.py`/`test_policy_rules.py` (скоуп `cross_sheet` для обох крос-перевірок), `test_tools_content_qa.py` (`/inspect`, `sheet`/`cross_sheet` query-параметри, зворотна сумісність `sheets_json IS NULL`).
- Живий Playwright-сценарій (не в репо — одноразовий верифікаційний скрипт у сесійному scratchpad) покриває UI-шар: автопідбір, пікер аркуша, фільтри, групування, Framework Check регресію, консоль/CSP.

### Follow-ups / борг

- Повноцінна мапа `TLD → locale` понад `.se` — `TODO(task-88?)`, коли зʼявиться другий не-англомовний клієнт.
- Наступна мова (напр. RU) — перевірити, що додається одним файлом лексикону без правок у `rules.py`.
- Експорт findings (CSV/XLSX), шаринг фільтрованого вигляду через URL, `kind` (page/blog) у формі Strategy Check — свідомо поза скоупом, див. §1.7 плану.
