# task-95 — Вихідні посилання: ланцюг Framework → Copy → Site замість «усі `<a>` сторінки»

- **Пакет:** Content QA (третій тул, `#/tools/strategy-package`, вкладка Comparison + Excel `Comparison`/`Mismatches`)
- **Depends on:** task-90 ✅ (рушій пакета, `readers/gdoc.py`, `site.py`), task-91 ✅ (матриця + Excel), task-92 ✅ (`copy_tab_blocks`, `CopyTabResult.role`, маркери `[...]`)
- **Owned paths:**
  `app/contentqa/links.py` (новий — ланцюг і вердикти), `app/contentqa/{matrix,matrix_xlsx,site,package}.py`,
  `app/contentqa/readers/gdoc.py` (лише `+copy_tab_links`), `app/schemas/strategy_package.py`,
  `app/api/tools_strategy_package.py`, `frontend/js/strategy-package-tool.js`, `frontend/css/strategy-package.css`,
  `tests/unit/contentqa/test_links.py` (новий), `tests/unit/contentqa/test_{site,matrix,matrix_xlsx,readers_gdoc}.py`,
  `tests/e2e/frontend/tools-strategy-package.cjs`
- **Адитивні дотики:** `frontend/js/tools-page.js` (bump `?v=` імпорту), `tests/unit/api/data/openapi_snapshot.json`
- **Міграцій — нуль.** Нова форма живе всередині вже наявного `matrix_json` (JSON-колонка, task-91); DTO пакета — plain `BaseModel` без `extra="forbid"`, тож старі персистовані рядки з полем `links` у клітинках парсяться й далі (зайвий ключ ігнорується), а новий блок має дефолт → пакети до цієї таски чесно показують «no link audit in this run», не крашать.
- **PLAN.md:** §9.1 (ізоляція: збій одного фреймворка не валить пакет), §12 (`None` ≠ `0`: «не перевірено» ≠ «посилань нема»), §14
- **frontend/AGENTS.md:** §5 (правда даних), §6 (обов'язкові стани), §12 (CSP: тон лише класами), §14 (копірайт англійською), §15–16 (візуальний loop)
- **AISeoTools:** не чіпається. **Нових залежностей — нуль** (усе stdlib + наявні `html.parser`, `openpyxl`).

---

## Мета (робота оператора, яку закриваємо)

Оператор дивиться на колонку «Outgoing links» у Comparison і має відповісти на два питання:

1. **Чи копірайтер поставив у копію ті посилання, які приписав фреймворк?**
2. **Чи ці посилання з копії реально є на живій сторінці?**

Чинна реалізація (task-91 §2.3 `_link_verdict`) відповідає на інше питання — «чи є заявлене посилання *будь-де* на сторінці» — і робить це проти **всіх `<a href>` сторінки**, включно з меню, футером, хлібними крихтами, сортуванням WooCommerce, кнопками «додати в кошик» і лайтбоксами галереї. Наслідок — не шум, а **хибне «зелено»** (§1.1): посилання з головного меню є на кожній сторінці сайту, тож `all claimed links found on the live page` світиться навіть там, де копія не містить жодного з них. Вкладка Copy при цьому в звірку посилань **не входить взагалі**: `readers/gdoc.py` читає лише текст `textRun.content` і викидає `textStyle.link.url` (§1.4).

У Excel це виглядає як шість підколонок `Link 1…Link 6` на кожне з трьох джерел, забитих `Gallery → /gallery/; About Us → /about-us/; Locations → …` — і вердикт ✓ зверху. Користувач: «сайт = слоп, лінки йдуть з меню, з футера; треба звіряти фрейм → копі → сайт; не шість колонок, а сходиться/не сходиться».

**Рішення користувача (2026-09-14, уточнено):** дві колонки-вердикти — **«in copy»** (Framework → Copy) і **«on site»** (Copy → Site) — замість `Link 1…6`; зовнішні посилання копії (PDF, Google Maps, сервіси замовлення) **входять** у звірку, виключаються лише рядки-інструкції в дужках `[MAP: …]`, `[Order →]`.

---

## 1. Дослідження (заміряно живо 2026-09-14 в `api`-контейнері, не з документації)

Скрипти замірів лежать у `.verify_tmp/` (gitignored, bind-mounted у контейнер): `probe_copy_links.py` (§1.4), `probe_ancestors.py`/`probe_anc2.py` (§1.3), `probe_rule.py`/`probe_rule2.py` (§1.2, §1.7 пастки 2–3), `probe_corpus.py` (§1.5), `probe_nolinks.py` (§1.5), `probe_forms.py` (§1.6), `probe_chain.py` (§1.7), `probe_cta.py` (§1.7 пастка 2). Запуск: `docker compose exec -T api poetry run python /app/.verify_tmp/<probe>.py`. Для DoD 3–6 їх варто перезапустити проти реалізації, а не писати заново.

### 1.1 Чинна перевірка дає хибне «зелено» — доказ на EFFYS

Пакет `a5fc79ba` (`EFFYS_SEO_STRATEGY_061126`, 4 фреймворки). Сторінка `/locations/columbus-ave/`:

| Джерело | Що там |
|---|---|
| Framework `internal_links` | `full menu (/menu/)`, `Effy's on 96th Street (/locations/upper-west-side/)`, `our café story (/about-us/)` |
| Site, чинний парсер (усі `<a>`) | 22 посилання: `https://effyscafe.com`, `Gallery → /gallery/`, `About Us → /about-us/`, `Locations → /locations/`, `UWS → /locations/upper-west-side/`, `Menu → /menu/` … |
| **Чинний вердикт** | `match — all claimed links found on the live page` ✅ |
| Site, **лише контент** (правило §2.2) | `Home → /`, `Locations → /locations/` (хлібні крихти), 2× Google Maps |
| **Правда** | **0 з 3** приписаних посилань є в контенті сторінки. Усі три знайшлись у головному меню. |

Те саме на `/locations/upper-east-side/` (0/3 у контенті, вердикт `match`) і `/locations/` (0/2, `match`). Лише `/locations/upper-west-side/` має `/menu/` у прозі (`full Mediterranean menu`), і навіть там `/about-us/` і `/locations/upper-east-side/` — тільки в меню. **3 з 4 сторінок EFFYS сертифіковані як «усе на місці», хоча жодне приписане посилання в копію не потрапило.** Перевірка не просто шумить — вона підтверджує саме те, що мала б ловити.

### 1.2 Слоп заміряний: 94–97% посилань сторінки — навігація

| Сторінка | Усі `<a>` (чинне) | Контентних (правило §2.2) | Прибрано |
|---|---|---|---|
| `laxton.se/shop/utrustning/` | 68 | 4 | 94% |
| `laxton.se/shop/utrustning/ouija-board-by-laxton/` | 32 | 3 | 91% |
| `laxton.se/shop/utrustning/rorelsesensor/` | 33 | 1 | 97% |
| `laxton.se/hemsokt-museum/` | 56 | 3 | 95% |
| `effyscafe.com/locations/columbus-ave/` | 22 | 4 | 82% |
| `effyscafe.com/locations/` | 26 | 10 | 62% |

Категорії шуму, знайдені в живих даних: головне меню й футер (`<nav>` всередині `<header>`/`<footer>`), хлібні крихти (`nav.woocommerce-breadcrumb` на LAXTON, але `p#breadcrumbs` на EFFYS — **без `<nav>`**), сортування WooCommerce (`Sortera efter pris → /shop/utrustning/?orderby=price` ×5), «додати в кошик» (`?add-to-cart=NNN` ×7 на сторінку), лайтбокси галереї (34 `.jpg`-посилання на `/hemsokt-museum/`), кошик, логотип.

### 1.3 Що відрізняє контентне посилання від меню — на живих темах (Elementor + WooCommerce)

Ланцюг предків для кожного `<a>` (замір `probe_ancestors`):

| Посилання | Предки | Висновок |
|---|---|---|
| `Podd → /podcast/` (меню) | `header.elementor-location-header > … > nav.elementor-nav-menu--main > ul > li.menu-item > a` | `<header>`/`<nav>` — надійний сигнал |
| `PODD → /podcast/` (футер) | `footer.elementor-location-footer > … > nav > ul > li > a` | `<footer>` — надійний |
| `Utrustning → /shop/utrustning/` (крихти LAXTON) | `… > nav.woocommerce-breadcrumb > a` | `<nav>` — надійний |
| `Home → /` (крихти EFFYS) | `… > p#breadcrumbs > span > span > a` | **`<p>` без `<nav>`** — ловиться лише за словом `breadcrumb` в `id`/`class` |
| `Paranormal REM Static → /shop/utrustning/prs/` (**копія**) | `… > div.elementor-widget > p > a` | всередині `<p>`, поза `nav/header/footer` |
| `Order Now → getsauce.com/…` (**CTA з копії**) | `… > div.elementor-element > a.elementor-button` | **не в `<p>`**, кнопка — треба окремий дозвіл для `class~=button/btn` |
| `Kosher Certified → …CERT-July-2027.pdf` (копія) | `… > p > a` | у прозі |

**Пастка, знайдена на власній шкурі:** евристика за словами в `class` («menu», «widget», «sidebar») **непридатна на Elementor** — `elementor-widget` стоїть на КОЖНОМУ елементі, включно з прозою; перша версія правила поклала 100% контентних посилань у «chrome». Надійні лише семантичні теги `<nav>/<header>/<footer>/<aside>`, `role=navigation|banner|contentinfo|search` і одне вузьке слово `breadcrumb`.

Це також перевертає рішення task-91 §3.3 («посилання в nav/footer лишаються, бо вони клікабельні»): те рішення відповідало на питання «чи є посилання на сторінці», а користувач ставить питання «чи є посилання **в копії** на сторінці». Для другого питання посилання в меню — не доказ, а шум. Заголовки (`_SKIP_TEXT_TAGS`) уже живуть за цією логікою — тепер посилання наздоганяють їх.

### 1.4 Копія НЕСЕ справжні гіперпосилання — але їх ніхто не читає

`documents.get?includeTabsContent=true` віддає для кожного `textRun` поле `textStyle.link.url`. `readers/gdoc.py:_paragraph_text` бере лише `content` — посилання губляться на вході. Живо:

```
LAXTON_OUIJA  › 'English Content'  paras=25 runs=82  hyperlinks=6 → 3 унікальні цілі:
   'Paranormal REM Static' -> /shop/utrustning/prs/
   'Paranormal Music Box'  -> /shop/utrustning/pmb/
   'all ghost-hunting equipment' -> /shop/utrustning/
EFFYS_UWS     › 'Content'          paras=53 runs=202 hyperlinks=7 → 5 цілей (about-us, menu, getsauce, 2 PDF)
EFFYS_LOCATIONS_HUB › 'Content'    paras=74 runs=253 hyperlinks=18 (з них 9 — у рядках `[MAP: …]`/`[More Info →]`)
```

Три спостереження, які визначають ридер (§2.3):

- **Анкор розбитий на кілька `textRun`** з однією й тією ж `url`: `'' -> /prs/` + `'Paranormal REM Static' -> /prs/`, або `'Since 2002' -> /about-us/` + `',' -> /about-us/`. Дедуплікація — за ціллю, анкор — конкатенація сусідніх ранів з тією ж `url`.
- **Framework-вкладка теж має гіперпосилання — і всі вони сміття:** `Hemingway Score → hemingwayapp.com`, `Title Case Converter → titlecaseconverter.com`, `Grammarly → grammarly.com/plagiarism-checker` — бойлерплейт брифу, є в **усіх 60** заміряних документах. Копі-посилання **ніколи** не збираються з Framework-вкладки; Framework-цілі — лише з підписаних полів `Internal link + anchor text` / `External link`, як і зараз.
- **Рядки-інструкції в дужках** (`[MAP: Upper West Side location] → google.com/maps…`, `[Order Pickup or Delivery →] → getsauce.com`) — це вказівки верстальнику, не проза. task-92 уже класифікує такі блоки (`DocBlock.has_marker`); посилання з них у звірку не йдуть (рішення користувача).

### 1.5 Портфельний масштаб: перевірка знайде реальні дефекти одразу

60 найновіших `FRAMEWORK`-документів із Диска (`probe_corpus`):

| Метрика | Значення |
|---|---|
| Документів із приписаними посиланнями у Framework | **60/60** (337 рядків, ≈5.6 на документ) |
| Документів із копі-вкладкою | 56/60 |
| …з яких копія **має** гіперпосилання | **43** (220 цілей) |
| …з яких копія **не має жодного** гіперпосилання | **13 (23%)** |

Вибіркова перевірка 5 із цих 13 (`SDS_FRAMEWORK_DENTAL_IMPLANTS`, `SHB_FRAMEWORK_HOME_PAGE`, `ALPHA_BLOG_…CREWNECK`, `3XE_FRAMEWORK_ABOUT`, `SAPHNY_BLOG_…FIRST_TIMER`): у тексті копії **нуль** рядків, схожих на посилання (ні `http`, ні `(/path)`, ні `→ /`, ні слова `link`). Тобто `0 гіперпосилань` = копірайтер справді не поставив приписані посилання, а не «формат, якого ми не читаємо». Це і є фіндинг, який користувач хоче бачити.

### 1.6 Форми Framework-посилань: чинний екстрактор губить 5.6% на «— /path»

285 рядків `internal_links`/`external_links` з 50 документів, проти чинного `matrix.py:_extract_link_target`:

| Форма | Частка | Чинний екстрактор |
|---|---|---|
| `анкор → /path` | 59% | ✓ |
| `анкор (/path)` | 24% | ✓ |
| абсолютний URL | 2% | ✓ |
| **`анкор - /path` / `анкор — /path`** (весь стиль SDS: `emergency dental care - /services/emergency-dental-care/`) | **5.3%** | **✗ → `None`** |
| голий `/path` у прозі (`Repointed, replacing /tickets/ which 301s…`) | 0.4% | ✗ (і не треба — це прим. верстальника) |
| цілі нема взагалі (`order online`, `utrustning för spökjakt → placement: H2 "…"`, `Already on the page, staying as they are:`, `Cosmetic Dentistry / cosmetic dentistry`) | 7.7% | ✗ — **чесно**, вигадувати нема з чого |

Додати одну форму (дефіс/тире + пробіл + `/`) — і покриття з 86.7% стає 92%; решта 7.7% — рядки без цілі, які мають окремий стан `no_target` (§2.6), а не мовчазне зникнення.

### 1.7 Прототип ланцюга на живих даних — і три пастки, які він виявив

`probe_chain` (Framework-цілі → гіперпосилання копії → контентні посилання сайту):

| Сторінка | Framework → Copy | Copy → Site | Коментар |
|---|---|---|---|
| `laxton.se/…/ouija-board-by-laxton/` | 3/3 ✓ | 3/3 ✓ | еталон — усе сходиться |
| `laxton.se/…/rorelsesensor/` | 2/3 (нема `Övervakningssystem → /overvakningssystem-laxton/`) | 1/2 (нема `/pma/`) | обидва — справжні, дієві фіндинги |
| `effyscafe.com/locations/upper-west-side/` | 2/3 (нема `/locations/upper-east-side/`) | 1/5 | серед «нема на сайті» — `…CERT-July-2026.pdf`, а на сайті вже `…CERT-July-2027.pdf`: **копія посилається на застарілий сертифікат** — дрейф, який без зовнішніх посилань не побачили б |
| `effyscafe.com/locations/` | 2/2 ✓ | 2/11 | 9 «відсутніх» — рядки `[MAP: …]`/`[Order →]`, інструкції верстальнику (§1.4) |
| `effyscafe.com/…/columbus-ave/` | **0/3** з абсурдним «copy» | — | **пастка 1**: прототип узяв `Tab 1` за копію |

**Пастка 1 — «Tab 1» ≠ копія.** У документах без перейменованих вкладок Framework-вкладка зветься `Tab 1`, і наївне «вкладка, що не Framework = копія» віддає бойлерплейт (Hemingway/Grammarly) як «посилання копії». Реалізація мусить брати **вже розв'язані** вкладки з `package.py` (`resolved.all_copy_tabs` + `CopyTabResult.role == "primary"` з task-92 §2.5, де правильну мовну вкладку LAXTON видає покриття, не назва), а не вгадувати вдруге.

**Пастка 2 — CTA-кнопки.** `Order Now` на EFFYS — `a.elementor-button` прямо в `div`, без `<p>`. Правило «лише в прозі» дало б хибне «нема на сайті» для посилання, яке копія має і сайт має. Дозвіл для `class ~ btn|button` / `role=button` поза `nav/header/footer` (§2.2).

**Пастка 3 — дозвіл для кнопок повертає WooCommerce-шум.** `Lägg till i varukorg → /shop/utrustning/?add-to-cart=817533` ×7 — теж кнопки. Їх знімає правило «self-link за шляхом»: `inspector_key` **зберігає query** (`laxton.se/shop/utrustning?add-to-cart=817533`), тому порівняння самопосилання йде по ключу **без query**. Після цього `/shop/utrustning/`: 32 → 4, `ouija`: 9 → 4 (`Utrustning`, `prs`, `pmb`, `fareharbor`).

### 1.8 `inspector_key` зберігає регістр шляху

`laxton.se/shop/Utrustning` ≠ `laxton.se/shop/utrustning`. Це вже так для чинної матриці й для GSC↔GA4-джойнів (`app/urlinspector/urlkey.py` — чужий модуль, використовується всюди); змінювати його тут **не можна**. Для звірки посилань достатньо казефолдити шлях **локально** в `links.py` перед порівнянням (копірайтер міг написати `/Locations/`; сервер WordPress віддасть 200 на обидва). Замір потреби — DoD 6.

### 1.9 Що є в БД, чого нема

- `content_qa_packages.matrix_json` — JSON-список `MatrixRowDTO` (task-91). Нова форма рядка — усередині нього, без міграції.
- `LivePage.internal_links`/`external_links` мають **рівно одного** споживача — `matrix.py` (`grep` по `app/`: `rules.py` працює з `ContentItem`, не з `LivePage`). Семантику можна міняти, не ламаючи інших лейнів; `blocks`/`headings`/`sections` не чіпаються (task-92 DoD #8 лишається в силі).
- 44 персистовані пакети зі старою формою — читаються далі (§ шапка, «міграцій нуль»), але їхній блок посилань показує чесний стан «this report predates the link audit — run the check again».

---

## 2. Рішення (архітектура)

### 2.1 Ланцюг Framework → Copy → Site; Strategy виходить із гри посилань

Вердикт посилань — не «три джерела погоджуються», а **два послідовні кроки**:

```
Framework (приписані цілі)  ──► Copy (гіперпосилання в прозі)  ──► Site (контентні посилання)
          крок «in copy»                      крок «on site»
```

Джерело істини для кроку 1 — Framework (він і є ТЗ копірайтеру). Джерело істини для кроку 2 — Copy (те, що копірайтер здав; сайт має це відтворити). Посилання, які **є в копії, але не приписані фреймворком**, теж проходять крок 2 — копія здана, значить має бути на сайті (так упіймався застарілий PDF, §1.7).

Стратегія з ланцюга виходить: на обох живих пакетах `strategy.links` — `absent`/`empty`; де вона є (8/12 стратегій, task-91 §1.2), її звірку з фреймворком робить лейн H, не ця колонка. Follow-up §6.

### 2.2 Контентні посилання сайту — `site.py`, семантика `internal_links`/`external_links` змінюється

`_HeadingParser` отримує стек тегів (не лічильники — саме лічильник без стеку «протік» у першій версії правила, §1.3) і збирає `<a href>` лише коли:

1. **не** всередині `<script>/<style>/<noscript>/<template>` (як зараз);
2. **не** всередині `<nav>/<header>/<footer>/<aside>`, елемента з `role=navigation|banner|contentinfo|search`, чи елемента, чий `id`/`class` містить `breadcrumb`;
3. **і** (є предок-проза `p|li|td|th|blockquote|figcaption|dd|dt|h1…h6` **або** сам `<a>` — кнопка: `class` ~ `\b(btn|button)\b` чи `role="button"`);
4. ціль — `http(s)`, не `#`/`javascript:`/`mailto:`/`tel:` (як зараз), **не** зображення/відео (`.jpg .jpeg .png .webp .gif .svg .avif .mp4 .webm .ico`) — PDF/DOC **лишаються** (§1.7, сертифікат);
5. **не** самопосилання: `inspector_key` без query збігається з ключем сторінки (знімає `?orderby=`, `?add-to-cart=`, «Lägg till i varukorg»).

Дедуплікація — за `inspector_key` без query; display-формат `"текст → /шлях"` лишається (Excel/UI його вже вміють). `_MAX_LINKS_PER_BUCKET = 60` лишається як запобіжник, але на заміряних сторінках контентних посилань 1–12.

Правило свідомо **без** класових евристик («menu», «widget», «sidebar») — §1.3, Elementor. Калібрування на 3 темах (Elementor/WooCommerce — LAXTON, EFFYS; ще одна не-Elementor — SDS або SAPHNY) — DoD 5.

### 2.3 Посилання копії — `readers/gdoc.py: copy_tab_blocks` вже проходить кожен абзац; додаємо `copy_tab_links(tab) -> tuple[CopyLink, ...]`

```python
@dataclass(frozen=True)
class CopyLink:
    url: str          # як у документі (абсолютний або відносний — Docs віддає те, що вставив автор)
    anchor: str       # склеєні сусідні textRun з тією ж url, пробіли нормалізовані
    in_marker: bool   # абзац класифікований як інструкція `[...]` (task-92 `_classify_copy_chunk`)
```

- Джерело — `paragraph.elements[].textRun.textStyle.link.url` **лише у вкладках копії**, які дав `package.py` (`resolved.all_copy_tabs`), ніколи у Framework-вкладці (§1.4, §1.7 пастка 1).
- Сусідні рани з тією ж `url` зливаються в один `CopyLink`; повторна поява тієї ж цілі далі в тексті — окремий запис (звірка все одно йде множинами, а UI показує перший анкор).
- `in_marker=True` → у звірку не йде, але в деталях рядка показується сірим «instruction, skipped» — оператор бачить, що ми це прочитали й свідомо пропустили, а не загубили.
- Копі-вкладок може бути кілька (LAXTON: `English Content` + `Swedish Content`). Для ланцюга береться вкладка з `CopyTabResult.role == "primary"` (task-92 §2.5 — та, чиє покриття збіглося з сайтом); якщо `copy_diffs` порожній (нема URL / сайт недоступний) — перша вкладка, і крок «on site» стає `not_checked`.

### 2.4 Framework-цілі — `matrix.py:_extract_link_target` переїжджає в `links.py` і вчить одну форму

`_extract_link_target` + `_norm_link_set` виносяться в `app/contentqa/links.py` (їхній єдиний споживач — цей ланцюг); `matrix.py` імпортує звідти. Додається форма 4: `[—–-]\s+(/\S+)` — дефіс/тире з пробілом перед шляхом (§1.6, 5.3%). Порядок спроб: абсолютний URL → `(/path)` → `→ /path` → `— /path` → `None`. Рядок із `None` — стан `no_target` (§2.6), у Framework-списку показується, у вердикт не входить.

### 2.5 Дві колонки замість шести — `links.py` дає один об'єкт на сторінку

```python
LinkPresence = Literal["yes", "no", "not_checked"]   # третій стан — не «ні»

@dataclass(frozen=True)
class LinkRow:
    target: str                    # inspector_key без query (для матчингу)
    display: str                   # як показати: "анкор → /шлях" або повний URL для зовнішніх
    origin: Literal["framework", "copy"]   # хто перший заявив це посилання
    in_copy: LinkPresence          # крок 1; для origin="copy" завжди "yes"
    on_site: LinkPresence          # крок 2
    note: str | None = None        # "no target in framework row", "instruction, skipped", …

@dataclass(frozen=True)
class LinkAudit:
    state: Literal["checked", "not_checked", "not_applicable"]
    reason: str | None             # чому not_checked/not_applicable (англійською, показується як є)
    in_copy: FieldVerdict          # "match" | "mismatch" | "not_comparable" + detail "2/3 framework links in copy"
    on_site: FieldVerdict          # …+ detail "1/2 copy links on the live page"
    rows: tuple[LinkRow, ...]      # деталі, одне посилання = один рядок
    skipped: tuple[str, ...]       # display-рядки без цілі / інструкції — показати, не рахувати
```

`MatrixRow.links: LinkAudit | None` (`None` для `strategy_only` і для пакетів до цієї таски). `SourceCells` **втрачає** поле `links`, `RowVerdicts` **втрачає** `links` — «Outgoing links» більше не є шостим полем трьох мінітаблиць. Excel `Link 1…Link 6` зникають; замість них дві колонки `In copy` / `On site` (§2.7).

`FieldVerdict` перевикористовується без змін (`outlier_sources=()` — у ланцюга нема «крайнього», є крок, що зламався).

### 2.6 Чесні стани (PLAN §12)

| Ситуація | `LinkAudit.state` | `in_copy` | `on_site` |
|---|---|---|---|
| Framework без жодної цілі (усі рядки `no_target` або поле порожнє) і копія без гіперпосилань | `not_applicable`, reason `"No links to compare: the framework declares none and the copy has none."` | `not_comparable` | `not_comparable` |
| Framework має цілі, копі-вкладки **нема** | `checked` | `not_comparable`, detail `"no copy tab in this document"` | `not_comparable` |
| Framework має цілі, копія є, гіперпосилань 0 | `checked` | **`mismatch`**, `0/N framework links in copy` (§1.5 — це справжній дефект, не «не вміємо читати») | залежить від сайту |
| `genre == "edit_brief"` (task-92, ≥2 маркери) | `checked` | вердикти рахуються, але кожен рядок несе `note="edit brief — links may live in the existing page"` і бейдж у UI сірий, не червоний (як task-92 робить для `missing`) |  |
| Сайт недоступний / `blocked` / нема URL | `checked` | рахується | `not_comparable`, detail = `site.error` або `"no URL"` |
| Усе на місці | `checked` | `match` | `match` |

«Сходиться» на рівні сторінки (бейдж у заголовку групи + фільтр «Mismatches only» + аркуш `Mismatches`) = обидва кроки не `mismatch`.

### 2.7 Excel — один DTO, дві колонки

`matrix_xlsx.py`: `_ALL_LABELS = 5 скалярних + ("In copy", "On site")`, `_TOTAL_COLUMNS = 7`. У мінітаблиці **Framework** обидві клітинки несуть вердикт зі зведенням (`✓ 3/3` / `✗ 2/3 — missing: /overvakningssystem-laxton/`), тон — той самий `_tone_font_fill_border`; у **Strategy** і **Site** ці дві колонки порожні з сірим «—» (ланцюг не має «сторони» для них). Аркуш `Mismatches` бере «✗» з нового бейджа. Per-page аркуші Copy (task-92) отримують унизу блок `Links` з рядками `LinkRow` (link / in copy / on site / note) — це і є «деталі», яких у двох колонках Comparison нема.

---

## 3. Етапи

1. **`site.py` — контентні посилання (§2.2)**: стек тегів, правила 1–5, тести на живих HTML-фрагментах з §1.3 (Elementor header/footer, `nav.woocommerce-breadcrumb`, `p#breadcrumbs`, `a.elementor-button`, `?add-to-cart=`, галерея `.jpg`, PDF у `<p>`). Оновити `test_nav_and_footer_links_are_included_unlike_headings` → зворотна семантика, з посиланням на §1.1 у docstring.
2. **`readers/gdoc.py` — `copy_tab_links` (§2.3)**: тести на злитих ранах (`'' + 'X5 EMF-mätare'`), на маркер-абзацах, на тому, що Framework-вкладка **не** є джерелом.
3. **`links.py` (§2.4–2.6)**: чиста функція `audit_links(framework_item, copy_links, copy_genre, site_page) -> LinkAudit`; таблиця станів §2.6 — по тесту на рядок; форма `— /path`; казефолд шляху.
4. **`package.py`**: `FrameworkOutcome.link_audit` (обчислюється поряд із `copy_diffs`, з тієї самої `site_page` — нуль додаткових HTTP). `matrix.py`: `MatrixRow.links = outcome.link_audit`, `SourceCells`/`RowVerdicts` без `links`.
5. **DTO + API**: `LinkRowDTO`, `LinkAuditDTO`, `MatrixRowDTO.links: LinkAuditDTO | None = None`; `MatrixSourceCellsDTO`/`MatrixRowVerdictsDTO` без `links`; `openapi_snapshot.json`; перевірити, що 44 старі пакети віддаються `GET /reports/{id}` без 500.
6. **Excel (§2.7)** + тести `test_matrix_xlsx.py` (замінити `test_outgoing_links_split_into_one_sub_column_per_link`/`…beyond_the_cap…`).
7. **Фронт**: `MATRIX_FIELDS` без `links`; блок `Outgoing links` під трьома мінітаблицями групи сторінки — дві колонки-вердикти + розгортання `<details>` з рядками `LinkRow` (текст `in copy` / `on site` / `skipped`); бейджі в заголовку групи: `In copy`, `On site` замість `Outgoing links`; «Mismatches only» враховує обидва. `?v=` бамп по ланцюжку (`tools-page.js` → `strategy-package-tool.js`). Візуальний loop 1440/390.
8. **Живий прогін** EFFYS + LAXTON: очікувані результати з §1.7 відтворені; §1.1 більше не «зелений».
9. **E2E** `tools-strategy-package.cjs`: очікує `.sp-links` блок і два бейджі; `.sp-matrix-mini` більше не має колонки `Outgoing links`.

---

## 4. Ризики й пастки

- **Правило «в прозі» на не-Elementor темах** (Shopify ALPHA, Squarespace, кастомні): контентні посилання можуть лежати у `<div>` без `<p>` → хибне «нема на сайті». Калібрування на третій темі обов'язкове (DoD 5); якщо покриття падає — розширити дозвіл на `<span>`/`<div>` **усередині** `<main>`/`<article>`, але не глобально.
- **Дозвіл кнопок повертає UI-кнопки** (кошик, «Читати далі» карток блогу). Самопосилання по шляху знімає кошик/сортування; картки блогу з `class="btn"` на список-сторінках лишаються — це реальні контентні посилання, не шум.
- **`inspector_key` не чіпати** (§1.8) — казефолд тільки в `links.py`.
- **Стара форма в БД**: будь-який `MatrixRowDTO(**m)` на старому рядку має пройти — тест на фікстурі з реальним старим `matrix_json` (взяти з пакету `e912be8b`).
- **`textStyle.link` може бути `{"bookmarkId": …}` або `{"headingId": …}`** (внутрішні якорі документа), не `url` — ігнорувати без винятку.
- **Відносні `url` в копії** (`/menu/` без хоста) — абсолютизувати від URL сторінки фреймворка, як уже робить `_norm_link_set(base_url=…)`.
- **Один фреймворк падає (виняток у парсері) — пакет живе** (PLAN §9.1): `audit_links` загорнутий так само, як `copy_diffs`, збій → `LinkAudit(state="not_checked", reason=…)`.

---

## 5. Definition of Done

1. На пакеті EFFYS сторінка `/locations/columbus-ave/` показує `In copy ✗ 0/3`, а не `match` (§1.1); `/locations/upper-west-side/` показує застарілий PDF у «on site ✗» (§1.7).
2. На LAXTON `ouija-board` — обидва кроки ✓; `rorelsesensor` — `2/3` / `1/2` з названими відсутніми цілями (§1.7).
3. Контентних посилань на 6 заміряних сторінках §1.2 — у межах ±2 від таблиці (4/3/1/3/4/10), жодного `/podcast/`, `/gallery/`, `?add-to-cart=`, `.jpg`.
4. Framework-вкладка ніколи не є джерелом копі-посилань: `hemingwayapp.com`/`grammarly.com` не з'являються в жодному `LinkRow` жодного з 60 документів §1.5 (скрипт-перевірка, вивід у Completion report).
5. Калібрування на третій темі (не Elementor): один живий пакет SDS або SAPHNY, вручну звірені ≥3 сторінки, розбіжність правила з очима — у Completion report.
6. Замір потреби казефолду: скільки цілей на 3 пакетах збігаються лише після casefold — число в Completion report.
7. Усі рядки таблиці §2.6 закриті юніт-тестами; `0 гіперпосилань у копії при N цілях фреймворка` = `mismatch`, не `not_comparable`.
8. 44 старі пакети: `GET /reports/{id}` → 200, блок посилань — «predates the link audit», Excel відкривається.
9. Excel: 7 колонок у Comparison, `Mismatches` містить рівно сторінки з ✗ хоча б в одному кроці, per-page аркуш має блок Links.
10. `ruff check .`, `mypy app`, `pytest tests/unit/contentqa tests/unit/api` — зелені в Docker; e2e `tools-strategy-package.cjs` — зелений проти живого стека.
11. Фронт: чекліст `AGENTS.md §15–16` — 1440/390, нуль емодзі, нуль inline-стилів, тон лише класами (CSP), `?v=` збамплено по ланцюжку, нуль горизонтального скролу.
12. Completion report називає: скільки з 60 документів §1.5 дали `in_copy = mismatch` після реалізації (очікування ≈13).

---

## 6. Follow-ups (свідомо поза обсягом)

- **Strategy → Framework для посилань** (8/12 стратегій мають `internal_links`): окремий рядок у лейні H (`sync-link-drift`), не ця колонка.
- **Анкор-текст**: зараз матчимо лише ціль; «анкор у копії ≠ анкор у фреймворку» (`utrustning för spökjakt` vs `Utrustning`) — окреме інфо-правило, коли буде запит.
- **Редиректи цілей**: копія посилається на `/tickets/`, який 301-ить на `/edison-nj/` (3XE, §1.6) — звірка по кінцевій URL вимагає HTTP на кожну ціль; наразі порівнюємо як написано.
- **Рядки без цілі у Framework (7.7%)** — підказка копірайтеру «додайте шлях» як `warn` у Framework QA, не тут.

---

## ✅ Completion report

- **Дата:** 2026-09-14
- **Гілка / комміт:** `feat/task-95-outgoing-links` @ e35de9a
- **Статус:** done

### Що зроблено

Ланцюг посилань Framework → Copy → Site живий у Strategy Package Check. Замінено колишню шосту колонку Comparison («Outgoing links», яка порівнювала приписані посилання з **усіма** `<a href>` сторінки) на дві колонки-вердикти **`In copy`** і **`On site`**: сайт тепер віддає лише **контентні** посилання (поза nav/header/footer/крихтами, у прозі чи CTA-кнопці, без самопосилань і без зображень), копі-вкладка вперше читається на гіперпосилання (раніше `readers/gdoc.py` брав лише текст, викидаючи `textStyle.link.url`), а Framework-цілі видобуваються п'ятьма живими формами (додана `— /path`, SDS-стиль). У Excel — ті самі дві колонки на Framework-рядку (Strategy/Site показують сірий «—»), плюс блок `Links` унизу per-page аркуша Copy з деталями по кожному посиланню. У фронті — ті самі дві колонки в трьох мінітаблицях, бейджі в заголовку сторінки, розгортка `<details>` з таблицею (link/in copy/on site/note), відкрита автоматично при мисметчі.

**Живо перевірений результат** (§1.1 EFFYS columbus-ave): вердикт більше не бреше «зелений», коли посилання копії реально не дійшли до сайту — на цій же сторінці `on_site` тепер `✗ 1/3`, показуючи, що `/about-us/` і `/locations/upper-west-side/` копія обіцяла, а сайт не має. На EFFYS_UWS ланцюг ловить дрейф застарілого PDF-сертифіката (копія посилається на `…CERT-July-2026.pdf`, сайт уже віддає `…CERT-July-2027.pdf`).

### Як зроблено (ключові рішення)

- **`app/contentqa/site.py`** — переписаний екстрактор посилань: стек `(tag, chrome, prose)` замість лічильника, дозвіл для CTA-кнопок (`class~=btn|button` чи `role=button`) поза прозою, виключення самопосилань за `inspector_key`-без-query. `LivePage.internal_links`/`external_links` тепер `tuple[SiteLink, ...]` (структуровано `url`+`text`, не рядок `"текст → шлях"`) — єдиний споживач цього поля (`links.py`) отримав повний абсолютний URL, потрібний для матчингу зовнішніх посилань (PDF/getsauce/maps), які старий формат просто не міг коректно позначити.
- **`app/contentqa/readers/gdoc.py`** — `+CopyLink`/`+copy_tab_links()`: зливає сусідні `textRun` з однаковою `url` в один анкор, класифікує абзац як `in_marker` через уже наявний `_classify_copy_chunk` (той самий, що task-92 використовує для `[MAP: …]`/`[Order →]`).
- **`app/contentqa/verdict.py`** (новий) — `FieldVerdict`/`Verdict` винесені з `matrix.py` в окремий модуль без залежностей: `links.py` (яку `package.py` викликає) і так уже імпортує з `package.py`, а `matrix.py` імпортує `package.py` — визначення `FieldVerdict` у `matrix.py` створило б цикл `matrix → package → links → matrix`. `matrix.py` ре-експортує обидва імені без зміни публічного інтерфейсу.
- **`app/contentqa/links.py`** (новий) — `audit_links()`, чиста функція: `_extract_link_target` (перенесено з `matrix.py`, +форма `— /path`), `_match_key` (казефолд локально тут — `inspector_key` регістр шляху навмисно не займає), таблиця чесних станів (§2.6 плану): `not_applicable`, коли обидва боки чесно не заявляють жодної цілі; `0 гіперпосилань у копії при N цілях` → `mismatch`, не мовчазне «нема даних».
- **`app/contentqa/package.py`** — `FrameworkOutcome.link_audit` (нове поле) + `_primary_copy_tab()` (переюзає роль `primary`/`secondary`, яку `_run_copy_lane` уже вирішив за покриттям проти сайту, task-92 §2.5 — не вгадує вдруге). Рахується незалежно від `check_published`: коли `copy_diffs` порожній (вимкнено чи нема ролі), падає на першу вкладку документа.
- **`app/contentqa/matrix.py`** — `SourceCells`/`RowVerdicts` втратили поле `links`; `MatrixRow.links: LinkAudit | None` — рядковий, не по-джерельний блок.
- **`app/schemas/strategy_package.py`** — `LinkRowDTO`/`LinkAuditDTO` (нові), `MatrixRowDTO.links`; `MatrixSourceCellsDTO`/`MatrixRowVerdictsDTO` без `links`.
- **`app/contentqa/matrix_xlsx.py`** — 7 колонок замість 12 (5 скалярних + `Link 1…6`); блок `Links` на per-page аркуші Copy тепер створюється **навіть коли** сторінка не має жодного sentence-diff issue (task-92), лише заради ланцюга посилань — інакше сторінка з чистою прозою, але зламаним ланцюгом посилань не отримала б аркуша взагалі.
- **`frontend/js/strategy-package-tool.js`** / **`frontend/css/strategy-package.css`** — те саме дзеркало Excel: 7-колончасті мінітаблиці, бейджі `In copy`/`On site` у заголовку сторінки, `<details class="sp-links-details">` з таблицею деталей (`renderLinkDetails`), відкрита автоматично при мисметчі.

### Контрактні зміни (Contract changes)

`MatrixRowDTO`/`MatrixSourceCellsDTO`/`MatrixRowVerdictsDTO` — публічний контракт `GET /reports/{id}` і `GET /reports/{id}/matrix.xlsx`: `links` пішов із `MatrixSourceCellsDTO`/`MatrixRowVerdictsDTO`, з'явився на рівні рядка (`MatrixRowDTO.links: LinkAuditDTO | None`) + нові DTO `LinkRowDTO`/`LinkAuditDTO`. Зворотна сумісність: 44 пакети, персистовані до цієї таски, читаються без падіння — `links` дефолтиться в `None` (Pydantic-модель без `extra="forbid"`, зайвий старий `links`-ключ у застарілому `matrix_json` просто ігнорується). `openapi_snapshot.json` перегенеровано, DoD-тест зелений.

### Як перевірити (verification)

```bash
docker compose up -d --build
docker compose exec api poetry run pytest tests/unit/contentqa tests/unit/api -q
docker compose exec api poetry run ruff check app/contentqa app/schemas/strategy_package.py
docker compose exec api poetry run mypy app
NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 \
  node tests/e2e/frontend/tools-strategy-package.cjs
```

Живий прогін: `POST /api/v1/tools/strategy-package/check` на EFFYS (`file_id=1cLRy5q9w0aHRv4u1pHEvlu8Tp_bQGyLGtTSAFdCMYMQ`, frameworks `EFFYS_COLUMBUS_AVE`+`EFFYS_UWS`) → відкрити `#/tools/strategy-package/<id>` → вкладка Comparison показує 7 колонок, `In copy ✓ 3/3` / `On site ✗ 1/3` на columbus-ave, розгорнутий блок Links із трьома рядками. Excel-експорт (`Export to Excel`) відкривається, аркуш `columbus-ave` має блок «Links (Framework -> Copy -> Site)» унизу.

### Тести

- **`tests/unit/contentqa/test_links.py`** (новий, 20 тестів) — `_extract_link_target` (усі 5 форм включно з новою `— /path`), `_match_key` (казефолд, ігнор query), `audit_links` — уся таблиця чесних станів §2.6: `not_applicable`, false-green-фікс (§1.1 відтворено буквально), стейл-PDF через copy-only посилання (§1.7), 0-гіперпосилань-це-mismatch (§1.5), нема-копі-вкладки, marker-рядки пропускаються, сайт недоступний/нема URL, `edit_brief`-нотатка.
- **`tests/unit/contentqa/test_readers_gdoc.py`** (+7 тестів) — `copy_tab_links`: злиття сусідніх ранів, роздільні різні `url`, ігнор внутрішніх якорів без `url`, `in_marker`-класифікація, Framework-вкладка як джерело (функція сама не фільтрує — це відповідальність викликача).
- **`tests/unit/contentqa/test_package.py`** (+5 тестів) — `link_audit` на `run_framework`: `not_applicable` без Frame-вкладки, false-green на реальній EFFYS-формі, ланцюг обирає МОВНУ (не першу) копі-вкладку через роль task-92, рахується і при `check_published=False`.
- **`tests/unit/contentqa/test_site.py`** (переписано §"outgoing links", +5 нових) — інвертовано `nav`/`footer`-тест (раніше «включені», тепер «виключені» — жива поправка до task-91 §3.3), крихти без `<nav>` (лише `id="breadcrumbs"`), Elementor-`widget`-клас НЕ chrome, CTA-кнопка поза `<p>`, WooCommerce cart/sort виключені як самопосилання, зображення виключені/PDF лишається.
- **`tests/unit/contentqa/test_matrix.py`** — прибрано тести перенесених функцій, додано `test_matrix_row_carries_the_outcome_link_audit`.
- **`tests/unit/contentqa/test_matrix_xlsx.py`** — переписано два тести колишніх `Link 1…6` на нові 2-колончасті вердикти + `not_comparable`-текст + `links=None` fallback + `Mismatches`-аркуш з ланцюгом.
- **`tests/e2e/frontend/tools-strategy-package.cjs`** — +перевірка: обидва бейджі `In copy`/`On site` у заголовку сторінки, 7 колонок у Framework-мінітаблиці, за наявності `.sp-links-details` — 4 очікувані заголовки колонок.

Підсумок прогону: **993 passed, 1 failed** (`test_dashboard_card_counts_measured_open_errors` — підтверджено `git stash`-ом як дореформений, дато-чутливий дефект, не пов'язаний із цією таскою) + e2e зелений.

### Живі заміри (DoD)

- **DoD 3/4** (портфель 60 живих Framework-документів): **0** протікань бойлерплейт-посилань (`hemingwayapp.com`/`titlecaseconverter.com`/`grammarly.com`) у жодному `LinkRow`.
- **DoD 5** (каліброка на не-Elementor темі, SDS): 3 сторінки, ручний дамп усіх `<a>` з ланцюгом предків — мегаменю (`li>ul>li>a` всередині зовнішнього `<nav>`), соцмережі-іконки й футерні `Privacy/Financial Policy` коректно виключені; жодного протікання шуму в контентні посилання. Відомий пробіл (не помилка, задокументований прогалиною плану §4): футерна CTA `"Reserve Your Appointment"` без явного класу `btn`/`button` не потрапляє в контентні — рамка v1 свідомо не намагається вгадувати кнопку за виглядом.
- **DoD 6** (потреба казефолду): 20 пар framework-ціль↔copy-посилання зіставлено на 6 живих документах, **0** зі знайдених пар потребували казефолду для збігу (реальний портфель поки що консистентний за регістром) — казефолд лишається захисним запобіжником на майбутнє, не holостим кодом.
- **DoD 12** (скільки з 60 дали `in_copy=mismatch`): **23/60** у **корпусному** замірі (без живого фетчу сайту — багатовкладкові документи тому падають на ПЕРШУ вкладку, а не на роль-обрану `primary` за покриттям task-92, як робить продакшн `run_framework`). Це верхня межа, не точне продакшн-число: живий прогін (LAXTON_RORELSE, перевірено прямим викликом `run_framework`) показує, що коли ланцюг коректно бере мовно-правильну вкладку, число часом падає (English-вкладка мала 2/3, але справжня Swedish-вкладка, яку показує сайт, має 0/3 — інший, не менш реальний дефект).

### Відхилення від плану (deviation) + причина

1. **§5 DoD 1/2 конкретні числа не збіглися з планом, але діагноз підтвердився.** План (написаний на прототипі без ролевого вибору копі-вкладки) очікував `in_copy=0/3` на EFFYS columbus-ave; жива реалізація (яка коректно переюзає `_run_copy_lane`'s роль за покриттям, task-92 §2.5) дає `in_copy=match, on_site=mismatch(1/3)` — головний баг (хибне «зелено» від меню) так само зникає, лише манфестується на іншому кроці ланцюга, бо прототип помилково брав `Tab 1` (Framework) за копію. Аналогічно LAXTON_RORELSE: план очікував `2/3`/`1/2`, жива реалізація коректно обирає Swedish-вкладку (мовний match із сайтом) — і саме вона має **0** реальних гіперпосилань, тобто справжній дефект копірайтера, зловлений точніше, ніж прототип міг показати.
2. **`FieldVerdict`/`Verdict` винесені в окремий `app/contentqa/verdict.py`**, не лишені в `matrix.py` — не в owned paths плану буквально, але потрібно для уникнення циклу імпортів `matrix → package → links → matrix`; `matrix.py` ре-експортує обидва імені, зовнішній контракт не зламано.
3. **`LinkAudit.state`** — у плані `Literal["checked", "not_checked", "not_applicable"]`; реалізовано без `"not_checked"` (він виявився недосяжним: `run_framework` уже повертає ранній `not_accessible`/`no_recognized_tabs` до того, як код доходить до `audit_links`, тож усередині функції завжди `checked`/`not_applicable`).
4. **Блок `Links` на Excel-аркуші Copy** — план не деталізував гейтинг створення аркуша; реалізовано так, що аркуш створюється або за наявності sentence-diff issues (task-92, як і раніше), або за наявності мисметчу в ланцюгу посилань (task-95) — без цього сторінка з чистою прозою, але зламаними посиланнями не отримала б аркуша для показу деталей.

### Follow-ups / борг

- **Strategy → Framework для посилань** (§6 плану) — окремий рядок лейну H, не ця колонка. Не робилося.
- **Анкор-текст** (копі-анкор ≠ Framework-анкор) — окреме info-правило, коли буде запит.
- **Редиректи цілей** (3XE: `/tickets/` 301 на `/edison-nj/`) — звірка йде "як написано", без HTTP-резолву кінцевої URL.
- **7.7% Framework-рядків без цілі** (`order online`, тощо) — підказка копірайтеру як окреме `warn`-правило Framework QA, не в цій звірці.
- **Футерна CTA без класу `btn`** (SDS "Reserve Your Appointment") — свідомий пробіл рамки v1 (§4 плану), розширення дозволу на візуальний стиль кнопки — окрема задача, якщо запит з'явиться.
