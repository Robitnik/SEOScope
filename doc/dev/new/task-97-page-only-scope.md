# task-97 — Однорідний скоуп прогону: перевірка **лише сторінок**

**Depends on:** 90 (живе), 91 (живе), 92 ✅, 95 ✅, 96 ✅
**Owned paths:** `app/contentqa/readers/tabular.py`, `app/contentqa/readers/gsheet.py`,
`app/contentqa/readers/gdoc.py`, `app/contentqa/package.py`,
`app/api/tools_strategy_package.py`, `app/schemas/strategy_package.py`,
`frontend/js/strategy-package-tool.js`, `frontend/css/strategy-package.css`,
`tests/unit/contentqa/{test_readers_xlsx,test_readers_gsheet,test_package}.py`,
`tests/unit/api/test_tools_strategy_package.py`,
`tests/unit/api/data/openapi_snapshot.json`,
`tests/e2e/frontend/tools-strategy-package.cjs`.
**Міграцій немає.** Нове поле `role` їде в уже наявних JSON-колонках
(`sheets_json`) і в DTO — схема БД не змінюється. Старі пакети без `role` → `None`,
не додумуємо заднім числом.

---

## 1. Дослідження (заміряно живо 2026-09-14 на бойовій БД — 63 пакети, 8 клієнтів)

### 1.1 Тул задуманий як page-only, але в 68% прогонів скоуп змішаний

```
mixed_scope (блоговий + сторінковий аркуш одночасно checked): 43
blog_only                                                   :  0
page_only                                                   : 20
total                                                       : 63
```

`blog_only = 0` — окремої перевірки блогів **ніхто ніколи не робив**: тул де-факто
page-only за наміром. Але дефолт форми тягне в скоуп усе несуче, і в 43 із 63 прогонів
блоговий аркуш таки увійшов у перевірку сторінок.

### 1.2 Що саме бачить оператор (живий `/inspect`, `3XE_SEO_STRATEGY_08032026`)

| Аркуш | Несучий | Рядків | Розпізнані колонки |
|---|---|---|---|
| `Structure / Features / Description` | так | 11 | page_name, url, primary_key, title, description, h1 |
| `Q3 2026 Strategy` | так | 21 | intent, page_name, **url**, primary/secondary/longtail_key, h1, title, description, **framework_link** |
| `Blog` | так | 11 | page_name, **keywords_csv**, title, description, internal_links, **framework_link** |

Кандидатів — **36**: 16 зі сторінкового аркуша, **11 із `Blog`**, 9 із резервного пошуку
по Диску. Дефолт форми ([strategy-package-tool.js:329-332](../../frontend/js/strategy-package-tool.js#L329-L332)):

```js
wizard.selectedSheets = new Set(inspection.suggested_sheets);          // ВСІ несучі → 3 з 3
wizard.selectedFrameworks = new Map(candidates.filter(c => c.origin === "strategy_link")); // 27 з 36
```

Тобто щоб запустити перевірку **лише сторінок**, оператор мусить щоразу зняти
**2 аркуші + 11 чекбоксів фреймворків = 13 ручних дій**, у плоскому списку без
групування. Саме це користувач і описує: «вкладку Блог я вручну знімаю… їх теж вручну
треба вимикати… інколи вони перепутуються».

### 1.3 Чим це коштує, коли «перепутується» (пакет SHB `2439c0ec`, 41 фреймворк)

Аркуші вибрані **правильно** (лише `Q3-Q4 2026 Strategy`, 20 рядків; обидва блогові
аркуші зняті), але блогові фреймворки зняти забули:

```
блогових фреймворків у прогоні : 21 з 41  (51%)
рядків матриці kind=matched    : 20
рядків матриці kind=framework_only : 21   ← рівно ті блоги
```

Findings, які ці 21 блогових фреймів згенерували **за правилами сторінки**:

| check | severity | n |
|---|---|---|
| `sync-url-unresolved` | warn | **21** |
| `duplicate-heading` | warn | 21 |
| `voice-em-dash` | error | 19 |
| `framework-tab-unnamed` | info | 17 |
| `meta-title-length` | warn | 13 |
| `internal-links-below-min` / `-above-max` | warn/info | 3 / 4 |
| решта | — | 25 |

`sync-url-unresolved` рівно 21 — тобто **кожен** блоговий фреймворк не зміг зіставитись
ні з чим (блогові фрейми не несуть URL сторінки), впав у `not_checked` для звірки
Frame↔Strategy і осів у матриці як `framework_only`. Ціна одного такого прогону:

- **51% таблиці порівняння — сміття**, що його оператор мусить очима відфільтровувати;
- **123 findings за хибним профілем**: `Profile.kind` — один на весь прогін
  ([rules.py:32](../../app/contentqa/rules.py#L32)), тож блоги перевірені сторінковими
  порогами, хоча для них у `rules.py` є власні банди (`BLOG_KEYWORDS_MIN/MAX`,
  `BLOG_H2_MIN`, `FAQ_MIN`, `LSI_MIN`, `LINKS_BY_INTENT`);
- **21 зайвий виклик Docs API + 21 завантаження живої сторінки** на прогін.

### 1.4 Назви аркушів стабільні по портфелю — на них можна спиратись

Усі аркуші всіх 8 клієнтів, що колись потрапляли в прогони:

| Роль | Живі назви |
|---|---|
| блог | `Blog` (3XE), `Blogs` (EFFYS, LAXTON, NYCHA, NYSAPH, SDS, SWM, SHB), `Blogs Q3-Q4` (SHB) |
| сторінки | `Q3 2026 Strategy`, `Q2 2026 Strategy`, `Q4 2026 Strategy`, `Q3-Q4 2026 Strategy`, `Q4 2026` |
| структура | `Structure / Features / Description`, `Structure` |
| інше | `Keywords` (SHB, `no_recognized_columns`) |

Два висновки, які дизайн мусить витримати:
- **SHB має ДВА блогові аркуші**, **LAXTON — два сторінкові** (`Q2` + `Q3`). Не можна
  припускати «рівно один аркуш на роль».
- Назва — стабільний сигнал (`^blogs?\b`, `^structure\b`, `Q[1-4]`/`strategy`),
  але має бути фолбек: аркуш, що не підпав під жоден патерн, лишається `other` і
  поводиться як сьогодні.

### 1.5 А ось назви ДОКУМЕНТІВ — ненадійні, класифікувати за ними не можна

```
3XE, SDS, SHB, EFFYS : <CLIENT>_FRAMEWORK_*
LAXTON, NYCHA, NYSAPH, SWM, SDS, EFFYS : <CLIENT>_PAGE_FRAMEWORK_*
NYSAPH, SHB          : <CLIENT>_BLOG_*
EFFYS                : EFFYS_COLUMBUS_AVE, EFFYS_UWS      ← сторінкові фрейми БЕЗ слова FRAMEWORK
SWM                  : https://docs.google.com/document/d/1V058RQ…/edit?tab=t.0  ← display_text = сирий URL
```

Тому первинний критерій — **аркуш походження кандидата** (`FrameworkLinkCandidate.sheet`,
100% відомий для `origin="strategy_link"`), а ім'я документа — лише вторинний сигнал для
кандидатів із резервного пошуку по Диску, де аркуша немає взагалі.

### 1.6 Тихий фолбек, який і робить «перепутування» мовчазним

Коли обраний фреймворк посилається на **не обраний** аркуш
([package.py:404-405](../../app/contentqa/package.py#L404-L405)):

```python
known_row = strategy_by_ref.get(source.strategy_ref)   # "Blog!row 8" → None, аркуш не читався
```

`known_row=None` тихо перемикає `check_frame_strategy` у режим вгадування за URL/назвою
([crosscheck.py:118-155](../../app/contentqa/crosscheck.py#L118-L155)). Сьогодні це дає
`sync-url-unresolved` (див. §1.3), але клас помилки гірший за симптом: до фіксу
`0674487` рядки стратегії з плейсхолдером `-` у колонці URL схлопувались в один ключ, і
таке вгадування **прив'язувало фреймворк до випадкового чужого рядка стратегії**. Фікс
прибрав конкретну колізію; сам тихий фолбек лишився. Симетрична діра досі відкрита в
[gdoc.py:218-224](../../app/contentqa/readers/gdoc.py#L218-L224) — `_first()` віддає `"-"`
як справжнє значення `url`, бо `_NO_VALUE` там застосовано лише до рядків-посилань.

---

## 2. Дизайн

### 2.1 Роль аркуша — нова властивість скану, не здогадка форми

`tabular.py` отримує `sheet_role(name, columns) -> "page" | "blog" | "structure" | "other"`:

1. `^blogs?\b` → `blog`;
2. `^structure\b` → `structure`;
3. `\bQ[1-4]\b` або `\bstrategy\b` → `page`;
4. інакше — за колонками: є `url` **і** `intent` → `page`; є `keywords_csv` **і** немає
   `url` → `blog`; решта → `other`.

Роль їде в `SheetPreview`/`ContentQaSheetPreviewDTO` (крок 2) і в `sheets_json`
(персистенція прогону) — адитивно, старі пакети без неї малюються як сьогодні.

`suggested_sheets` перестає означати «всі несучі» і означає **«несучі з роллю `page`»**.
Страховка: якщо page-аркушів не знайшлось жодного — `suggested_sheets` лишається старим
(усі несучі), бо порожній дефолтний вибір гірший за широкий.

### 2.2 Роль кандидата — за аркушем, з якого він прилетів

`FrameworkCandidateDTO` отримує `role` (`page`/`blog`/`structure`/`other`/`unknown`):
для `origin="strategy_link"` — роль його аркуша (§2.1); для `origin="drive_search"` —
`unknown`, з підказкою за іменем (`_BLOG_` → `blog`), бо §1.5 забороняє довіряти імені
як вердикту.

Дефолт вибору у формі стає: `origin === "strategy_link" && role === "page"`. Для 3XE це
16 із 36 замість сьогоднішніх 27 — **0 ручних дій замість 13**.

### 2.3 UI: групи замість плоского списку

Крок 2 групує кандидатів за роллю, кожна група з власним лічильником і кнопками
All/None у її межах:

```
Pages · 16 of 16 selected                    [All] [None]
Blog · 0 of 11 selected — not part of a page check   [All] [None]
Found by name search · 0 of 9 selected               [All] [None]
```

Блокова група не ховається (оператор має бачити, що блоги існують і свідомо їх лишає
поза прогоном), але згорнута за замовчуванням. Аналогічно для аркушів: не-`page`
аркуші лишаються в списку зі своєю роллю в підписі.

### 2.4 Змішаний скоуп — видимий стан, а не тиха аномалія

Дві лінії захисту, обидві не блокують оператора:

1. **У формі:** щойно вибір містить і `page`, і `blog` — під кнопкою з'являється
   попередження: `Mixed scope: page and blog frameworks are checked with different rules
   (Framework type applies to the whole run).` Не помилка — свідомий вибір лишається
   можливим.
2. **На беку:** фреймворк, чий `strategy_ref` вказує на **не обраний** аркуш, більше не
   провалюється в режим вгадування. `run_package` віддає для нього
   `FrameworkOutcome(state="out_of_scope", reason="Linked from <sheet>, which is not in
   this run's sheet scope.")` — четвертий чесний стан у дусі `not_configured`/
   `not_applicable`/`not_selected`, **до** будь-яких викликів Docs API та завантаження
   сайту. Це і прибирає 21 сміттєвий `framework_only` рядок, і економить 21+21 зовнішній
   виклик на прогоні рівня SHB.

`out_of_scope` не потрапляє в матрицю взагалі (як і `not_accessible` сьогодні —
`build_matrix` пропускає все, крім `state == "checked"`), але видно у вкладці Frameworks
зі своєю причиною.

### 2.5 Симетричний фікс плейсхолдера у Frame-документі

`gdoc.py:_first()` (і `docx.py`-двійник) мають віддавати `None` для `url`, коли значення —
плейсхолдер із `_NO_VALUE`. Інакше фрейм із `Page: -` будує ключ `url:-` і лишається
кандидатом на хибний матч — рівно той клас багу, що фікс `0674487` закрив з боку
стратегії. Обсяг — тільки поле `url`; `title`/`description`/`h1` не чіпаємо (там `-`
може бути свідомим значенням, і для них хибний матч неможливий).

### 2.6 Чого цей план НЕ робить

Повноцінного **блогового прогону** (`kind="blog"` з блоговим аркушем, блоговими
фреймами і блоговими правилами) тут немає. `blog_only = 0` за всю історію (§1.1) — попиту
не заміряно, і вигадувати його під цю таску не будемо. Усе, що тут робиться, лише
**не заважає** такому прогону з'явитись пізніше: ролі, групи й `out_of_scope` працюють
однаково в обидва боки — достатньо буде дозволити `suggested` = `blog`-аркуші, коли
`Framework type = Blog framework`. Це окрема таска, якщо користувач її попросить.

---

## 3. Етапи

| # | Що | Файли |
|---|---|---|
| 1 | `sheet_role()` + роль у `SheetPreview`/`SheetScan`; `suggested_sheets` → page-аркуші з фолбеком | `readers/tabular.py` |
| 2 | Роль у `FrameworkLinkCandidate` і в кандидатах `/inspect`; `role` в `FrameworkCandidateDTO`/`ContentQaSheetPreviewDTO`/`ContentQaSheetDTO` | `readers/gsheet.py`, `api/tools_strategy_package.py`, `schemas/strategy_package.py` |
| 3 | `out_of_scope` state + гілка в `run_package` до зовнішніх викликів | `contentqa/package.py` |
| 4 | Плейсхолдер `-` у `url` Frame-документа → `None` | `readers/gdoc.py`, `readers/docx.py` |
| 5 | Групи кандидатів, дефолт page-only, попередження про змішаний скоуп | `frontend/js/strategy-package-tool.js`, `frontend/css/strategy-package.css` |
| 6 | Тести: юніти ролей/дефолтів/`out_of_scope`, оновлений openapi snapshot, e2e | `tests/**` |

---

## 4. Definition of Done / Gate

1. `/inspect` на `3XE_SEO_STRATEGY_08032026` віддає ролі: `Q3 2026 Strategy` → `page`,
   `Blog` → `blog`, `Structure / Features / Description` → `structure`;
   `suggested_sheets == ["Q3 2026 Strategy"]`.
2. Форма для 3XE відкривається з **16 обраними** кандидатами (усі `page`), 0 блогових,
   0 з drive-пошуку — **жодної ручної дії**, щоб запустити перевірку лише сторінок.
3. Ролі правильні для всіх 8 живих клієнтів §1.4, включно з двома блоговими аркушами SHB
   і двома сторінковими LAXTON (перевірка на збережених `sheets_json`, без витрат
   Google-квоти).
4. Аркуш, що не підпав під жоден патерн (`Keywords` у SHB), лишається `other` і
   поводиться як сьогодні; стратегія без жодного `page`-аркуша не дає порожній дефолт.
5. Фреймворк із `strategy_ref` на необраний аркуш дає `out_of_scope` **без** виклику
   Docs API і без завантаження сайту; у матриці його немає, у вкладці Frameworks — є з
   причиною.
6. Перепрогін SHB-скоупу з §1.3 (41 кандидат, сторінковий аркуш) дає 20 `matched` і
   **0** `framework_only` від блогів; findings блогових фреймів за page-правилами — 0.
7. Змішаний вибір усе ще можливий, але показує попередження §2.4.
8. Фрейм із `Page: -` не отримує ключ `url:-` (юніт на `gdoc.py`).
9. `docker compose exec api poetry run pytest tests/unit/contentqa tests/unit/api -q` —
   зелено; `ruff check .` і `mypy app` — чисто.
10. e2e `tests/e2e/frontend/tools-strategy-package.cjs` зелений, з новою перевіркою
    груп і дефолтного вибору.
11. Візуальний чекліст `frontend/AGENTS.md §15-16` пройдено (групи, згорнута блокова
    секція, попередження — у світлій і темній темі, на 400px).
12. Contract changes: `role` у `ContentQaSheetPreviewDTO`/`ContentQaSheetDTO`/
    `FrameworkCandidateDTO`, `out_of_scope` у переліку станів `FrameworkOutcomeDTO` —
    усе **адитивне**; `openapi_snapshot.json` оновлено, `task-02` синхронізовано.
