# task-60 — Web Archive: повнота ассетів (CSS-картинки, шрифти) і живі ембеди замість зламаних iframe

- **Пакет:** довести replay до візуальної відповідності оригіналу. Дві речі, які зараз ламають картинку: (1) кожен ресурс, підключений **через CSS**, ніколи не захоплювався — 1 456 унікальних файлів (1 332 картинки + 124 шрифти) реально відсутні в архіві; (2) кожен `<iframe>` переписується на `/archive/r/...` і віддає «Not archived» — замість карт, відео й аудіо оператор бачить порожню рамку.
- **Хвиля:** U — Web Archive (друга таска).
- **Depends on:** **59** ✅ (весь пайплайн захоплення/replay; ця таска його розширює, нічого не переписує).
- **Owned paths:** `app/webarchive/**`, `app/api/archive_replay.py`, `app/api/archive.py`, `app/schemas/web_archive.py`, `app/db/repositories/web_archive.py`, `frontend/js/archive-*.js`, `frontend/css/archive.css`, `tests/unit/webarchive/**`, `tests/unit/api/test_archive_*.py`
- **Адитивні дотики (узгодити):** `app/cli.py` (+1 команда `archive-backfill-assets`), `app/core/config.py` (2 нові ключі)
- **Міграції:** **не потрібні.** Усе лягає в наявні `archive_assets` / `archive_asset_versions`.
- **PLAN.md:** §12 (чесні стани — «не знято» має бути видно, а не мовчки зламане), §9.1 (ізоляція клітинки), §13 (SSRF-гард на кожному фетчі)
- **frontend/AGENTS.md:** §5 (правда даних), §12 (CSP), §14 (UI-копірайт англійською)
- **Нових залежностей — нуль.**

---

## Мета

Оператор відкриває знімок і бачить сторінку такою, якою вона була. Сьогодні він бачить її **без фонових зображень, без частини шрифтів і з порожніми прямокутниками замість карт**. Ця таска закриває обидві дірки й робить повноту вимірюваною, а не «на око».

---

## §0. Досліди (виміряно на бойових даних 2026-09-05)

Не гіпотези — цифри з реального портфеля (1 566 знімків, 13 369 ассетів, 673 CSS-файли).

### §0.1 Відправна точка — конкретний баг користувача

`https://seo.theyovth.space/archive/r/20260904133527/https://hearwellservices.com/wp-content/uploads/2025/02/image-1.webp` → «Not archived at this point in time.»

Простежено до кінця:
- рядка в `archive_assets` для цього URL **немає взагалі** — тобто ассет не «не завантажився», його **ніколи не знайшли**;
- у жодному захопленому HTML HWELL цього URL немає;
- знайдено його тут: `wp-content/uploads/elementor/css/post-7052.css` → `.elementor-motion-effects-layer{background-image:url("https://hearwellservices.com/wp-content/uploads/2025/02/image-1.webp")}`.

Тобто ресурс підключений **лише з CSS-файлу**. `discover_css_assets()` у нас написаний і покритий тестами, але **не підключений до пайплайну** (свідомо відкладений followup task-59). При цьому replay `url()` у CSS **переписує** — виходить гарантований 404 на кожному фоні.

### §0.2 Масштаб дірки

| Джерело посилання | Знайдено посилань | Розпізнає поточна розвідка | **Відсутні в архіві** |
|---|---|---|---|
| Інлайн `<style>` блоки в HTML | 7 462 / 200 знімків | ні | **6 240 звернень**, 1 228 унікальних |
| `url()` всередині завантажених CSS-файлів | 2 215 / 400 файлів | ні | 228 унікальних |
| **Разом по портфелю** | | | **1 456 унікальних: 1 332 картинки + 124 шрифти** |

Типи відсутніх (з CSS-файлів): `.webp` 62, `.svg` 60, `.woff2` 39, `.png` 21, `.woff` 16, `.ttf` 8, `.jpg` 6, `.eot` 6. Тобто **та сама дірка ламає і шрифти** — це пояснює «поїхалу» типографіку в replay, яку раніше списували на CORS.

### §0.3 Що НЕ є причиною (перевірено, щоб не лікувати здорове)

- **Бюджет ассетів на сторінку** (`archive_new_assets_per_page = 300`): знімків з `assets_truncated = 1` — **0 з 1 552**. Ліміт не досягається ніколи.
- **Невдалі завантаження**: ассетів без жодного збереженого блоба — **2 з 13 369** (`failure_count > 0`: 1). Мережа працює.
- **Лінива підвантажка `data-lazy-src` / `data-lazy-srcset`**: розпізнається (випадково, але коректно — регекси не прив'язані до початку імені атрибута). Перевірено на EFFYS і HWELL.

### §0.4 Екзотичні патерни — вимірювання, що їх НЕ треба підтримувати

Прогнано по 200 знімках. Нуль — це нуль, і будувати під них код не будемо:

| Патерн | Входжень |
|---|---|
| `data-bg` / `data-background` / `data-background-image` | **0** |
| `poster` (відео/зображення) | **0** |
| `<link imagesrcset>` | **0** |
| SVG `<use href>` (спрайти) | **0** |
| SVG `<image href>` | **0** |
| CSS `image-set()` | **0** |
| `<object data>` / `<embed src>` | **0** |
| `@import` у CSS | **1** на 673 файли (`@import "bare"` — 0) |

**Висновок:** розширювати треба рівно **два** місця — інлайн `<style>` і тіла CSS-файлів. Рекурсію по `@import` не будуємо (одне входження на портфель), решту патернів не додаємо взагалі.

### §0.5 Ключова сприятлива обставина — бекфіл лікує заднім числом

`WebArchiveRepository.resolve_asset_version` при відсутності версії «на момент або раніше» **падає назад на найранішу наявну** ([web_archive.py:407](app/db/repositories/web_archive.py#L407)). Отже ассет, завантажений сьогодні, автоматично підхоплюється **всіма старими знімками**. Бекфіл не треба прив'язувати до дат — досить один раз докачати відсутнє.

### §0.6 Iframe — що там насправді (усі 1 566 знімків)

| Хост | Посилань |
|---|---|
| `about:blank` (плейсхолдери плагінів) | 1 521 |
| `www.googletagmanager.com` | 349 |
| `www.google.com` (maps/embed) | 54 |
| `maps.google.com` | 16 |
| `open.spotify.com` | 12 |
| `www.youtube.com` | 2 |
| `player.vimeo.com` | 1 |

Атрибут: `src` — 1 914, `data-lazy-src` — 41. Тобто змістовних ембедів **~85** (карти + аудіо/відео), і рішення користувача «не бекапити, лишати оригінальний src» повністю відповідає даним: це не контент сайту, це віджети третіх сторін, які на бекапі коштують довго й ловлять антибот.

### §0.7 Спайк: чи виживе вкладений iframe у нашому sandbox

Ризик був реальний: sandbox-прапорці **успадковуються** вкладеними фреймами, а наш replay-фрейм навмисно без `allow-same-origin`. Якби Google Maps вимагав власного origin — рішення «лишити live src» не спрацювало б.

Перевірено експериментом (Playwright, реальний maps-ембед EFFYS, точна копія нашої структури `сторінка → sandbox-фрейм → maps-фрейм`):

```
sandbox="allow-scripts allow-forms allow-popups"        (те, що ми віддаємо сьогодні)
   google/maps відповідей: 9 (усі 200)   console errors: 0   → карта промальовується повністю
```

**Sandbox послаблювати не треба.** Єдине, що ламає ембеди сьогодні, — наш власний рерайт і наш власний CSP.

---

## §1. Рішення 1 — ассети з CSS (картинки + шрифти)

### §1.1 Інлайн `<style>` блоки

Розширити `discover_html_assets()`: окрім наявних патернів, витягти тіла `<style>…</style>` і прогнати кожне через уже написаний `discover_css_assets(block, base_url)`.

Чому саме там, а не окремим викликом у `capture.py`: `discover_html_assets` — єдина точка входу «що браузер завантажив би для цієї сторінки», і вона має лишитись єдиною.

### §1.2 Тіла завантажених CSS-файлів (один рівень)

`_capture_assets_for_page` сьогодні — плоский `for` по результатах розвідки HTML. Перетворити на **чергу з одним рівнем розгортання**:

1. черга ініціалізується посиланнями з HTML;
2. коли обробляється ассет `kind == "css"` і в нього є збережений блоб (свіжо завантажений **або** підтверджений 304 — тіло читаємо з локального сховища, це дешево), його тіло проганяється через `discover_css_assets` і знайдене додається в чергу;
3. кожен CSS розгортається **один раз на сторінку** (дедуп по `url_key`), глибина — **1** (CSS всередині CSS не слідуємо: §0.4, одне входження `@import` на весь портфель).

Бюджет `archive_new_assets_per_page` лишається спільним — при 300 і фактичному нулі усічень (§0.3) запасу вистачає; якщо після ввімкнення почнуть з'являтись `assets_truncated`, це видно в Meta й піднімається окремо.

**Ізоляція:** кожен ассет — власна клітинка (як зараз), збій розгортання CSS не валить сторінку.

### §1.3 Бекфіл наявних знімків

CLI `archive-backfill-assets [--client-id X | --all] [--dry-run]`:
- пройти збережені знімки (інлайн `<style>`) і збережені CSS-блоби (`url()`);
- зібрати посилання, яких немає в `archive_assets`;
- завантажити через той самий `_check_or_fetch_asset` (тобто SSRF-гард, `HostThrottle`, ліміти розміру — усе штатне);
- `--dry-run` друкує скільки і чого бракує, нічого не пише.

Очікуваний обсяг першого прогону: **1 456 файлів**. Завдяки §0.5 це ретроактивно чинить усі 1 566 знімків.

Чесність: файли, які на живому сайті вже видалені, повернуть 404 → лишаться дірою. Це правильний результат (архів не вигадує), і він має бути **видимим** — див. §1.4.

### §1.4 Вимірювана повнота (щоб «ідеал» був числом)

- У Meta-панелі знімка вже є `assets_missing` ([archive.py:303](app/api/archive.py#L303)) — зробити його помітним: `212 captured · 4 missing` з тоном `warning` при `missing > 0`.
- На хабі — плитка **Asset coverage** (`captured / referenced` по портфелю) поруч із наявними лічильниками.

Це і є критерій приймання: до фіксу — 1 456 відсутніх, після бекфілу — тільки ті, що реально мертві на джерелі.

---

## §2. Рішення 2 — iframe лишаємо живими

Рішення користувача: не архівувати, лишати оригінальний `src`. Дані (§0.6) і спайк (§0.7) це підтверджують.

### §2.1 Три місця, які треба змінити

1. **`rewrite.py`** — тег `<iframe>` (і `<frame>`) виводиться з-під атрибутного рерайту тим самим механізмом плейсхолдерів, яким уже захищені тіла `<script>`/`<style>`: тег стешиться цілком і повертається байт-у-байт. Так само не чіпається `data-lazy-src` на ньому (41 випадок — ті самі ембеди).
2. **CSP у `archive_replay.py`** — `frame-src 'self'` → `frame-src https:`. Це **єдиний** напрямок, у якому replay свідомо дивиться в живий інтернет; решта директив лишаються `'self'` без винятків.
3. **`shim.js`** — прибрати `HTMLIFrameElement` із списку пропатчених сеттерів і додати виняток у патч `setAttribute` (`this.tagName !== "IFRAME"`), інакше JS-вставлені ембеди знову поїдуть у `/archive/r/`.

### §2.2 Аналітичні iframe лишаємо заблокованими (важливо)

349 з ембедів — це `googletagmanager.com` (noscript-піксель GTM). Пустити їх у живий інтернет означає **слати хіти в GA клієнта щоразу, коли хтось відкриває архівний знімок** — тобто ми власноруч засмічуємо дані клієнта.

Тому: не «всі iframe живі», а **список аналітичних хостів лишається переписаним** (тобто мовчки не завантажується): `googletagmanager.com`, `google-analytics.com`, `facebook.com/tr`, `doubleclick.net`, `connect.facebook.net`. Усе інше — живе.

Це не ускладнення заради ускладнення: це різниця між «карта працює» і «ми зіпсували клієнту статистику».

### §2.3 Чесність в UI

Живий ембед у знімку 3-місячної давнини показує **сьогоднішній** стан. Це компроміс, і він має бути видимим, а не прихованим:

- у Meta-панелі — рядок `Live embeds: 2 (www.google.com, open.spotify.com)`;
- підпис поруч: `Embedded third-party frames are loaded live, not archived.`

Приватність: браузер оператора звертається до Google/Spotify напряму з opaque-origin фрейму. Для внутрішнього інструмента прийнятно, але фіксуємо явно.

---

## §3. Порядок робіт

| # | Крок | Перевірка |
|---|---|---|
| 1 | `discover_html_assets` + інлайн `<style>` | юніт на реальному Elementor-фрагменті |
| 2 | Черга з розгортанням CSS у `_capture_assets_for_page` | юніт: HTML → CSS → фон + шрифт потрапили в чергу; CSS розгортається один раз |
| 3 | CLI `archive-backfill-assets --dry-run` | має показати ≈1 456 |
| 4 | Бекфіл наживо (`--all`) | HWELL `2025/02/image-1.webp` віддається як `image/webp` |
| 5 | Iframe: rewrite + CSP + shim + денай-лист аналітики | юніт на кожен із трьох шарів |
| 6 | Meta/хаб: `assets_missing`, Asset coverage, Live embeds | візуальний loop, AGENTS §15–16 |
| 7 | Playwright: EFFYS (карта) + HWELL (фони) | нуль зламаних `<img>`, карта рендериться |

---

## §4. Ризики

- **Зростання обсягу.** +1 456 файлів одноразово, далі — приріст на нові фони. Дедуп контент-адресний, ті самі теми в кількох клієнтів злипнуться. Оцінка до 200 МБ; якщо більше — це сигнал, а не аварія.
- **Навантаження першого бекфілу.** Іде через `HostThrottle`, послідовно на хост. Запускати вручну, не в слот 13:30/19:30, щоб не змагатись за writer-лок (урок task-59 hotfix #2).
- **`frame-src https:` — свідоме послаблення.** Обмежене рівно фреймами; `default-src`/`img-src`/`script-src`/`connect-src` лишаються `'self'`. Без цього рішення користувача не реалізується взагалі.
- **Живий ембед ≠ архів.** Якщо Google колись зламає старі `pb=`-посилання, у знімку буде порожньо. Прийнятно: альтернатива (бекап карти) дорожча й ловить антибот.

---

## Definition of Done / Gate

1. `archive-backfill-assets --dry-run` після фіксу показує **0** нових відсутніх на свіжозахопленому клієнті (тобто розвідка більше не втрачає нічого, що сама ж потім переписує).
2. HWELL `.../2025/02/image-1.webp` віддається зі `content-type: image/webp` на тому самому `ts`, що зараз дає 404.
3. Playwright на EFFYS: нуль зламаних `<img>` **і** промальована карта в живому ембеді.
4. GTM-iframe у знімку **не** робить запиту в googletagmanager (перевірити перехопленням мережі).
5. Meta показує `captured / missing`, хаб — Asset coverage; обидва числа збігаються з прямим запитом до БД.
6. `ruff` + `mypy` чисті, повний `pytest` без нових фейлів, нові юніти на кожен шар §3.
7. Completion report за шаблоном CLAUDE.md, план перенесено в `doc/dev/done/`.

---

## ✅ Completion report

- **Дата:** 2026-09-05
- **Гілка / комміт:** `main` @ `a4073d9` (база); власний код цієї таски — **частково закомічений окремо**, дивись «Контрактні зміни» нижче (та ж ситуація, що task-59 вже документував для цього самого файлу)
- **Статус:** done

### Що зроблено

Обидві дірки з §0 закриті й перевірені живо на реальних клієнтах:

- **CSS-ассети (§1).** `discover_html_assets` тепер бачить `url(...)` всередині інлайн `<style>` блоків; `_capture_assets_for_page` розгортає завантажений CSS-файл у чергу з одним рівнем (фон/шрифт з тіла CSS потрапляють у той самий пайплайн `_check_or_fetch_asset`, той самий бюджет, та сама ізоляція клітинки). CLI `archive-backfill-assets [--client-id X | --all] [--dry-run]` заднім числом лікує вже захоплені знімки.
- **Iframe (§2).** `<iframe>`/`<frame>` стешаться цілком і повертаються байт-у-байт (живий хост), окрім аналітичних маячків (GTM/GA/Meta pixel/doubleclick) — ті лишаються переписаними й падають у «Not archived». CSP `frame-src` — єдиний послаблений напрямок (`https:` замість `'self'`), і те й інше — за фіче-флагом.
- **Вимірюваність (§1.4/§2.3).** Meta-панель показує `N missing` тоном warning і `Live embeds: N (хости)` з підписом-застереженням; хаб отримав плитку **Asset coverage** (`captured / referenced`, живо: 14 428 / 14 430 по всьому портфелю на момент здачі).

### Як зроблено (ключові рішення)

- **Черга з одним рівнем розгортання, не рекурсія** ([capture.py](app/webarchive/capture.py)) — `_capture_assets_for_page` перероблено на `deque`: початкові посилання з HTML, і коли обробляється ассет `kind=="css"` з успішно збереженим блобом, його тіло читається з локального сховища (дешево, диск, не мережа) і прогонюється через `discover_css_assets`; знайдене додається в чергу, дедуп по `normalize_url`. `css_expanded`-множина гарантує розгортання щонайбільше раз на CSS-файл навіть якщо два різні href-рядки (напр. з різним `?utm_source=`) нормалізуються в той самий актив — окремий юніт-тест (`test_css_body_is_expanded_at_most_once_per_page_even_with_aliased_refs`) підтверджує це шпигунським підрахунком викликів `discover_css_assets`.
- **Бекфіл сканує напряму збережені байти, не через `ArchiveSnapshotAsset`** ([capture.py:backfill_css_assets](app/webarchive/capture.py)) — старі знімки НЕ мають зв'язку snapshot→asset для CSS-only картинок (їх ніколи не було знайдено), тож бекфіл читає HTML/CSS блоби напряму (`storage.read_blob` + `discover_html_assets`/`discover_css_assets`) і збирає посилання, яких немає в `archive_assets` за жодним `url_key`. Завдяки fallback-на-найранішу-версію в `resolve_asset_version` ([web_archive.py:407](app/db/repositories/web_archive.py#L407)) один прогін лікує ВСІ старі знімки заднім числом — не треба ітерувати по датах.
- **`ArchiveAsset` — глобальний реєстр без `client_id`**, тож «--client-id X» для бекфілу скопований через `ArchiveSnapshot.client_id` (знімки) і join `ArchiveSnapshotAsset`→`ArchiveSnapshot` (CSS-файли, які клієнт реально використовує) — нові репозиторні методи `all_snapshots`, `css_assets_for_client`, `all_css_assets`, `asset_coverage_totals`.
- **Iframe: стеш цілого тега тим самим плейсхолдер-механізмом, що вже захищає `<script>`/`<style>`** ([rewrite.py](app/webarchive/rewrite.py)) — на відміну від script/style (де тіло стешиться, а атрибути тега — ні), тут стешиться ВЕСЬ тег, і рерайту не відбувається взагалі, крім одного винятку: якщо `src` резолвиться в хост зі списку аналітичних маячків (`is_analytics_iframe_src`, [urls.py](app/webarchive/urls.py)) — тоді тег НЕ стешиться і падає в звичайний рерайт-пайплайн (→ `/archive/r/...` → 404, мовчки).
- **`shim.js`: прибрано `HTMLIFrameElement` з патчених сеттерів + виняток у `setAttribute`** ([archive_replay.py](app/api/archive_replay.py)) — це виявилося критичним не для статичного `src`, а для **лінивого завантаження**: жива EFFYS-мапа насправді сидить у `src="about:blank" data-lazy-src="https://www.google.com/maps/embed?..."` (WP Rocket lazy-load, IntersectionObserver), і саме JS-промоушн `data-lazy-src → src` мусить іти повз наш рерайт. Перевірено живим Playwright-скролом у сендбоксі — мапа підвантажується (3/3 запити 200) лише ПІСЛЯ скролу в зону видимості, точнісінько як на живому сайті.
- **Фіче-флаги, не хардкод** ([config.py](app/core/config.py)) — `archive_live_iframes` (кілл-свіч, глушить і рерайт, і CSP `frame-src` разом) та `archive_iframe_analytics_hosts` (список, не заморожений у коді) — обидва додані свідомо, щоб оператор міг вимкнути живі ембеди чи додати новий маячок без деплою коду.
- **`live_iframe_hosts()` — окрема, незалежна від рендеру функція** ([rewrite.py](app/webarchive/rewrite.py)) — Meta-панелі не потрібне нове поле в БД (міграції нема): вона просто читає збережений HTML знімка вдруге при відкритті деталей і рахує хости так само, як порахував би `rewrite_html`.

### Контрактні зміни (Contract changes)

Адитивні, без версіонування:

- `ArchiveSnapshotDetailDTO` — нове поле `live_iframe_hosts: list[str]`.
- `ArchiveOverviewDTO` — нові поля `assets_referenced: int`, `assets_captured: int`.
- `tests/unit/api/data/openapi_snapshot.json` — перегенеровано, diff обмежений рівно цими трьома полями (перевірено вручну).
- `Settings` — 2 нові ключі: `archive_live_iframes: bool = True`, `archive_iframe_analytics_hosts: list[str]`.
- CLI: нова команда `archive-backfill-assets [--client-id X | --all] [--dry-run]`.

**Важливо для наступного агента:** `app/cli.py` і `app/core/config.py` **не закомічені цією таскою**, хоча в них є код task-60 (перевірено `git diff` рядок-у-рядок): обидва файли вже мали незакомічені зміни ІНШОЇ паралельної сесії (task-58: `monitor_client_workers`/`monitor_page_workers`/`monitor_host_concurrency`/`monitor_browser_*`/`monitor_block_ratio_abort` у config.py; `--ignore-cloud` і переписаний `monitor-prune` у cli.py) ще ДО старту цієї таски — та сама ситуація, яку task-59 вже задокументував для `app/cli.py` у своєму власному completion report, і яка тепер явно поширюється й на `app/core/config.py` (він мав бути в тому самому списку, але не був). Код task-60 у цих двох файлах **робочий і потрібен** (без нього `archive-backfill-assets` не існує як команда, а фіче-флаги — як налаштування), просто фізично лежить у файлі, який комітить інша сесія. Нічого не втрачено й не зіпсовано — коли task-58 закомітить свою частину, код цієї таски приїде в тому ж дифі як «вже був у робочому дереві».

### Як перевірити (verification)

```bash
docker compose exec api poetry run ruff check app/webarchive app/api/archive.py app/api/archive_replay.py app/schemas/web_archive.py app/db/repositories/web_archive.py app/cli.py app/core/config.py tests/unit/webarchive tests/unit/api/test_archive_api.py tests/unit/api/test_archive_replay_api.py
docker compose exec api poetry run mypy app/webarchive app/api/archive.py app/api/archive_replay.py app/schemas/web_archive.py app/db/repositories/web_archive.py app/cli.py app/core/config.py
docker compose exec api poetry run pytest tests/unit/webarchive tests/unit/api/test_archive_api.py tests/unit/api/test_archive_replay_api.py -q   # 118 passed
docker compose exec api poetry run pytest -q   # повний набір: лише 2 фейли, обидва передують цій тасці й до неї не стосуються
                                                # (tests/contract/test_aiseotools_signatures.py — дрейф сигнатур сусіднього
                                                #  репо AISeoTools; tests/unit/api/test_tech_audit.py::...measured_open_errors —
                                                #  дата-залежний поріг staleness у dashboard/tech_audit, не в owned paths)

# Живо, на реальних даних (2026-09-05):
docker compose exec api poetry run yovth-report archive-backfill-assets --client-id <HWELL> --dry-run   # 50 missing (44 img + 6 font)
docker compose exec api poetry run yovth-report archive-backfill-assets --client-id <HWELL>              # fetched: 50, failed: 0
curl -s -D - -o /dev/null "http://localhost:6524/archive/r/20260904133527/https://hearwellservices.com/wp-content/uploads/2025/02/image-1.webp"
# → HTTP/1.1 200, content-type: image/webp (був 404 до бекфілу — саме URL з репорту користувача)
```

Playwright (headless Chromium, `/tmp/yovth-e2e/task60-verify.cjs`, реальний бек проти localhost:6524):
- **EFFYS**, снепшот `585f612d-...` (`https://effyscafe.com/`): мапа в `src="about:blank" data-lazy-src="...maps/embed..."` (WP Rocket lazy-load) — після скролу в зону видимості `src` стає живим `https://www.google.com/maps/embed?...` (НЕ через `/archive/r/`), 3/3 запити до google.com — `200`. Meta-панель показує `Live embeds: 1 (www.google.com)`.
- **GOREPORT**, знімок `20260904133527`: GTM-скрипт (`gtm.js?id=GTM-55ZPWNH`) коректно переписаний у `/archive/r/.../googletagmanager.com/...` (і там 404) — **жоден** запит з реальним hostname `googletagmanager.com` не пішов у мережу (перевірено парсингом `new URL(...).hostname`, а не підрядком — перша версія тесту хибно спрацьовувала на власний переписаний шлях).
- Візуально (`task60-hub.png`, `task60-meta.png`): хабова плитка «Asset coverage» і рядок «Live embeds» у Meta-панелі рендеряться коректно в темній темі, без консольних помилок окрім вже задокументованого в task-59 Cloudflare RUM CORS-гепу.

### Тести

18 нових юніт-тестів у `tests/unit/webarchive/` (+2 нові файли: `test_assets.py`, `test_web_archive_repo.py`) і `tests/unit/api/`:
- `test_urls.py` (+5): `is_analytics_iframe_src` — GTM/www-варіанти, `facebook.com/tr` (шлях, не весь домен), справжні ембеди НЕ фолсяться, кастомний host-set із `Settings`.
- `test_assets.py` (новий, 6): інлайн `<style>` → фон/шрифт знаходяться, резолв відносних URL, порожні блоки, дедуп при колізії з `<img>`, `discover_css_assets` не зачеплено.
- `test_capture.py` (+5): CSS-файл → фон+шрифт у черзі; розгортання CSS рівно раз на сторінку навіть з аліас-посиланнями (шпигун на `discover_css_assets`); бекфіл dry-run/fetch/scope-по-клієнту.
- `test_web_archive_repo.py` (новий, 3): `all_snapshots` по клієнту, `css_assets_for_client` не бачить чужого CSS, `asset_coverage_totals`.
- `test_rewrite.py` (+9): live-iframe байт-у-байт, `<frame>` теж живий, `about:blank`-плейсхолдер не займаний, аналітичний iframe і далі переписується, кілл-свіч `live_iframes=False` повертає стару поведінку, кастомний analytics-host-set, `data-lazy-src` не займаний, `live_iframe_hosts()` — дедуп/сортування/порожньо-при-вимкненому-прапорці.
- `test_archive_replay_api.py` (+4): CSP містить `frame-src https:`, живий iframe не переписаний на реальному replay-роуті, аналітичний iframe переписаний і там же блокується, `shim.js` не містить `HTMLIFrameElement` у списку патчених сеттерів.
- `test_archive_api.py` (+2): `assets_referenced`/`assets_captured` в overview, `live_iframe_hosts` у деталях знімка (GTM виключено зі списку).

### Follow-ups / борг

- `app/cli.py`/`app/core/config.py` commit — чекає, поки паралельна task-58-сесія закомітить свою частину цих файлів (див. «Контрактні зміни» вище).
- Повний портфельний `archive-backfill-assets --all` — свідомо НЕ запущений цією таскою (ризик §4: навантаження, слот 13:30/19:30). HWELL і EFFYS пролікувані живо як доказ; решту клієнтів варто прогнати оператору вручну, поза піковими слотами.
- `assets_missing` на Meta-панелі рахує тільки те, що БУЛО пов'язано зі знімком на момент захоплення (`ArchiveSnapshotAsset`) — для знімків, захоплених ДО цього фіксу, число залишиться `0` навіть якщо портфельний лічильник «Asset coverage» на хабі показує реальну діру; це чесно (архів не вигадує зв'язків заднім числом), але не інтуїтивно з першого погляду — вартий одного речення в майбутньому onboarding/tooltip, не міграції.
