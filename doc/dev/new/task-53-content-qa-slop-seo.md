# task-53 — Content QA: два тули для перевірки стратегій і фреймворків (слоп + SEO-правила)

- **Пакет:** **два** нові інструменти у лейні Tools на спільному рушії — **Strategy Check** (оператор кидає SEO-стратегію, XLSX) і **Framework Check** (кидає page/blog-фреймворк, DOCX). Обидва повертають список findings: порушені SEO-правила (довжини meta, H1, ієрархія заголовків, лінки), AI-слоп у копірайті, порушення brand voice і читабельність. Замість «агент перечитав власний вивід» — детермінований прогін. Чому два тули, а не один із перемикачем — §4a.
- **Хвиля:** P — Content QA (4-й і 5-й інструменти у хабі Tools після task-27 Alt Text, task-46 Redirect Checker, Character Counter).
- **Depends on:** **27** ✅ (каталог Tools), **06** ✅ (REST), **13** ✅ (harness)
- **Не конфліктує** з task-51 (`app/monitor/**`) — зони не перетинаються.
- ⚠️ **Спільні файли досі зайняті паралельною сесією** (перевірено 2026-08-21, `git status` нечистий): `app/api/tools.py`, `frontend/js/tools-page.js`, `frontend/css/tools.css`, `frontend/fixtures/tools-catalog.json`, `frontend/index.html`, `frontend/js/router.js` — незакоммічені правки, що додають Character Counter. Це рівно ті файли, яких торкаються наші адитивні дотики (§4a: +2 записи в `_TOOLS`, +2 в `TOOL_MODULES`). Ізольована частина (`app/contentqa/**`, `app/api/tools_content_qa.py`, `app/schemas/content_qa.py`, фронт-модулі нижче) не перетинається — роби її; до спільних файлів вертайся, коли їх `git status` чистий.
- **Owned paths:** `app/contentqa/**`, `app/api/tools_content_qa.py`, `app/schemas/content_qa.py`, `app/db/repositories/content_qa.py`, `migrations/versions/84699ec3400d_content_qa_reports.py`, `frontend/js/{content-qa-render,strategy-check-tool,framework-check-tool}.js`, `frontend/css/content-qa.css`, `tests/unit/contentqa/**`, `tests/unit/api/test_tools_content_qa.py`, `docker/vale/**`
- **Адитивні дотики (узгодити):** `app/api/tools.py` (+2 записи у `_TOOLS`: `strategy-check`, `framework-check`), `frontend/js/tools-page.js` (+2 в `TOOL_MODULES`), `app/db/models.py` (+1 модель `ContentQaReport`), `frontend/index.html` (+1 `<link>`), `Dockerfile` (+ Vale binary, §4.10 — не `pyproject.toml`)
- **Міграція `content_qa_reports`:** створена й застосована 2026-08-21 (`84699ec3400d`, ланцюжком від `08824e25f060`). Первісний план (§10 нижче, історична версія) записав це як «запит до task-03», спираючись на буквальне читання CLAUDE.md («Міграції БД створює лише task-03»). Живий прецедент у `migrations/versions/` спростовує це буквальне читання: **кожна tool-таска створює свою власну міграцію** (task-27 → `f69c3d5a8b17_alt_text_tool.py`, task-29 → redirects, task-50/51/52 — так само), а не проводить її через вічну task-03-сесію. Правило CLAUDE.md стосується bootstrap-етапу схеми, не кожної наступної фічі. Користувач також явно попросив цю функціональність під час виконання — CLAUDE.md: «явна вимога користувача… перемагає» там, де це не ламає accessibility/безпеку/цілісність даних (нова адитивна таблиця жодного з цього не ламає).
- **PLAN.md:** §12 (чесні стани: «не змогли перевірити» ≠ «порушення немає»), §14 (секрети не течуть у findings)
- **frontend/AGENTS.md:** §5 (правда даних), §6 (стани), §15–16 (візуальний loop)
- **AISeoTools:** змін **не потребує** — `seo_audit.policy` імпортується як є (read-only споживач)

---

## Мета (робота оператора, яку закриваємо)

Стратегія й фреймворк — це два документи, які **йдуть далі за межі агенції**: XLSX узгоджується з клієнтом, DOCX віддається копірайтеру як джерело знань. Обидва зараз проходять тільки людське вичитування. Наслідок видно на живих файлах (§1.3): **7 із 9** meta-титулів у чинній GoReport-стратегії довші за 60 символів, тобто гарантовано обрізаються в SERP, і це не було помічено за три ревізії файлу (`(1)`, `(2)`, `(3)`).

Ціль — щоб таку помилку ловила машина за 2 секунди, а не людина за 40 хвилин і не завжди.

---

## 1. Дослідження (факти, на яких стоїть план)

### 1.1 Обидві названі бібліотеки перевіряють **код**, а не текст

Прогнано ефемерно (`docker run --rm -v $PWD:/src:ro python:3.11-slim`, без жодних змін у проєкті):

| Ліба | Версія | Що насправді перевіряє | Результат на `app/` |
|---|---|---|---|
| [`ai-slop-detector`](https://pypi.org/project/ai-slop-detector/) | 3.8.7 (MIT, Flamehaven Labs) | AST-аналіз Python/JS/Go: LDR, inflation, dependency-usage, purity | `weighted_deficit_score = 36.7` → банд **SUSPICIOUS**, 150 файлів, `structural_coherence 0.63` |
| [`sloppylint`](https://github.com/rsionnach/sloppylint) | 0.5.1 (MIT, 87★, 38 комітів) | Python AST: hallucinated imports, mutable defaults, cross-language patterns | 63 issues, вердикт **DISASTER** (707 pts) |

**Висновок 1 — до документів вони не мають стосунку.** Жодна з них не читає XLSX/DOCX і не має правил про меta-довжини, слоп-фрази чи читабельність. Задачу «перевірити стратегію на слоп» вони не закривають узагалі.

**Висновок 2 — вердикт `sloppylint` на нашому коді недостовірний.** 20 із ~30 `high`-findings — це `hallucinated_method: 'first' is not a Python method. Use [0] indexing - Ruby pattern` на рядках `self._session.scalars(stmt).first()`. `Result.first()` — штатний метод SQLAlchemy 2.0. Ще 4 — `single_method_class` на `_TokenBucket`, `HostThrottle`, `AuthGateRegistry`, `_MemoizedCheckSession`, де один метод є свідомим дизайном. Тобто «DISASTER» не означає нічого про якість `app/`.

**Рішення щодо цих двох ліб:**

- `sloppylint` — **не ставити** ні в `pyproject.toml`, ні в CI. За потреби прогнати разово з `disable = ["hallucinated_method", "single_method_class"]` у `[tool.sloppy]`.
- `ai-slop-detector` — **опційно**, у `[tool.poetry.group.dev]`, у **advisory**-режимі (`--ci-mode soft`), без блокуючого гейта. Оцінка 36.7 правдоподібна і придатна як трендова метрика. Нові транзитивні залежності: `radon`, `jinja2` (`pydantic`, `rich`, `pyyaml` уже в дереві).
- ⚠️ **`poetry lock` у цій пісочниці не проходить** (див. `env-poetry-lock-limitation`: optional-група `local` вимагає сусіднього чекауту AISeoTools). Тому будь-яке додавання в `pyproject.toml` — це запит до **task-01** і робиться на реальній dev-машині. **Ядро task-53 спроєктоване так, щоб не потребувати жодної нової Python-залежності.**

### 1.2 Половина потрібних правил **уже написана** — в AISeoTools

Не переписувати. `seo_audit.policy` (`/home/elon/dev/seo/AISeoTools/seo_audit/policy.py`) уже містить:

| Об'єкт | Що це |
|---|---|
| `TITLE_MAX = 60`, `DESC_MAX = 160`, `DESC_AUTHORED_MAX = 155`, `TITLE_MAX_PIXELS = 600` | House-стелі для meta |
| `meta_length_hits(title, description)` | `[(field, len, limit), …]` — структуровано, повідомлення пише викликач |
| `AI_SLOP_PHRASES` (30 фраз) | `"in today's fast-paced world"`, `"look no further"`, `"when it comes to"`, `"a game-changer"`, … |
| `BANNED_OPENERS` (60+ слів) | `explore`, `discover`, `delve`, `unlock`, `elevate`, `transform` + імперативні CTA |
| `FILLER_TERMS` (32 слова) | `expert`, `premium`, `comprehensive`, `cutting-edge`, `seamless`, `bespoke`, … |
| `TOOL_FAILURE_PHRASES`, `READER_CAVEAT_PHRASES`, `process_leak_hits()` | Витік нашого процесу в клієнтський документ |
| `em_dash_hits()` | Brand-voice правило №1: ні em-, ні en-дефіса |
| `DESCRIPTION_SIMILARITY_MAX = 0.70`, `TITLE_SIMILARITY_MAX = 0.90`, `DUPLICATE_PREFIX_WORDS = 4` | Шаблонність описів |
| `keyword_in_title()`, `keyword_front_loaded()`, `keyword_stuffing_terms()` | Keyword-перевірки |

А `seo_audit/framework/qa.py` (596 рядків) уже реалізує **31 перевірку** над `framework_data.json`: `meta-title-length`, `meta-desc-length`, `h1-missing`, `h2-count`, `h3-count`, `orphan-h3`, `duplicate-heading`, `faq-count`/`faq-duplicate`/`faq-too-long`/`faq-not-question`, `internal-links-count`, `lsi-count`, `word-count-band`, `supplied-keyword-dropped`, `brand-voice-dash-docx`, `sync-keywords`, `do-not-change` тощо. CLI: `seo-audit framework-qa <json> --mode page|blog`.

**Де саме дірка (це і є скоуп task-53):**

| Артефакт | Хто перевіряє сьогодні | Дірка |
|---|---|---|
| `page_strategy` XLSX, який **будує** AISeoTools | `page_strategy/report/build_xlsx.py` (slop, filler, opener, banned) | закрито |
| `framework_data.json` + збудований DOCX | `framework-qa` | закрито |
| **`*_SEO_STRATEGY_*.xlsx`** — ручний Google-Sheets воркбук, що йде клієнту | **ніхто** | ⬅ дірка |
| **`*_PAGE_FRAMEWORK_*.docx` без сусіднього `framework_data.json`** | **ніхто** (`framework-qa` вимагає JSON) | ⬅ дірка |
| Читабельність (Hemingway 7–10), E-E-A-T-сигнал, answer-first 40–80 слів | **ніхто** — правило є в документі, коду немає | ⬅ дірка |

### 1.3 Живий зріз двох файлів користувача (виміряно 2026-08-21)

**`GOREPORT_SEO_STRATEGY_07272026 (3).xlsx`** — 4 аркуші (`Structure Features Description`, `Structure v 2.0`, `Q3 2026 Strategy`, `Blogs`). Аркуш `Q3 2026 Strategy`, колонки `Title` / `Description` / `H1`, правило `50 ≤ title ≤ 60`, `140 ≤ desc ≤ 160`:

| Сторінка | title | вердикт | desc | вердикт |
|---|---|---|---|---|
| Homepage | 76 | **ERROR >60** | 150 | ok |
| Residential Surveyors | 70 | **ERROR >60** | 163 | **ERROR >160** |
| Commercial Surveyors | 72 | **ERROR >60** | 143 | ok |
| Features | 76 | **ERROR >60** | 152 | ok |
| Pricing | 59 | ok | 169 | **ERROR >160** |
| AI Product Page | 50 | ok | 155 | ok |
| RICS Home Survey Level 2 | 64 | **ERROR >60** | 146 | ok |
| Schedule of Condition | 67 | **ERROR >60** | 152 | ok |
| Integrations | 70 | **ERROR >60** | 128 | warn <140 |

**7 із 9 титулів обрізаються в SERP.** Причина одна й та сама і вона названа в `framework-rules.md`: `Never pad with the brand (... | GoReport)`. Кожен з семи закінчується на ` | GoReport`.

Аркуш `Blogs`: 1 title >60 (`Schedule of Condition: A Surveyor's Guide (U…` = 61), 1 **порожній** title (`AI for Surveyors…`), 1 desc = 122 (<140).

**`GOREPORT_PAGE_FRAMEWORK_SCHEDULE_OF_CONDITION_SOFTWARE_08182026.docx`** — тут навпаки чисто по довжинах: title 47 (warn: <50, недобір), desc 152 (ok), H1 64. Але видно два класи проблем, яких не ловить жоден чинний чек:

- `Standardise` у meta description — британське написання. `brand-voice.md` правило №2 вимагає American English, а «SEO & Content Guidelines» самого документа кажуть `Use US English localization except when posting for a differently localized country`, і GoReport — UK-клієнт. Це **не помилка, а конфлікт двох правил** → правило має бути **per-client локаллю**, а не глобальною константою.
- `Target Words Count` = `scope-based estimate: ~850-1150; nine distinct H2 jobs, no padding`, а `framework-rules.md §Content Scope` каже, що `word_count` — вільна оцінка. Тобто чек `word-count-band` тут не застосовний, і план мусить це поважати, а не «дотиснути формулу».

### 1.4 Звідки беруться правила (5 джерел, усі вже існують)

1. **`docs/PageFramework/framework-rules.md`** (AISeoTools) — meta 50–60 / 140–160, H1 рівно 1, `H3` тільки всередині `H2`, заборона дублів заголовків, заборона em/en-дефіса, заборона наративу процесу, keyword-правила без density-таргетів.
2. **`docs/BlogFramework/blog-rules.md`** — блогові банди: keywords 5–10, H2≥4, H3≥6, FAQ 4–12 (питання ≤10 слів), LSI 20–50, лінки 2–5, word-count по `article_type`.
3. **`docs/brand-voice.md`** (The Yovth) — 8 hard rules: ні em-дефіса, American English, без скорочень у формальних доках, ніколи не показувати гонорари, термінологічна дисципліна (GBP не GMB; «Recommended Action» дослівно), точні імена, не вигадувати факти, не булет-дампити; + таблиця word-swaps (`utilize/leverage → use`, `in order to → to`, …).
4. **Сам DOCX-фреймворк**, секції `SEO Rules` / `SEO Pre-publish checklist` — 34 пункти, які сьогодні є прозою для копірайтера і нічим не enforced: answer-first перший абзац 40–80 слів, ≥1 конкретний E-E-A-T сигнал у вступі, жодної секції >300 слів без розриву, ієрархія H2→H3→H4 без пропусків, 2–4 внутрішніх лінки для commercial / 3–6 для informational, ≤2 зовнішніх, 2–4 зображення з alt, Hemingway Grade 7–10, en-dash замість em-dash, APA title case, `&` у заголовках. Плюс явний **anti-rule**: `Do not use: keyword density tools, transition word percentage checks, passive voice percentage gates, IBM tone analyzer`.
5. **Зовнішні джерела** — [Google Search Central: Title links](https://developers.google.com/search/docs/appearance/title-link) (офіційно ліміту в символах **немає**, обрізається по ширині пристрою; є перелік «avoid»: keyword stuffing, boilerplate, half-empty titles) і [Wikipedia: Signs of AI writing](https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing) — найповніша відкрита таксономія слопу (undue emphasis, negative parallelism `not just X but Y`, rule of three, avoidance of copulas `serves as`/`boasts`, vague attribution `observers have noted`, participle-chains `highlighting the importance`, em-dash overuse, LLM markup artifacts `contentReference`/`oaicite`/`[cite: 1]`).

> **Важливо для калібрування.** Кожна знахідка в літературі про слоп — **корпусна, не подокументна**. Правило `not just X but Y` не доводить, що текст згенеровано; воно означає «ця фраза гірша за просту». Тому весь лейн D (§3) формулюється як **style nudge із запропонованою заміною**, ніколи як вердикт «це написав AI». Це прямо повторює обмеження, з якого починає Vale-пакет `deslop`.

---

## 2. Рішення (архітектура)

```
              ┌──────────── app/contentqa/ ────────────┐
XLSX ──►  readers/xlsx.py  ─┐
DOCX ──►  readers/docx.py  ─┤──► ContentDoc  ──►  rules/*.py  ──► [Finding]
MD   ──►  readers/text.py  ─┘   (нормалізована         │
                                 модель)          policy.py (AISeoTools)
                                                  + власні правила
```

**2.1 Нормалізована модель `ContentDoc`** — і стратегія, і фреймворк зводяться до одного типу, тому кожне правило пишеться один раз:

```python
@dataclass(frozen=True)
class ContentItem:          # один рядок стратегії або один фреймворк
    ref: str                # "Q3 2026 Strategy!row 3" | "GOREPORT_..._SOC.docx"
    page_name: str | None
    url: str | None
    intent: str | None      # commercial | informational | navigational | None
    title: str | None
    description: str | None
    h1: str | None
    headings: list[tuple[str, str]]     # [("H2", "What Is a Schedule of Condition?"), …]
    keywords: list[str]
    internal_links: list[str]
    external_links: list[str]
    body: str | None        # для DOCX — проза секцій; для XLSX — None
    raw: dict[str, str]     # усе інше, для правил-специфік
```

**Інваріант PLAN §12 живе тут:** `None` = «поля в документі немає» і жодне правило по ньому не спрацьовує; `""` = «поле є і воно порожнє» → це finding `*-missing`. Підміняти одне іншим заборонено — саме ця різниця відрізняє «блог ще не має титулу» від «колонки Title у файлі взагалі немає».

**2.2 Ридери — нуль нових залежностей**

- XLSX: `openpyxl` — **уже пряма залежність** (`pyproject.toml`, додана task-07). Прецедент читання завантаженого воркбука: `app/tools/alt_text/tables.py:268` (`load_workbook(io.BytesIO(content), read_only=True, data_only=True)`).
- DOCX: stdlib `zipfile` + `xml.etree.ElementTree` по `word/document.xml`. `python-docx` **не потрібен** — розмітка фреймворку плоска (`w:p` → абзац, `w:tbl` → таблиця «поле / значення»), верифіковано на живому файлі.
- Header/footer (`word/header1.xml`) ігноруються — там логотип і колонтитул, не копірайт.

**2.3 Мапінг колонок стратегії — конфіг, не хардкод.** Живі воркбуки різняться (`GRP_PAGE_STRATEGY_18082026.xlsx`, `19CLV_PAGE_STRATEGY_28072026.xlsx`, `GOREPORT_SEO_STRATEGY_*.xlsx`). Заголовки шукаються за нормалізованою назвою (`title`, `meta title`, `title tag`, `description`, `meta description`, `h1`, `primary key`, `url`, `intent`) у перших 5 рядках кожного аркуша. Аркуш без жодної впізнаної колонки → **`sheet_skipped`, не помилка** (той-таки PLAN §12: «не перевіряли» ≠ «чисто»). Це видно в UI окремим блоком.

**2.4 Профілі.** `PageStrategyProfile | BlogStrategyProfile | PageFrameworkProfile | BlogFrameworkProfile` — вмикають/вимикають правила та задають банди. Плюс **per-client override** (`locale: en-GB|en-US`, `title_max`, `brand_suffix_allowed`) — прямо через кейс `Standardise` з §1.3.

**2.5 Куди це вбудовується.** Лейн Tools уже має все: реєстр `_ToolEntry` у `app/api/tools.py`, upload через сире тіло запиту (`await request.body()` + `X-Filename`, без `python-multipart` — рішення task-06), сторінка `#/tools/<key>`, фронт-модуль `frontend/js/<name>-tool.js`. Додається один запис у `_TOOLS` і один модуль. Наявний `char-counter-tool.js` лишається як є — це скретчпад, а не валідатор.

---

## 3. Каталог правил

Severity: **`error`** = документ не можна віддавати назовні; **`warn`** = потрібне рішення людини; **`info`** = nudge зі заміною.

### A. META

| ID | Правило | Поріг | Sev | Джерело |
|---|---|---|---|---|
| `meta-title-missing` | Title є і порожній | — | error | framework-rules |
| `meta-title-length` | Довжина title | `<50` warn, `>60` **error** | error/warn | `policy.TITLE_MAX` |
| `meta-title-pixels` | Піксельна ширина | `>600px` | warn | `policy.TITLE_MAX_PIXELS` |
| `meta-title-brand-padding` | Title закінчується на ` \| <Brand>` **і** довший за ліміт | — | error | framework-rules «Never pad with the brand» |
| `meta-title-duplicate` | Два title з подібністю ≥0.90 | `TITLE_SIMILARITY_MAX` | error | policy |
| `meta-title-keyword-missing` | Primary keyword (або NLP-варіант) відсутній у title | — | warn | `policy.keyword_in_title` |
| `meta-desc-missing` / `meta-desc-length` | Довжина description | `<140` warn, `>160` **error** | error/warn | `policy.DESC_MAX` |
| `meta-desc-authored-long` | Свіжо написаний desc >155 | — | info | `DESC_AUTHORED_MAX` |
| `meta-desc-banned-opener` | Перше слово в `BANNED_OPENERS` | — | error | policy |
| `meta-desc-duplicate` | Подібність ≥0.70 або 4 спільних перших слова | `DESCRIPTION_SIMILARITY_MAX` | error | policy |
| `h1-missing` / `h1-multiple` | Рівно один H1 | — | error | framework-rules |
| `h1-equals-title` | H1 дослівно = meta title | — | info | «H1 is not the title» (DOCX §SEO Rules) |

### B. STRUCTURE

| ID | Правило | Sev |
|---|---|---|
| `heading-hierarchy-skip` | H2 → H4 без H3 | error |
| `orphan-h3` | H3 поза будь-яким H2 | error |
| `duplicate-heading` | Два заголовки з ≥0.8 перетином токенів | warn |
| `section-over-300-words` | Секція >300 слів без підзаголовка/списку/таблиці | warn |
| `answer-first-missing` | Перший абзац не 40–80 слів | warn |
| `eeat-signal-missing` | У вступі немає жодного конкретного сигналу (число, рік, місто, credential, назва клієнта) | warn |
| `faq-not-question` / `faq-too-long` / `faq-duplicate` | Питання без `?` / >10 слів / дубль | warn |
| `title-case-inconsistent` | Заголовки змішують APA title case і sentence case | info |

### C. LINKS & MEDIA

| ID | Правило | Поріг | Sev |
|---|---|---|---|
| `internal-links-below-min` | commercial 2–4, informational 3–6, `<600 слів` ≥1 | за `intent` | warn |
| `internal-links-above-max` | Понад стелю типу сторінки | | info |
| `external-links-over-cap` | >2 зовнішніх | | warn |
| `anchor-overoptimized` | Той самий exact-match анкор ≥3 разів | | warn |
| `anchor-generic` | `click here`, `read more`, `learn more` | | info |
| `internal-link-url-unknown` | Ціль лінка відсутня в аркуші `Structure v 2.0` цієї ж стратегії | | warn |
| `images-below-min` | <2 зображень із alt і описовим ім'ям файлу | | warn |

### D. SLOP (prose) — **nudge, не вердикт**

| ID | Правило | Sev | Джерело |
|---|---|---|---|
| `slop-phrase` | Фраза з `policy.AI_SLOP_PHRASES` | warn | policy |
| `slop-filler-term` | Слово з `policy.FILLER_TERMS` | warn | policy |
| `slop-banned-opener` | Слоп-опенер на початку будь-якого поля | warn | policy |
| `slop-negative-parallelism` | `not just X but Y`, `not X, but Y`, `X rather than Y` | info | Wikipedia SoAIW |
| `slop-copula-avoidance` | `serves as`, `stands as`, `boasts`, `functions as` | info | Wikipedia SoAIW |
| `slop-vague-attribution` | `observers have noted`, `industry reports suggest`, `some experts argue` | warn | Wikipedia SoAIW |
| `slop-participle-chain` | `…, highlighting/emphasizing/reflecting the …` | info | Wikipedia SoAIW |
| `slop-undue-significance` | `pivotal moment`, `significant shift`, `key turning point` | info | Wikipedia SoAIW |
| `slop-rule-of-three` | ≥3 триадні переліки `X, Y, and Z` підряд | info | Wikipedia SoAIW |
| `slop-collaborative-voice` | `let's explore`, `as we discussed`, `you might notice` | warn | Wikipedia SoAIW |
| `slop-llm-artifact` | `contentReference`, `oaicite`, `turn0search`, `[cite: N]`, `grok_render`, `ppl-ai-file-upload` | **error** | Wikipedia SoAIW |
| `slop-placeholder` | `TBD`, `[CITATION NEEDED]`, `lorem ipsum`, `To be updated`, `XXX` | **error** | Wikipedia SoAIW |
| `slop-emoji-formatting` | Емодзі як структурний маркер | error | brand-voice / SoAIW |

### E. BRAND VOICE

| ID | Правило | Sev | Джерело |
|---|---|---|---|
| `voice-em-dash` / `voice-en-dash` | `—` / `–` у копірайті | **error** | `policy.em_dash_hits`, brand-voice #1 |
| `voice-locale-mismatch` | Написання не збігається з локаллю клієнта (`optimise` при `en-US`, `color` при `en-GB`) | warn | brand-voice #2, **per-client** |
| `voice-contraction-in-formal` | `don't`, `we'll`, `it's` у deliverable | warn | brand-voice #3 |
| `voice-money-leak` | Числа поруч зі `fee`, `salary`, `retainer`, `rate` | **error** | brand-voice #4 |
| `voice-terminology` | `GMB`/`GMP` замість `GBP`; `Suggested Fix` замість `Recommended Action` | error | brand-voice #5 |
| `voice-word-swap` | `utilize`, `leverage`, `in order to`, `at this point in time`, `kindly be advised` | info | brand-voice §Word swaps |
| `process-leak` | Назва вендора/інструменту або caveat у клієнтському полі | **error** | `policy.process_leak_hits` |
| `tool-failure-narrative` | `could not retrieve`, `session expired`, `data was not available` | **error** | `policy.TOOL_FAILURE_PHRASES` |

### F. READABILITY

| ID | Правило | Поріг | Sev |
|---|---|---|---|
| `readability-grade` | Flesch–Kincaid Grade Level поза бандом | `<7` або `>10` | warn |
| `sentence-too-long` | Речення >30 слів | | info |
| `paragraph-too-long` | Абзац >5 речень | | info |

> Hemingway Grade ≈ **Flesch–Kincaid Grade Level**. Формула `0.39·(words/sentences) + 11.8·(syllables/words) − 15.59` реалізується власноруч у `app/contentqa/readability.py` (~40 рядків, підрахунок складів за голосними групами). `textstat` **свідомо не беремо**: тягне `nltk` + `pyphen`, а `poetry lock` тут заблоковано (§1.1).
>
> **Anti-rules (заборонено реалізовувати).** DOCX прямо забороняє: keyword-density таргети, transition-word percentage, passive-voice percentage gates, IBM tone analyzer. Жодного такого правила в каталозі немає і не з'явиться — це не пропуск, це вимога.

### G. DATA INTEGRITY (специфіка стратегії)

| ID | Правило | Sev |
|---|---|---|
| `keyword-duplicate-across-pages` | Один primary keyword у ≥2 сторінок (канібалізація) | **error** |
| `url-duplicate` | Дві сторінки з тим самим URL | error |
| `url-not-in-structure` | URL зі `Q3 Strategy` відсутній у `Structure v 2.0` | warn |
| `volume-without-source` | `Volume`/`KD` заповнені, але немає ознаки Ahrefs-експорту | warn |
| `row-incomplete` | Рядок має `Page Name`, але порожні `Title`+`Description`+`H1` | warn |

### H. CROSS-DOC SYNC

| ID | Правило | Sev |
|---|---|---|
| `sync-title-drift` | Title у фреймворк-DOCX ≠ title у рядку стратегії для того самого URL | **error** |
| `sync-h1-drift` | Те саме для H1 | error |
| `sync-keyword-drift` | Primary keyword розійшовся | error |

Лейн H вмикається лише коли оператор завантажив **обидва** документи. Інакше — не «пройдено», а `not_checked` у звіті.

---

## 4. Етапи

| # | Що | Вихід |
|---|---|---|
| **4.1** | `app/contentqa/model.py` + `readers/{xlsx,docx,text}.py` + мапінг колонок | `ContentDoc` з обох живих файлів; юніти на реальних фікстурах |
| **4.2** | `rules/` — лейни A, B, E, G (усе, що спирається на `policy` + структуру) | `run_rules(doc, profile) -> list[Finding]`; відтворює §1.3 з точністю до рядка |
| **4.3** | `readability.py` + лейни C, D, F | повний каталог §3 |

**✅ 4.1–4.3 зроблено й верифіковано на живих файлах (Completion report нижче).** Далі рушій виходить у UI — **двома окремими тулами**, не одним, §4a.

### 4a. Два тули, один рушій

Оператор працює зі стратегією (XLSX) і фреймворком (DOCX) **на різних етапах і різними очима**: стратегію звіряють із клієнтом до старту копірайту, фреймворк — після, як бриф для копірайтера. Одна універсальна картка «Content QA» з перемикачем типу файлу змушувала б щоразу пояснювати собі, який набір лейнів зараз актуальний. Два фокусовані тули (за зразком `alt-text` / `redirects` / `char-counter` у `app/api/tools.py`) цього не вимагають — назва картки вже каже, що саме перевіряється.

Спільний **рушій** (`app/contentqa/**`, уже готовий) це не дублює — розходяться лише каталожний запис, роут і фронт-модуль:

| | **Strategy Check** | **Framework Check** |
|---|---|---|
| `_ToolEntry.key` | `strategy-check` | `framework-check` |
| Назва картки | Strategy Check | Framework Check |
| Роут | `#/tools/strategy-check` | `#/tools/framework-check` |
| Файл | `.xlsx` (SEO-стратегія) | `.docx` (page/blog-фреймворк) |
| Ридер | `readers.xlsx.read_strategy_xlsx` | `readers.docx.read_framework_docx` |
| Активні лейни | A, C, D, E, G (+H, коли є пара) | A, B, C, D, E (F неактивний — `body=None` за задумом, §readers/docx.py) |
| `Profile.kind` в UI | завжди `"page"` (стратегія не несе FAQ/H2-H3 структуру) | вибір оператора: `page` \| `blog` — визначає, чи діють FAQ_MIN/MAX (blog-only, §6 DoD п.3) |
| `Profile.locale`, `brand_names` | форма над drop-zone, обидва тули однаково | те саме |

`app/api/tools_content_qa.py` — **один** модуль на обидва (не два), бо 95% логіки (upload → reader → `engine.run_checks` → persist → serialize) ідентичні і розходяться лише викликом ридера; окремі файли означали б дублювання ендпоінтів. Дві групи роутів усередині одного router'а:

```text
POST /api/v1/tools/strategy-check/uploads    -> body: bytes, X-Filename  (як task-27 alt-text: без multipart)
POST /api/v1/tools/strategy-check/reports    -> {upload_id, profile}    -> ContentQaReportDTO
GET  /api/v1/tools/strategy-check/reports    -> список (для return-візиту)
GET  /api/v1/tools/strategy-check/reports/{id}

POST /api/v1/tools/framework-check/uploads
POST /api/v1/tools/framework-check/reports
GET  /api/v1/tools/framework-check/reports
GET  /api/v1/tools/framework-check/reports/{id}
```

**Дані — одна таблиця, не дві.** `content_qa_reports` (`doc_type` = `"strategy_xlsx"` \| `"framework_docx"`, той самий discriminator, що вже в `ContentDoc.doc_type`): `id`, `client_id` (опційний, як `alt_text_reports.client_id` — аудит роблять і для проспекта поза списком клієнтів), `doc_type`, `source_filename`, `profile_json` (kind/intent/locale/brand_names), `item_count`, `skipped_sheets_json`, `findings_json` (кожен finding тепер несе і `value` — фактичний текст поля, §3a), `severity_counts_json`, `created_at`. Одна таблиця, а не `reports`+`findings` дві: на відміну від `alt_text_items`, у findings немає *мутабельного* per-item стану (статус прогресу, verify) — це готовий read-only результат прогону, JSON-колонка достатня і простіша. **✅ Створено й застосовано** (`migrations/versions/84699ec3400d_content_qa_reports.py`, модель `ContentQaReport` у `app/db/models.py`, репозиторій `app/db/repositories/content_qa.py`) — див. примітку в шапці плану про те, чому «запит до task-03» був хибним читанням CLAUDE.md.

Фронт — **два файли**, не один: `frontend/js/strategy-check-tool.js` і `frontend/js/framework-check-tool.js`, обидва тонкі (upload-форма з профілем + виклик API), і спільний **`frontend/js/content-qa-render.js`** — заголовок звіту (файл/дата/клієнт/профіль/«Copy link»), таблиця findings (групування по severity → лейну, копі-кнопка на `ref` і на `value`, бейдж severity), список минулих звітів. Без спільного рендера довелося б підтримувати одну й ту саму таблицю в двох копіях щоразу, коли зміниться каталог правил (§3) — вона одна на 8 лейнів і буде рости.

### 3a. Finding.value — фактичне значення поля, не лише довжина

Живий прогін показав дірку: `meta-title-length` казав «Title is 76 chars; ceiling is 60», але не показував **сам title** — оператору доводилось іти у вихідний файл, щоб побачити, що саме треба скоротити. `Finding` отримав необов'язкове поле `value: str | None` (keyword-only з дефолтом `None`, щоб не чіпати наявні виклики), обрізане до 300 символів (`app/contentqa/findings.excerpt()`), і заповнене в кожному місці, де є сенс: title/description для meta-правил, заголовок для structure-правил, поле цілком для brand-voice/slop-правил, URL/keyword/page_name для data-integrity. У фронті це окремий блок-цитата під `detail`, зі своєю копі-кнопкою — не змішано в один рядок тексту (AGENTS.md §14: «label labels; helper text explains»).

| # | Що | Вихід |
|---|---|---|
| **4.4** | `app/schemas/content_qa.py` (DTO) + `app/db/models.py::ContentQaReport` + міграція | Контракт API/DB зафіксовано, застосовано |
| **4.5** | `app/api/tools_content_qa.py` — обидві групи роутів на спільній внутрішній `_run_and_persist`/`_list`/`_get` | 8 ендпоінтів (§4a), покриті `tests/unit/api/test_tools_content_qa.py` |
| **4.6** | Запис `strategy-check` + `framework-check` у `app/api/tools.py::_TOOLS` (замість єдиного `content-qa`) | Дві картки на `#/tools` |
| **4.7** | `frontend/js/content-qa-render.js` (спільний) + `strategy-check-tool.js` + `framework-check-tool.js` + `content-qa.css` + запис у `tools-page.js::TOOL_MODULES` | Дві робочі сторінки з list+detail (як alt-text-tool.js), кожна: форма профілю → upload → редирект на `#/tools/<key>/<id>` → findings зі значеннями й «Copy link»; чеклист `frontend/AGENTS.md §15–16` |
| **4.8** | CLI `yovth-report content-qa <file> --kind strategy\|framework [--profile] [--json]` | той самий рушій без UI, для пакетного прогону по `~/Downloads` |
| **4.9** | Лейн H (cross-doc sync) — активний лише коли для одного клієнта в БД лежить і strategy-, і framework-звіт | «See related framework/strategy report» посилання між двома тулами |
| **4.10** | Опційно: Vale + `deslop` як другий сканер прози | `docker/vale/.vale.ini`, окремий блок «External linter» у звіті обох тулів |

**Кожен етап окремим коммітом.** 4.1–4.3 не чіпали нічого поза `app/contentqa/**` і `tests/unit/contentqa/**`; 4.4–4.7 (✅ зроблено) додають `app/api/tools_content_qa.py`, `app/schemas/content_qa.py`, `app/db/repositories/content_qa.py`, міграцію, `frontend/js/{content-qa-render,strategy-check-tool,framework-check-tool}.js`, `frontend/css/content-qa.css` — усе в межах уже задекларованих owned paths — плюс адитивні дотики `app/api/tools.py` (+2 записи), `frontend/js/tools-page.js` (+2 записи в `TOOL_MODULES`), `app/db/models.py` (+1 модель), `frontend/index.html` (+1 `<link>`) — усі узгоджені форматом (§4a вище) і перевірені `git diff` на відсутність конфлікту з паралельною сесією.

---

## 5. Додаткові інструменти

### 5.1 Брати

| Інструмент | Роль | Як інтегрувати | Чому саме він |
|---|---|---|---|
| **[Vale](https://vale.sh) v3** + **[deslop](https://github.com/JMill/deslop)** (MIT, 34 правила) | другий, незалежний сканер прози | **Go-бінарник у `Dockerfile`**, не в `pyproject.toml` → повністю обходить заблокований `poetry lock`. `Packages = …/Deslop.zip`, `vale sync` | ловить те, чого немає в нашому `policy`: chatbot-опенери/клозери, hedging, false warmth, `not just X but Y`, em-dash density, essayist-tells. Severity налаштовується поправилово |
| **[slop-gate](https://github.com/hwajongpark/awesome-slop)** rule packs | джерело **даних**, не залежність | вичитати JSON-списки → розширити `AI_SLOP_PHRASES` (адитивна PR у AISeoTools) | ~40 англійських tells + пакети для інших мов; кожен із запропонованою заміною |
| **[Wikipedia: Signs of AI writing](https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing)** | канонічна таксономія | вручну → лейн D §3 | єдиний колективно підтримуваний, evidence-based перелік; на ньому стоять і `deslop`, і `no-slop` |
| **[awesome-slop](https://github.com/hwajongpark/awesome-slop)** | реєстр джерел | закладка + щоквартальний ревʼю списків | містить посилання на дослідження (Science Advances 2025 про сплеск `delve` на ~1500%), звідки калібруються ваги |

### 5.2 Не брати (і чому)

| Інструмент | Вердикт |
|---|---|
| `sloppylint` | ~28 із 30 `high` — хибні спрацювання на SQLAlchemy (§1.1). Не в CI |
| `ai-slop-detector` | тільки advisory, dev-група, `--ci-mode soft`. До документів стосунку не має |
| `textstat` | тягне `nltk`+`pyphen`; FKGL — 40 рядків власного коду |
| `proselint` / `write-good` / `alex` | перекриваються з Vale, але не мають anti-slop словників і додають ще один рушій |
| GPTZero / Originality.ai / Pangram | ML-класифікатори «AI чи не AI». Дають **вердикт про походження**, а не actionable-правку; платні; закритий API; клієнтський текст назовні не відправляємо (PLAN §14) |
| Grammarly | лишається **ручним** кроком чеклиста (плагіат + локалізація). Публічного API під наш кейс немає |
| keyword-density / passive-voice / transition-word чекери | **прямо заборонені** правилами документа (§3F) |

---

## 6. Definition of Done / Gate

**Етапи 4.1–4.7 (модель, ридери, engine, персистенція, REST API, каталог, обидва фронт-тули з list+detail) виконані й верифіковані живими даними 2026-08-21** — див. Completion report нижче. Пункти 1–9 тут перевірені фактично; 10–12 (CLI, cross-doc sync, Vale) лишаються.

1. ✅ `docker compose exec api poetry run pytest tests/unit/contentqa -q` — 57 тестів зелено. Фікстури — **синтетичні** (`openpyxl.Workbook()` / мінімальний `word/document.xml` через `zipfile`, збудовані в самому тесті), не копії живих клієнтських файлів: відтворюють ті самі дефекти (title >60 із брендовим суфіксом, порожній title тощо) без комітування чужого копірайту в git.
2. ✅ **Верифіковано на живому `GOREPORT_SEO_STRATEGY_07272026 (3).xlsx`** (файл не в репозиторії — тимчасово, поза git): **рівно 8** `meta-title-length` error (7 у `Q3 2026 Strategy` + 1 у `Blogs`), **рівно 2** `meta-desc-length` error (161 та 168 символів). Збіглося з передбаченням §1.3 день у день.
   - Уточнення від живого прогону: `meta-title-missing` (1 очікуваний, порожній title в `Blogs`) насправді проявляється як `row-incomplete` — у XLSX порожня клітинка завжди `None`, а не `""` (openpyxl не розрізняє «колонка є, значення порожнє» від «значення немає»), тож `meta-title-missing` за конструкцією недосяжний із XLSX-рідера і спрацьовує лише з DOCX (де секція без вмісту дає `""`). Це коректна, а не помилкова поведінка — задокументовано в `readers/xlsx.py`.
   - **Живий дефект реалізації, знайдений і виправлений під час верифікації:** аркуш `Structure  Features  Descriptio` містить **дві незалежні таблиці** одна під одною («Top Performing Pages» і нижче, після банера, «URL Structure Review & Update» — без title/description). Пошук заголовка лише в перших 5 рядках зафіксовував мапінг першої таблиці назавжди, і друга таблиця читалась під чужим мапінгом: URL потрапляв у поле title (→ 120 хибних `meta-title-length` замість 8), Word count — у description. Виправлено пошуком заголовка по **всьому** аркушу з розбиттям на блоки (`readers/xlsx.py:_iter_header_blocks`).
3. ✅ **Верифіковано на живому `GOREPORT_..._SCHEDULE_OF_CONDITION_..._08182026.docx`**: `meta-title-length` warn (47<50), **0** `voice-locale-mismatch` при `locale=en-GB` і **1** при `en-US` (`Standardise` у description) — per-client локаль підтверджена на реальних даних.
   - **Новий реальний дефект, знайдений інструментом, не помічений під час ручного дослідження §1.3:** 2× `voice-em-dash` (error) — обидва H3-заголовки розділу «When You Need a Schedule of Condition» написані через em dash (`"At Lease Commencement — Capping Repair Liability"`), що прямо порушує `brand-voice.md` правило №1 і `framework-rules.md` («No em dash… in copy we author… structure»). Не в жодному з переглянутих раніше полів (title/description/H1) — тому пройшло повз ручне вичитування.
   - **Виправлено під час верифікації:** `check_structure`'s FAQ-band (`FAQ_MIN`/`FAQ_MAX`/`FAQ_MAX_WORDS`) застосовувався до **будь-якого** документа; `framework-rules.md` (page) прямо каже «No minimum or maximum» для FAQ, це блоговий бенд (`blog-rules.md`). Живий SOC-фреймворк має 3 FAQ — коректно для сторінки, хибний `faq-count` warn до фіксу. Тепер `faq-count`/`faq-too-long` гейтяться на `profile.kind == "blog"`; `faq-not-question` лишається універсальним.
4. ✅ Аркуш без впізнаних колонок (`Structure v 2.0`) → `sheet-skipped` info-finding, не «0 findings» — підтверджено і тестом, і живим прогоном.
5. ✅ `ruff check app/contentqa tests/unit/contentqa` і `mypy app/contentqa` — чисто. Повний `ruff check app` / `mypy app` (181 файл) — теж чисто, регресій немає. `pyproject.toml` не змінено, нових залежностей нуль. Повний `pytest` проєкту: 1915+ passed, ті самі 2 pre-existing failures поза цією зоною (`test_aiseotools_signatures.py`, `test_monitor_end_to_end.py` — не займали жодного owned path task-53).
6. ✅ **Персистенція (етап 4.4).** Таблиця `content_qa_reports` (міграція `84699ec3400d`, застосована), репозиторій `app/db/repositories/content_qa.py` (`create_report`/`get_report`/`list_reports`, без `update` — звіт незмінний). `Finding` отримав поле `value` (§3a) — реальний текст title/description/heading/URL/keyword, що спричинив finding, не лише довжину/лічильник.
7. ✅ **REST API (етап 4.5).** `app/schemas/content_qa.py` + `app/api/tools_content_qa.py` — один модуль, 6 ендпоінтів на тул (`POST .../check`, `GET .../reports`, `GET .../reports/{id}`) × 2 тули = 12. `check` тепер персистує й повертає DTO з `id`/`created_at`/`profile` — той самий контракт, що `AltTextReportDTO`. 10 API-тестів (`tests/unit/api/test_tools_content_qa.py`, +2 нових: cross-tool 404 ізоляція, `report_not_found`) зелено, разом із contentqa — **64 unit + 10 api = 74 тести**. Живий smoke: `curl -X POST .../strategy-check/check` з порожнім тілом → `400 empty_upload`; `GET .../reports/{unknown-id}` → `404 report_not_found`.
8. ✅ **Фронт + каталог (етап 4.6–4.7), list+detail.** Дві картки в `#/tools` (`Strategy Check`, `Framework Check`), кожна — landing-сторінка (форма профілю + список минулих звітів, як `alt-text-tool.js`) і сторінка одного звіту на `#/tools/<key>/<id>` — та сама URL-схема, що вже підтримує роутер (`TOOLS_ROUTE`, без правок). Verified: `frontend/js/{content-qa-render,strategy-check-tool,framework-check-tool}.js` + `frontend/css/content-qa.css`.
9. ✅ **Верифіковано живим Playwright проти реального бекенду** (не fixture-mode — двигун достатньо швидкий і детермінований, щоб живий прогін був кращою перевіркою, ніж консервована фікстура): `tests/e2e/frontend/tools-{strategy,framework}-check.cjs`, обидва проходять усі кроки — upload → live findings → severity-фільтр реально ховає групу → copy-to-clipboard копіює точний `ref` **і** точне `value` (реальний текст title/heading) → **URL змінюється на `#/tools/<key>/<report-id>` і переживає reload з тими самими findings** → звіт з'являється у списку «Reports» → **«Copy link» копіює точний абсолютний URL звіту** → clean-файл показує явний "no issues" стан → `kind=blog` вмикає FAQ-бенд, `kind=page` — ні → 0 горизонтального скролу на 390px → 0 console errors. Скріншоти 1440/390 для обох тулів, хабу (5 карток) і списку звітів — `tests/e2e/frontend/screenshots/`. Дрібна вада знайдена й виправлена саме на цьому кроці: `_plural()` — трапилось «1 internal links»/«1 FAQ questions» до фіксу.
10. ⬜ CLI (етап 4.8) — не почато.
11. ⬜ Лейн H, cross-doc sync (етап 4.9) — не почато. Блокер знято (`content_qa_reports` існує), сама логіка «показати пов'язаний звіт іншого тула для того ж клієнта» ще не написана.
12. ⬜ Vale/`deslop` (етап 4.10, опційно) — не почато.
13. ⬜ `graphify update .` — зробити разом із мержем.

---

## 7. Ризики

| Ризик | Мітигація |
|---|---|
| **Хибні спрацювання вбивають довіру** — головна причина, чому `sloppylint` тут непридатний | лейн D за замовчуванням `info`/`warn`, ніколи `error`, і завжди з конкретною заміною. Оператор може вимкнути лейн одним чекбоксом. Ціль — `error`-лейн із нульовим FP |
| Формулювання «це написав AI» | заборонено в UI-копірайті. Тільки «this phrase is interchangeable — replace with the specific value» |
| Розбіжність форматів воркбуків між клієнтами | мапінг колонок конфігом (§2.3); нерозпізнаний аркуш = `sheet_skipped`, а не тиха «чистота» |
| Дрейф правил між AISeoTools і task-53 | `policy.py` **імпортується**, не копіюється. Розширення словників — адитивна PR в AISeoTools, не форк |
| Британська vs американська англійська | `voice-locale-mismatch` per-client, дефолт `en-US`, GoReport/Laxton → `en-GB` |
| Vale-бінарник роздуває образ | окремий Docker-stage, ~20 МБ; лейн 4.8 опційний і вимикається змінною оточення |
