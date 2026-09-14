# task-96 — Окрема вкладка «Links»: усі таблиці посилань в одному місці

**Depends on:** 90 ✅, 91 ✅, 92 ✅, 95 ✅
**Owned paths:** `app/contentqa/matrix_xlsx.py`, `frontend/js/strategy-package-tool.js`,
`frontend/css/strategy-package.css`, `tests/unit/contentqa/test_matrix_xlsx.py`,
`tests/e2e/frontend/tools-strategy-package.cjs`, адитивно `frontend/js/tools-page.js`,
`frontend/index.html`, і за живим доповненням користувача (§2.6) — адитивно
`app/schemas/strategy_package.py`, `app/api/tools_strategy_package.py`,
`tests/unit/api/test_tools_strategy_package.py`, `tests/unit/api/data/openapi_snapshot.json`.
**Міграцій немає.** Основна частина (аркуш/вкладка `Links`) не чіпає контракт — усі дані
вже віддає `MatrixRowDTO.links` (task-95), це суто **подача**. Живе доповнення §2.6
(«Open strategy») додало одне нове **адитивне** поле `source_file_id` до
`ContentQaPackageSummaryDTO` — DoD 12 нижче скориговано під це.

---

## 1. Дослідження (заміряно живо 2026-09-14, не з голови)

Скрипти замірів — `.verify_tmp/probe_xlsx_sheets.py`, `.verify_tmp/probe_links_depth.py`.

### 1.1 Блок `Links` існує, але розсипаний по аркушах

task-95 §2.7 поклав блок `Links (Framework -> Copy -> Site)` **у низ кожного per-page
аркуша `Copy`**. Побудова реального Excel для `LAXTON_SEO_STRATEGY_04142926`
(пакет `92f980f5`, 12 фреймворків) дає:

```
sheets: 15   Comparison / Mismatches / Copy / + 12 per-page аркушів
усі 12 per-page аркушів мають блок Links
```

і ось на якому рядку той блок починається:

| Аркуш | Рядок блоку `Links` | Усього рядків |
|---|---|---|
| utrustning | 7 | 14 |
| ouija-board-by-laxton | 9 | 13 |
| rorelsesensor | 7 | 11 |
| x5-emf-matare-svart | 20 | 24 |
| k2-emf-matare-2 | **50** | 57 |
| mel-meter | **52** | 59 |
| pmb | 46 | 49 |
| prs | 47 | 50 |
| p-sb7t | **52** | 55 |
| ovilus-5 | 45 | 50 |
| 3h-ghost-livestream | 8 | 12 |
| hemsokt-museum | 21 | 26 |

**Медіана — рядок 45.** Щоб побачити картину посилань по одному клієнту, оператор має
відкрити 12 різних аркушів і в кожному прокрутити повз таблицю розбіжностей речень.
Саме це й просить користувач прибрати: «така таблиця до кожної сторінки **на одній вкладці**».

### 1.2 Скільки взагалі є даних (скан усіх 49 персистованих пакетів)

```
packages fetched: 49   with a link audit: 5
pages with a link audit: 17
pages whose link table is INVISIBLE in xlsx today: 0
pages with skipped-only (no LinkRow): 6
```

Три висновки, кожен стає вимогою:

1. **44 з 49 пакетів мають `links = None`** (персистовані до task-95). Аркуш `Links` для
   такого звіту мусить сказати це словами, а не показати порожню сітку (PLAN §12:
   «перевірено-чисто ≠ не перевірено»).
2. **Нуль сторінок сьогодні втрачають таблицю зовсім** — тобто це не баг видимості, а
   проблема **розсіяності**. Задача — консолідація, не порятунок даних. Чесно фіксую:
   гейт `with_sheet` (task-95) структурно ще може сховати сторінку з чистою прозою і чистим
   ланцюгом, але на живому корпусі такого випадку немає; нова вкладка прибирає цей ризик
   як побічний ефект, бо не залежить від `copy_diffs` узагалі.
3. **6 сторінок мають лише `skipped`, жодного `LinkRow`** — блок для них не порожній,
   у ньому лише рядки «Skipped: …». Аркуш має їх показувати, інакше зникне інформація
   «фреймворк щось приписав, але цілі не видобулось».

### 1.3 Пастка: `strategy_only` рядки — 33 з 35

`EFFYS_SEO_STRATEGY_061126` (пакет `834f50fb`): `Counter({('strategy_only', links=None): 33,
('matched', links=set): 2})`. LAXTON: 13 рядків матриці, 12 аудитів, 13-й — `strategy_only`
рядок `CPC` без `framework_document_id`.

Тобто **наївне «блок на кожен рядок матриці» дало б 33 блоки шуму на 2 корисні**.
Правило: у вкладку `Links` потрапляють лише рядки, де є фреймворк
(`kind != "strategy_only"`), решта не згадується взагалі — це не «не перевірено», у них
просто немає сторони, яку можна аудитувати.

### 1.4 Що вже є в UI

Фронт показує ланцюг у двох місцях: дві колонки-бейджі `In copy`/`On site` на шапці групи
сторінки (`renderMatrixVerdictBadges`) і згорнутий `<details class="sp-links-details">`
з 4-колонковою таблицею під міні-таблицями (`renderLinkDetails`) — усе всередині вкладки
**Comparison**, тобто так само розсіяно по групах сторінок. Наявні вкладки:
`Comparison / Overview / Strategy / Frameworks / Copy / Cross-checks`.

### 1.5 Форма даних (без змін, лише споживається)

`MatrixRowDTO.links: LinkAuditDTO | None` → `state` (`checked`/`not_applicable`), `reason`,
`in_copy`/`on_site` (`MatrixFieldVerdictDTO` зі `state` + `detail` виду
`"1/3 copy link(s) on the live page"`), `rows: list[LinkRowDTO]`
(`target`/`display`/`origin`/`in_copy`/`on_site`/`note`), `skipped: list[str]`.
Плюс на рядку вже є `framework_document_id` / `framework_document_name` (task-91) — саме
з них береться **посилання на фрейм**, і `label` — **посилання на сторінку**.

---

## 2. Архітектура

### 2.1 Excel: новий аркуш `Links`

Порядок аркушів: `Comparison` · `Mismatches` · **`Links`** · `Copy` · per-page.
Один **блок на сторінку**, у порядку матриці, блоки розділені порожнім рядком:

```
https://www.laxton.se/shop/utrustning/                     ← гіперлінк на сторінку (bold 13)
Framework: LAXTON_utrustning_framework                     ← гіперлінк на Google Doc
In copy: ✗ 2/3 framework link(s) in copy · On site: ✗ 1/3 copy link(s) on the live page
Copy sheet: utrustning                                     ← внутрішній лінк #'utrustning'!A1 (якщо аркуш є)

 Link                                    | In copy | On site | Note
 utrustning för spökjakt → /shop/utrust… |  ✗ no   |  ✓ yes  |
 Paranormal Motion Alert (PMA) → /shop/… |  ✗ no   |  ✗ no   |
 Skipped: Hemingway Editor (instruction, skipped)
```

«Посилання на сторінку та фрейм» з формулювання користувача — це саме рядки 1–2 шапки
блоку, точно як їх уже робить `_copy_page_sheet`.

### 2.2 Чесні стани блоку (жодного вигаданого рядка)

| Ситуація | Що в блоці |
|---|---|
| `links is None`, `kind != "strategy_only"` | шапка + курсивом `This run predates the link audit (task-95) — re-run the check to get one.` Таблиці немає |
| `kind == "strategy_only"` | блоку немає взагалі (немає фреймворка — немає що звіряти) |
| `state == "not_applicable"` | шапка + курсивом `reason` (`No links to compare: …`) + рядки `Skipped:`, якщо є |
| `not_comparable` вердикт | у рядку зведення пишемо `detail` («no copy tab in this document», «site not checked»), **не** символ — те саме правило, що в колонках `In copy`/`On site` на `Comparison` |
| у пакеті жодного аудиту | увесь аркуш = один курсивний рядок, не порожня сітка |

### 2.3 Рефактор, не копіпаст

Тіло блоку сьогодні вшите в `_copy_page_sheet` (рядки ~592–616). Виносимо в спільний
`_links_block(ws, r, link_audit, *, width) -> int` (повертає наступний вільний рядок) і
кличемо з обох місць. **Блок на per-page аркушах лишається** — копірайтеру, який
править одну сторінку, він потрібен саме там, у контексті; нова вкладка — це додатковий
крос-сторінковий зріз, не переїзд. (Якщо оператор скаже, що дубль зайвий, прибрати його
потім — один рядок виклику.)

### 2.4 Порядок створення аркушів (неочевидна деталь)

`build_copy_sheets` рахує `used = {name.lower() for name in workbook.sheetnames}`, щоб
per-page назви не колідували. Тому `Links` створюємо **до** нього
(`workbook.create_sheet("Links", index=2)`) — тоді сторінка з URL `/links/` автоматично
отримає `links (2)`, без окремої правки. Але внутрішні лінки `Copy sheet: …` відомі лише
**після** `build_copy_sheets`. Рішення: `build_copy_sheets` повертає
`dict[str, str]` (`document_id -> назва primary-аркуша`), `build_matrix_xlsx` створює
порожній `Links` на позиції 2, викликає `build_copy_sheets`, і аж тоді наповнює `Links`
з отриманою мапою. Якщо на стадії реалізації це виявиться крихким — рядок
`Copy sheet:` просто не рендериться (єдине, що втрачається, — зручний перехід).

### 2.5 UI: нова вкладка `Links`

Кнопка між `Copy` і `Cross-checks`, іконка `link`. `renderLinksTab(panel, pkg)`:
одна картка на сторінку — шапка з лінком на сторінку, лінком на Google Doc фреймворка і
двома бейджами вердиктів (перевикористати `renderMatrixVerdictBadge`), під нею та сама
4-колонкова таблиця. Таблицю виносимо з `renderLinkDetails` у `renderLinkTable(links)` і
кличемо з обох місць — розмітка одна. Тулбар вкладки — чекбокс «Mismatches only»,
дзеркало наявного на `Comparison` (окрема змінна стану, не спільна).
`<details>` на `Comparison` лишається як є.

### 2.6 Живе доповнення: «Open strategy» на Comparison

Під час реалізації користувач попросив мідсесії: «Додай ще посилання на саму стратегію у
першій вкладці» — тобто лінк на сам strategy-документ (Google Sheet/xlsx на Диску), не
лише на фреймворки. Заміряно: `ContentQaPackage` (модель БД) уже зберігає
`source_file_id` (task-90, `default=None`), але жоден DTO його не віддавав — ні
`ContentQaPackageSummaryDTO`, ні звідси успадкований `ContentQaPackageDTO`. Рішення:
додаю **одне нове опційне поле** `source_file_id: str | None = None` до
`ContentQaPackageSummaryDTO` (адитивно — старі персистовані пакети мають `None` там, де
колонка не була заповнена, це не баг), заповнюю з `package.source_file_id` в
`_package_summary_dto`. У Comparison-тулбарі (перед «Export to Excel») — кнопка
`Open strategy`, URL залежить від `source_kind`: `spreadsheet` → Google Sheets edit-лінк,
інакше (`xlsx_blob` тощо) → Google Drive file-view лінк (те саме, що вже робить
`resolve`/`inspect` для показу файлу). Кнопка не рендериться, коли `source_file_id`
відсутній (старий пакет) — чесний стан, не мертвий лінк.

---

## 3. Етапи

| # | Що | Файли |
|---|---|---|
| A | Винести `_links_block`, перевести `_copy_page_sheet` на нього (поведінка ідентична — тести task-95 мають лишитись зеленими без правок) | `matrix_xlsx.py` |
| B | `build_links_sheet(ws, rows, *, copy_sheets)` + чесні стани §2.2 | `matrix_xlsx.py` |
| C | `build_copy_sheets` повертає мапу; `build_matrix_xlsx` створює `Links` на `index=2` і наповнює після | `matrix_xlsx.py` |
| D | Юніт-тести аркуша | `tests/unit/contentqa/test_matrix_xlsx.py` |
| E | Фронт: `renderLinkTable` винести, `renderLinksTab` + кнопка + фільтр | `strategy-package-tool.js` |
| F | CSS картки вкладки (тон лише класами, без inline `style=`) | `strategy-package.css` |
| G | Cache-bust `?v=` для обох змінених файлів + e2e | `tools-page.js`, `index.html`, `tools-strategy-package.cjs` |
| H | Візуальний loop 1440/390, DoD, перенесення в `done/` + Completion report | — |

---

## 4. Ризики

- **Дублювання блоку** (вкладка + per-page аркуш) — свідоме, §2.3; ризик у тому, що
  розійдуться при майбутній правці. Мітигація: один спільний `_links_block`.
- **Розмір аркуша**: 12 сторінок × ~8 рядків = ~100 рядків. Стратегія на 40 сторінок дасть
  ~350 — прийнятно; нічого не ріжемо і не ставимо капів (капи на посилання вже прибрано в task-95).
- **Порожній аркуш для 44 старих пакетів** — закрито §2.2, але це головне, що треба
  перевірити живо: відкрити саме старий звіт і побачити речення, а не сітку.
- **Колізія назви аркуша** `Links` — закрито порядком створення §2.4; окремим тестом.
- **`role == "secondary"` вкладки копії**: аудит рахується проти primary (task-95 §2.3),
  тож у нової вкладки **одна** таблиця на сторінку, не по одній на мовну вкладку. Не
  «загубили дані» — вторинна вкладка не має свого ланцюга за побудовою.

---

## 5. Definition of Done

1. В Excel є аркуш `Links` третім за порядком; на ньому по одному блоку на кожну сторінку
   з фреймворком, з гіперлінками на сторінку і на Google Doc.
2. `strategy_only` рядки на аркуш не потрапляють (перевірено на EFFYS: 2 блоки, не 35).
3. Старий пакет (`links=None`) дає речення «predates the link audit», не порожню сітку —
   перевірено живо на одному з 44 таких звітів.
4. `not_applicable` і `skipped`-only сторінки показані з причиною (перевірено на LAXTON:
   6 таких сторінок).
5. `not_comparable` рендериться текстом `detail`, не символом.
6. Блок на per-page аркушах `Copy` лишився й ідентичний — старі тести task-95 зелені **без правок**.
7. В UI є вкладка `Links` з карткою на сторінку і фільтром «Mismatches only».
8. Жодних inline `style=` (CSP), жодних emoji, UI-копірайт англійською.
9. `ruff check .`, `mypy app`, `pytest tests/unit/contentqa -q` — зелені в Docker.
10. e2e `tools-strategy-package.cjs` зелений, включно з новою перевіркою вкладки.
11. Візуальний loop 1440 і 390 — таблиця не виносить сторінку горизонтально
    (`contain: paint` на `.table-scroll`, відомий дефект).
12. Контракт лишається зворотно сумісним: `Links`-вкладка/аркуш узагалі не чіпають API;
    єдина зміна контракту — адитивне поле `source_file_id` (§2.6). `openapi_snapshot.json`
    регенерується й показує рівно цей один доданий `anyOf[str, null]` — жодних інших
    відмінностей.
13. Comparison-тулбар має кнопку `Open strategy` з правильним хостом
    (`docs.google.com/spreadsheets` для `spreadsheet`, `drive.google.com/file` інакше) —
    перевірено живо на SHB (Google Sheet) + e2e-перевіркою.

---

## 6. Follow-ups

- Якщо дубль блоку на per-page аркушах виявиться зайвим — прибрати (рішення оператора).
- Зведений підсумок зверху аркуша (`N pages · M broken links`) — не в цій таксі, спершу
  подивитись на живий аркуш.

---

## ✅ Completion report

- **Дата:** 2026-09-14
- **Гілка / комміт:** feat/task-96-links-sheet-tab @ 5ec2601
- **Статус:** done

### Що зроблено
- Excel: новий аркуш `Links` (третій, після `Mismatches`) — один блок на сторінку
  (гіперлінк на сторінку + гіперлінк на Google Doc фреймворка + зведення `In copy`/
  `On site` + лінк на per-page аркуш `Copy`, якщо є + сама таблиця посилань), у порядку
  матриці, без `strategy_only`-шуму. Блок на per-page аркушах `Copy` лишився ідентичним
  (спільний `_links_block`).
- UI: нова вкладка **Links** (між Copy і Cross-checks) — картка на сторінку, той самий
  4-колонковий стіл (`renderLinkTable`, винесений зі старого `renderLinkDetails`), чекбокс
  «Mismatches only», кнопка «Export to Excel». Чесні стани §2.2 реалізовані 1:1
  (predates the audit / not_applicable з причиною / no links to compare).
- Живе доповнення користувача мідсесії (§2.6): кнопка **Open strategy** на Comparison-тулбарі
  — лінк на сам strategy-документ (Google Sheet чи Drive-файл, залежно від `source_kind`).

### Як зроблено (ключові рішення)
- `_links_block(ws, r, link_audit, *, width)` — винесено з `_copy_page_sheet`, викликається
  звідти і з нового `build_links_sheet`; поведінка per-page блоку не змінилась (тести
  task-95 зелені без жодної правки їхніх очікувань, окрім зсуву `sheetnames`).
- `build_copy_sheets` тепер повертає `dict[document_id, primary_sheet_title]` — міст назад
  для рядка `Copy sheet: …` на новому аркуші; `build_matrix_xlsx` створює порожній `Links`
  на позиції 2 (`index=2`) **до** виклику `build_copy_sheets`, щоб застовпити ім'я аркуша
  проти дедуп-набору `used` (сторінка з URL `/links/` перевірена — колізії нема,
  `.verify_tmp`-скрипт підтвердив живо, видалений після перевірки).
- Фронт: `renderLinkTable(links)` — спільна таблиця для `<details>` на Comparison і нової
  вкладки; `GDOC_URL`/`strategySourceUrl` — локальні URL-білдери (дзеркалять
  `matrix_xlsx.py:_gdoc_link`), раніше в жодному фронт-модулі такого лінка не було.
- Живе доповнення (Open strategy) відкрило дірку в контракті: `ContentQaPackage` (БД)
  завжди зберігав `source_file_id`, але жоден DTO його не віддавав. Додав поле до
  `ContentQaPackageSummaryDTO` (адитивно, `None`-дефолт), заповнив у `_package_summary_dto`.
- Головні файли: `app/contentqa/matrix_xlsx.py` (аркуш Links + рефактор), 
  `frontend/js/strategy-package-tool.js` (вкладка Links + Open strategy), 
  `frontend/css/strategy-package.css` (`.sp-links-doc`/`.sp-links-empty`), 
  `app/schemas/strategy_package.py` + `app/api/tools_strategy_package.py` (`source_file_id`).

### Відхилення від плану
- План §2.6 з'явився мідсесії (не в первинному дослідженні) — очікувано, бо це було живе
  прохання користувача під час реалізації, задокументоване і закрите тим самим прогоном
  верифікації.
- DoD-пункт 12 плану («контракт не змінюється») довелось скоригувати на «лишається
  зворотно сумісним» через §2.6 — сама вкладка/аркуш Links контракту дійсно не чіпають,
  зміна прийшла з окремого, пізнішого запиту.

### Контрактні зміни (Contract changes)
- `ContentQaPackageSummaryDTO.source_file_id: str | None = None` (нове, адитивне поле,
  успадковане і `ContentQaPackageDTO`) — Drive file id стратегії, для лінка «Open strategy».
  `openapi_snapshot.json` регенеровано, диф — рівно цей доданий `anyOf[str, null]`
  у двох місцях схеми (Summary + успадкований Detail).
- `MatrixRowDTO`/`LinkAuditDTO`/`LinkRowDTO` — без змін (усе вже було в task-95).

### Як перевірити (verification)
```bash
docker compose exec api poetry run pytest tests/unit/contentqa tests/unit/api/test_tools_strategy_package.py tests/unit/api/test_openapi_snapshot.py -q
docker compose exec api poetry run ruff check .
docker compose exec api poetry run mypy app
NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 node tests/e2e/frontend/tools-strategy-package.cjs
```
У UI: відкрити будь-який Strategy Package Check звіт → вкладка **Links** → картки по
сторінках з таблицею `In copy`/`On site`; на **Comparison** — кнопка «Open strategy» у
тулбарі поруч з «Export to Excel»; **Export to Excel** → аркуш `Links` третім за порядком.

### Тести
- `tests/unit/contentqa/test_matrix_xlsx.py`: +6 нових тестів для `build_links_sheet`
  (пропуск `strategy_only`, чесність `predates the audit`/`not_applicable`/порожньо,
  таблиця + `Copy sheet:`-лінк, порожній прогін), + оновлені `sheetnames`-очікування у
  8 наявних тестах (зсув на `"Links"`).
- `tests/unit/api/test_tools_strategy_package.py`: +1 асерт (`source_file_id`) в
  існуючому тесті `/check`; оновлено `sheetnames` в xlsx-download тесті.
- `tests/unit/api/data/openapi_snapshot.json`: регенеровано, диф підтверджено точковим.
- `tests/e2e/frontend/tools-strategy-package.cjs`: новий крок 8b (вкладка Links — заголовки
  таблиці, фільтр Mismatches only) + асерт на кнопку «Open strategy» (крок 6c-ter).
- Живо: LAXTON (12 фреймворків, 8 сторінок з реальними лінками + 4 not_applicable),
  EFFYS (35 рядків матриці → 2 блоки в Links, 33 `strategy_only` прибрано), старий пакет
  без link audit (`links=None` на всіх 12 сторінках → чесне речення на кожній), колізія
  імені аркуша `/links/` перевірена й закрита.

### Follow-ups / борг
- Дублювання блоку Links (вкладка + per-page аркуш Copy) лишено свідомо (§2.3) — прибрати,
  якщо оператор скаже, що дубль зайвий.
- Зведений підсумок зверху аркуша/вкладки (`N сторінок · M зламаних лінків`) — не в цій
  таксі.
- Живо підтверджено, але поза owned paths і поза скоупом: горизонтальне переливання на
  390px на LAXTON-пакеті (`302 Errors`/`106 Warnings` — тризначні лічильники) — існує вже
  на вкладці Comparison **до** будь-якого дотику до Links (перевірено вимкненням Links),
  відсутнє на SHB (малі числа); джерело — `.cq-summary`/шапка звіту в
  `content-qa.css`/`content-qa-render.js`, не `strategy-package.css`/`.js`. Той самий клас
  дефекту, що вже задокументований у цьому e2e-файлі для масштхеда — не виправляю, бо поза
  owned paths цієї задачі.
