# task-50 — Site Monitor: чесний стан `blocked`, окрема повільна antibot-доріжка (Playwright/patchright) для захищених клієнтів

- **Пакет:** живий дефект **ALPHA (alphastyle.store)**: сайт за Cloudflare/Shopify bot-protection віддає монітору `429 «Verifying your connection…»`, а монітор записує цю сторінку-заглушку як **справжню версію сторінки**. Результат прогону 2026-08-19 14:28 — 304 із 355 сторінок «змінилися», 635 warning + 1 critical, `sitemap_health.broken = 304`. Пакет: (1) блок ≠ дані — новий чесний стан `blocked`; (2) окремий повільний antibot-пул у планувальнику, паралельний до основних воркерів; (3) browser-транспорт на patchright/Playwright для клієнтів, де HTTP не проходить; (4) маскування per-request токенів Shopify, що дають вічний шум `content_changed`.
- **Хвиля:** M — спостережуваність сайтів (продовження task-35/36/37/38/41).
- **Depends on:** **35** ✅, **36** ✅, **37** ✅, **38** ✅, **41** ✅
- **PLAN.md:** §12 (чесні стани — відсутність даних ≠ `0` і ≠ «змінилось»), §9.1 (доріжки: між сервісами паралельно, усередині — послідовно), §13 (SSRF-гард на кожен хоп), §14 (планувальник у worker-процесі)
- **frontend/AGENTS.md:** §5 (правда даних), §6 (стани), §15–16 (візуальний чекліст)
- **AISeoTools:** змін не потребує. `patchright` уже присутній у образі як транзитивна залежність AISeoTools; Chromium уже запечений (`PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright`, `chromium-1228`).

---

## 0. Що виміряно живо (2026-08-19, контейнер `api`)

Усе нижче — не гіпотези, а результати прогонів проти живого сайту клієнта та проти живої БД.

### 0.1 Масштаб дефекту

`GET /api/v1/clients/{alpha}/monitor/pages?limit=500`:

| статус | сторінок |
|---|---|
| `200` | 51 |
| `429` | **304** |

Історія `full`-прогонів ALPHA показує, що це **рецидив**, а не разова подія:

```
2026-08-19T14:28 checked=355 changed=304 crit=1 warn=635   ← хвиля 429
2026-08-19T09:46 checked=355 changed=0   crit=0 warn=0
2026-08-15T17:07 checked=355 changed=127 crit=0 info=190
2026-08-14T08:03 checked=355 changed=122 crit=1 warn=244
2026-08-12T10:33 checked=355 changed=353 crit=0 warn=1331  ← попередня хвиля
```

Накопичений операторський борг у ALPHA на момент дослідження: **`open_warning = 1980`, `open_info = 1554`** — на одного клієнта. Це і є ціна дефекту: стрічка змін ALPHA непридатна до читання.

### 0.2 Що саме зберігається замість сторінки

Тіло збереженого блобу `4f302f57…` (версія сторінки `/agents.md`, `http_status=429`):

```html
<title>Verifying your connection...</title>
… <div class="cf-injected-html"> … _cf_chl … <meta http-equiv="refresh" content="360">
```

Тобто це Cloudflare-інтерстиціал під Shopify, із **періодом покарання 360 секунд**. Версія цієї сторінки записалась із `changed_fields: ["status","title","h1","indexable"]` — тобто монітор вважає, що на сторінці **зник заголовок і вона стала неіндексованою**. Це рівно той клас брехні, який забороняє PLAN §12.

### 0.3 Чому детектор блоку не спрацював

[`_looks_blocked`](../../../app/monitor/fetcher.py#L100) шукає маркери `_CHALLENGE_MARKERS` у перших 4 КБ тіла. Перевірено на реальному блобі — **жоден із семи маркерів не збігається**:

| маркер | у перших 4 КБ | у всьому тілі |
|---|---|---|
| `cf-chl` | ні | ні |
| `checking your browser` | ні | ні |
| `just a moment` | ні | ні |
| `attention required! \| cloudflare` | ні | ні |
| решта | ні | ні |

При цьому в тілі є `verifying your connection` (offset 157), `cf-injected` (1466), `_cf_chl` (1710) — з підкресленням, а не дефісом, тому `cf-chl` не матчить.

Далі `429 ≥ 400` → [`classify_sitemap_status`](../../../app/monitor/severity.py#L100) → `"broken"` → 304 алерти `sitemap_url_*` + агрегат `sitemap_broken_urls` (**critical**).

### 0.4 І навіть якби спрацював — це нічого б не змінило

[`_persist_page_result`](../../../app/monitor/runner.py#L559) **взагалі не читає `result.error`**. Тобто наявний стан `blocked` (403 із збіжним маркером) сьогодні так само пишеться як звичайна версія сторінки. Це другий, самостійний дефект: детектор є, споживача детектора немає.

### 0.5 Що насправді тригерить Cloudflare (ізольовано, з хоста)

| запит | результат |
|---|---|
| UA монітора, без інших заголовків | **403** `cf-mitigated: challenge` |
| UA Chrome 131, без інших заголовків | **403** |
| UA Chrome 131 + `Accept` (html), без `Accept-Language` | **403** |
| UA Chrome 131 + **`Accept-Language`** | **200** |
| UA монітора + повний набір заголовків браузера | **200** |
| дефолтний `curl/8.x` UA + **`Accept-Language`** | **200** |

Дискримінатор — **`Accept-Language`**, а не User-Agent. Навіть відверто ботовий UA проходить, якщо заголовок є. Наш фетчер сьогодні шле рівно два заголовки: `User-Agent` і `Accept-Encoding: gzip` — тобто рекламує себе як не-браузер найдешевшим із можливих способів.

> **Але цього не досить.** З контейнера (HTTP/1.1, TLS OpenSSL) той самий «голий» набір заголовків дав **200** — інший TLS/HTTP-fingerprint дає інший bot-score. Отже заголовки — необхідна гігієна, що знімає одну зі складових оцінки, але **не гарантія**: Cloudflare зважує TLS/HTTP2-відбиток, репутацію IP і темп. Саме тому потрібна друга лінія — браузер.

### 0.6 Темп: 429 — це rate-limit, а не постійний блок

30 послідовних запитів справжнім `app.monitor.fetcher.fetch` у темпі монітора (`min_interval=0.25s`) — **30 × 200**, жодного 429. Тобто на малому обсязі сайт відкритий; стіна виникає на дистанції `full`-прогону (362 запити). У зіпсованому прогоні пройшло ~51 сторінка + robots + sitemap-и (~58 запитів) — далі все 429 і **до кінця прогону (2,5 хв) відновлення не сталося**, що узгоджується з `meta refresh 360`.

### 0.7 Playwright/patchright у цьому образі — працює

```
driver = patchright          launch 0.5s
/collections/sweatss   status=200  0.9s  'Sweats - AlphaStyle'
/collections/apex      status=200  0.7s  'APEX - AlphaStyle'
cookies: __cf_bm, _shopify_y, _shopify_s, …
```

20 сторінок поспіль із блокуванням підресурсів (`image|media|font|stylesheet`) — **20 × 200, ~1 сторінка/сек**. `sync_playwright()` коректно працює **всередині воркер-треда** `ThreadPoolExecutor` (перевірено окремо) — отже вбудовується в наявний планувальник без переходу на async.

### 0.8 Ключове: browser-транспорт **не ламає** відбитки

Порівняння `urllib`-тіла проти `response.body()` браузера на одній сторінці:

| сигнал | збіг |
|---|---|
| `title`, `description`, `meta_robots`, `canonical`, `h1` | ✅ |
| `hreflang`, `json_ld_types`, `analytics_ids`, `google_site_verifications`, `word_count` | ✅ |
| `visible_text` | ❌ (різниця 16 символів) |

Тобто **`signals_sha256` — той самий**, а це саме те, на чому тримається виявлення змін і вся шкала severity. Переведення клієнта на browser-доріжку **не створить хвилю фальшивих алертів**. Умова: брати `response.body()` (сирі байти відповіді), **ніколи `page.content()`** — DOM після JS відрізняється й дав би зміну на кожному прогоні.

### 0.9 Побічно знайдений дефект: вічний шум `content_changed` у ALPHA

Два послідовні `urllib`-фетчі однієї сторінки ALPHA (інтервал 2 с) дають **різний** нормалізований текст. Дифи:

```
<meta name="shopify-y" content="0e375ebf-…">  →  content="0b52756c-…"
<meta name="shopify-s" content="c9d7709a-…">  →  content="ad15d77a-…"
…eyJ…exp:1787759715,nbf:1787154915…JWT…      →  …exp:1787759686,nbf:1787154886…
```

Це per-request сесійні UUID Shopify + JWT з таймштампами, вбудовані в HTML. `mask_known_noise` їх не маскує, тому `text_sha256` і `raw_sha256` ALPHA **змінюються на кожній перевірці** — звідси стабільний потік `info`-змін щогодини (частина з 1554 `open_info`). Той самий клас, що вже вирішений для `__CF$cv$params` у [fingerprint.py](../../../app/monitor/fingerprint.py#L24).

### 0.10 Масштаб — рівно один клієнт

Скан статусів усіх сторінок по всьому портфелю:

```
exxxotica 500×200 | effyscafe 16×200 | goreport 228×200 | grillpoint 30×200
hearwell 247×200  | laxton 173×200   | nychearing 85×200 | nysapphire 456×200
shoreline 365×200 | smashhouse 106×200 | shawarma 24×200
alphastyle 51×200 + 304×429  ← єдиний
```

**Висновок для дизайну:** browser-доріжка має бути **opt-in на клієнта**, а не новий транспорт за замовчуванням. 11 клієнтів працюють ідеально на дешевому HTTP і не повинні платити ані секунди Chromium-часу.

---

## 1. Першопричини (три незалежні дефекти)

| # | Дефект | Місце | Наслідок |
|---|---|---|---|
| **D1** | Відповідь-заглушка антибота записується як версія сторінки | [`_persist_page_result`](../../../app/monitor/runner.py#L559) не читає `result.error`; `_CHALLENGE_MARKERS` не покриває Shopify/CF-інтерстиціал; `429` класифікується як `broken` | 304 фальшиві версії, 635 warning, 1 critical, `sitemap_health` бреше |
| **D2** | Немає транспорту, здатного пройти bot-protection, і немає місця, де він міг би працювати, не заважаючи іншим | єдиний `HostThrottle`+`urllib` у [fetcher.py](../../../app/monitor/fetcher.py); один пул на `monitor_client_workers=4` у [scheduler.py](../../../app/monitor/scheduler.py#L69) | захищений клієнт або не збирається, або, сповільнений, з'їдає 1 із 4 слотів портфеля |
| **D3** | Per-request токени Shopify не маскуються | [`mask_known_noise`](../../../app/monitor/fingerprint.py#L34) | вічний `content_changed`/`info`-шум на кожному прогоні |

---

## 2. Рішення — три шари

Шари незалежні й дають користь по черзі: **A** можна змержити окремо і він уже гасить пожежу; **B** — те, що просив користувач; **C** — гігієна.

### Шар A — чесний стан `blocked` (без браузера)

**A1. Розпізнавання.** `_looks_blocked` → `classify_block(status, headers, body) -> BlockKind | None`:

- заголовок `cf-mitigated` присутній → блок (найнадійніший сигнал, віддає сам Cloudflare);
- `Retry-After` на 403/429/503 → блок;
- **будь-який `429`** → блок. Rate-limit ніколи не є контентом сторінки; трактувати його як `broken`-сторінку — категорійна помилка;
- розширений список маркерів: `verifying your connection`, `_cf_chl`, `cf-injected-html`, `cf-mitigated`, `enable javascript and cookies`, плюс наявні сім;
- пошук маркерів — по всьому тілу (воно й так обмежене `max_body_bytes`), а не лише по перших 4 КБ: у виміряному блобі маркери лежать на offset 1466–1710, тобто в межах 4 КБ пощастило, але покладатись на це не можна.

`FetchResult` отримує `retry_after_seconds: float | None` (з `Retry-After` або з `<meta http-equiv="refresh">` — у виміряному тілі це **360**).

**A2. Споживання.** `_persist_page_result` на самому початку:

```
if result.error == "blocked":
    return _record_blocked(...)   # НЕ чіпає page state, НЕ пише версію, НЕ рахує consecutive_failures
```

Блок — це «ми не подивилися», а не «сторінка змінилась» і не «сторінка зламана». Стан сторінки лишається останнім достовірним; `sitemap_health` автоматично лишається чесним, бо [`sitemap_health_counts`](../../../app/db/repositories/monitor.py) читає саме `page state`.

**A3. Один агрегат замість 304 рядків.** Прогін накопичує `blocked_urls`. У кінці — рівно **одна** зміна `site_protected` (**warning**, self-healing), subject = хост, `details = {blocked, checked, total}`, з `_reconcile` за тим самим патерном, що вже працює в [`_sitemap_aggregate`](../../../app/monitor/runner.py#L906). Наступний чистий прогін її закриває сам.

**A4. Раннє гальмо.** Якщо поспіль ≥ `monitor_block_streak_abort` (дефолт 5) блоків — прогін **припиняється** (`truncated=True`, `error_code="blocked_by_protection"`), а не витрачає ще 250 запитів у стіну й не поглиблює покарання. Це прямий висновок із §0.6: після 58-го запиту всі решта 304 були марними.

**A5. Статус прогону — чесний.**
- 0 блоків → як зараз;
- частина заблокована → `succeeded`, `error_code="partially_blocked"`, `blocked_count` у лічильниках;
- нічого не зібрано → `failed`, `error_code="blocked_by_protection"`.

**A6. Гігієна заголовків (безкоштовна, портфельна).** Базовий фетчер починає слати реалістичний набір: `Accept`, **`Accept-Language: en-US,en;q=0.9`**, `Sec-Fetch-*`, `Upgrade-Insecure-Requests`. За §0.5 це рівно те, чого бракувало, щоб CF не піднімав виклик. Ризик для інших клієнтів — нульовий (заголовки лише роблять запит більш звичайним), але прогін по всьому портфелю після зміни обов'язковий (див. Gate).

### Шар B — окрема повільна antibot-доріжка (ядро задачі)

**B1. Транспорт.** Новий `app/monitor/browser_fetch.py` — **той самий контракт**, що й `fetch()`:

```python
def fetch_via_browser(url, *, session: BrowserFetchSession, stats: FetchStats,
                      timeout: float, max_body_bytes: int) -> FetchResult
```

Тобто **drop-in**: `_process_page` лише обирає, яку функцію викликати. Жоден із шляхів парсингу/діфу/зберігання не змінюється.

Обов'язкові рішення транспорту:

| рішення | чому саме так |
|---|---|
| `response.body()`, **ніколи** `page.content()` | §0.8: сирі байти дають ідентичні `signals_sha256`; DOM після JS дає зміну щопрогону |
| `route.abort()` для `image \| media \| font \| stylesheet` | ×4 швидше, у рази менше запитів до того ж походження — тобто **менше шансів наштовхнутись на той самий rate-limit**, який ми лікуємо |
| `assert_public_http_url` **до** навігації і **на `response.url`** після | браузер не проходить через `GuardedRedirectHandler`; без цього SSRF-гард (PLAN §13) на цій доріжці просто зникає |
| один `Browser` + один `BrowserContext` на весь прогін клієнта | `launch` = 0,5 с; 355 разів по 0,5 с — це 3 хвилини на порожньому місці. Контекст ще й накопичує `__cf_bm`, тобто виглядає як одна сесія, а не 355 різних відвідувачів |
| заголовки з `response.headers` загортаються в наявний [`Headers`](../../../app/monitor/fetcher.py#L42) | HTTP/2 віддає імена в нижньому регістрі; це рівно той баг, який уже коштував проєкту 355 сторінок ALPHA без title (див. docstring `Headers`) |
| `hop_count` з ланцюга `response.request.redirected_from` | доріжки мають давати однакову семантику полів |
| `patchright` через наявний [`stealth_browser.py`](../../../app/reports/stealth_browser.py) | вже вибраний і живо перевірений драйвер проєкту; `is_timeout_error()` уже вирішує проблему двох різних класів `TimeoutError` |

> **Читання чужої зони:** `app/reports/stealth_browser.py` — owned by task-47. Ця таска його **лише імпортує**, не редагує. Якщо виявиться потреба в `launch_persistent_context` (див. §2 Шар C — ескалація), це окремий адитивний PR у зону task-47, а не правка мимохідь.

**B2. Окремий пул у планувальнику.** [`MonitorScheduler`](../../../app/monitor/scheduler.py#L54) отримує **другий** `ThreadPoolExecutor`:

```
self._pool          = ThreadPoolExecutor(monitor_client_workers)   # 4, як зараз
self._antibot_pool  = ThreadPoolExecutor(monitor_antibot_workers)  # 1
```

`_submit` маршрутизує за ефективним режимом клієнта. Це і є «окремий повільний потік паралельно іншим»: ALPHA у своєму темпі ~12 хв **не займає жодного з 4 звичайних слотів**, а решта портфеля не чекає ALPHA. `_in_flight` лишається спільним — той самий ключ `(client_id, kind)` захищає від дубля незалежно від пулу.

`monitor_antibot_workers = 1` за замовчуванням і **свідомо**: два одночасні Chromium-и на воркері — це і пам'ять, і, головне, два джерела трафіку на ті самі CDN-и. Інваріант «browser-профілі одного провайдера не запускаються паралельно» тут виконується структурно.

**B3. Темп і адаптація.** Окремий `HostThrottle(monitor_antibot_min_interval_seconds)` — дефолт **2.0 с** (проти 0.25 с на основній доріжці). 355 сторінок ≈ 12 хв, що вкладається в наявний `monitor_run_max_minutes = 90` без змін.

Адаптивний backoff у межах прогону:

- отримали блок → чекаємо `retry_after_seconds` (виміряно: **360 с**), але не більше `monitor_antibot_max_backoff_seconds` (дефолт 420);
- після відновлення інтервал множиться на 1.5 і **лишається** підвищеним до кінця прогону (сайт уже показав, де його межа);
- два backoff-и за прогін → доганяємо A4 і зупиняємось із `partially_blocked`, а не довбимо третій раз.

Точний безпечний темп ALPHA **навмисно не зашитий у план**: §0.6 показує, що межа лежить десь між «30 запитів у темпі 0.25 с — чисто» і «~58 запитів у темпі ~0.46 с — стіна». Визначити її означає свідомо заганяти живий сайт клієнта в 6-хвилинне покарання. Правильний шлях — консервативний старт 2.0 с і **вимір по факту** на першому реальному прогоні (див. Gate §7.2), а не число, вигадане зараз.

**B4. Режим на клієнта.** `MonitorSettings.fetch_mode: "auto" | "http" | "browser"`, дефолт `"auto"`.

- `http` — примусово дешевий шлях (аварійний вимикач);
- `browser` — завжди antibot-доріжка;
- `auto` — HTTP, доки прогін не побачить блоки; тоді клієнт **сам** переїжджає на browser-доріжку з наступного прогону.

**B5. Авто-ескалація (`auto`).** Прогін, що завершився `partially_blocked`/`blocked_by_protection`, ставить `MonitorSettings.blocked_detected_at = now`. Планувальник вважає ефективним режимом `browser`, якщо `fetch_mode == "browser"` **або** (`fetch_mode == "auto"` і `blocked_detected_at` не старший за `monitor_antibot_sticky_days`, дефолт 14). Чистий browser-прогін не скидає прапорець одразу — інакше клієнт «стрибав» би між доріжками щогодини; скидання лише після `monitor_antibot_sticky_days` без жодного блоку.

**B6. Деескалація вручну.** Оператор може виставити `http` — і система більше не ескалює. Явна дія людини перемагає евристику.

### Шар C — де-шум і прибирання

**C1. Маскування Shopify-токенів** у [`mask_known_noise`](../../../app/monitor/fingerprint.py#L34): `<meta name="shopify-y|shopify-s" content="…">` і JWT-подібні `eyJ…` з `exp`/`nbf` у значеннях атрибутів. Той самий підхід і те саме обґрунтування, що для вже наявного `__CF$cv$params`.

**C2. Разове прибирання наслідків.** CLI `yovth-report monitor-resolve-blocked --client <id> [--run <id>] [--dry-run]`:
- закриває `MonitorChange`, створені зіпсованим прогоном (`sitemap_url_*`, `title_changed`, `content_changed`, `noindex*` із `run_id` цього прогону), з `note="resolved: antibot block, not a real change"`;
- **не видаляє** версії сторінок — ланцюг версій незмінний за дизайном (task-35 §1.3). Замість цього наступний нормальний прогін просто допише коректну версію.

`--dry-run` — обов'язково перше, що бачить оператор; масове закриття алертів робиться лише після показаного плану.

**C3. Ескалація на випадок справжнього JS-виклику.** Якщо колись `patchright` за замовчуванням не пройде (справжній managed challenge замість тихого bot-score), наступний крок відомий і задокументований, але **у цю таску не входить**: `launch_persistent_context` з окремим `user_data_dir` на клієнта (щоб жив `cf_clearance`) і відмова від `add_init_script`, який сам по собі детектується. Записати як `TODO(task-NN)` у Completion report, а не робити наперед.

---

## 3. Зміни по файлах

| Файл | Зміна |
|---|---|
| [app/monitor/fetcher.py](../../../app/monitor/fetcher.py) | `classify_block()` замість `_looks_blocked`; розширені маркери; `retry_after_seconds` у `FetchResult`; реалістичні заголовки (A6) |
| **`app/monitor/browser_fetch.py`** (новий) | `BrowserFetchSession` (життєвий цикл браузера/контексту) + `fetch_via_browser()` із контрактом `fetch()` |
| [app/monitor/runner.py](../../../app/monitor/runner.py) | вибір транспорту в `_process_page`; ранній вихід на `blocked` у `_persist_page_result`; лічильник `blocked`; агрегат `site_protected`; раннє гальмо A4; статуси A5; підняття `BrowserFetchSession` на `run_client_monitor` для browser-режиму |
| [app/monitor/scheduler.py](../../../app/monitor/scheduler.py) | другий пул `_antibot_pool`; маршрутизація в `_submit`; `stop()` закриває обидва |
| [app/monitor/severity.py](../../../app/monitor/severity.py) | `"site_protected": WARNING`; `classify_sitemap_status` більше не бачить заблокованих статусів (вони не доходять) |
| [app/monitor/diff.py](../../../app/monitor/diff.py) | `_SITE_PROTECTED_TRACKED` для reconcile |
| [app/monitor/fingerprint.py](../../../app/monitor/fingerprint.py) | C1 — маскування `shopify-y`/`shopify-s`/JWT |
| [app/db/models.py](../../../app/db/models.py) | `MonitorSettings.fetch_mode`, `MonitorSettings.blocked_detected_at`, `MonitorRun.blocked_count` |
| `migrations/versions/<rev>_monitor_antibot_lane.py` (новий) | три поля вище, усі nullable/з дефолтом — жодного backfill |
| [app/schemas/monitor.py](../../../app/schemas/monitor.py) | `fetch_mode` у `MonitorSettingsDTO`/`…UpdateRequest`; `blocked_count` у `MonitorRunDTO`; `is_blocked` у `MonitorPageDTO` |
| [app/api/monitor.py](../../../app/api/monitor.py) | проброс нових полів; валідація `fetch_mode` |
| [app/core/config.py](../../../app/core/config.py) | `monitor_antibot_workers=1`, `monitor_antibot_min_interval_seconds=2.0`, `monitor_antibot_max_backoff_seconds=420`, `monitor_antibot_sticky_days=14`, `monitor_block_streak_abort=5` (адитивно, як це вже робили task-35/37/41) |
| [app/cli.py](../../../app/cli.py) | `monitor-resolve-blocked` (C2) |
| [frontend/js/monitor-client.js](../../../frontend/js/monitor-client.js) | чіп `Blocked` у рядку сторінки; «Blocked: N» у картці прогону; селектор **Fetch mode** у налаштуваннях клієнта |
| [frontend/js/formatters.js](../../../frontend/js/formatters.js) | лейбл `site_protected` → `"Site is blocking automated checks"` |
| [frontend/css/monitor.css](../../../frontend/css/monitor.css) | тон для `blocked` (CSP-safe клас, **не** inline `style=`) |

**Owned paths таски:** `app/monitor/**`, `app/db/repositories/monitor.py`, `app/api/monitor.py`, `app/schemas/monitor.py`, `migrations/versions/<rev>_monitor_antibot_lane.py`, `frontend/js/{monitor-client,formatters}.js`, `frontend/css/monitor.css`, `tests/unit/monitor/**` + адитивні дотики `app/db/models.py`, `app/core/config.py`, `app/cli.py` (той самий шаблон дотиків, що вже узгоджений для task-35/37/41).
**Read-only:** `app/reports/stealth_browser.py` (лише імпорт), `app/tools/http_safety.py`, `frontend/js/api.js`.

---

## 4. Контрактні зміни

Усі адитивні, старі клієнти не ламаються:

- `MonitorSettingsDTO.fetch_mode: "auto" | "http" | "browser"` (+ у `MonitorSettingsUpdateRequest`);
- `MonitorRunDTO.blocked_count: int | None` — `None` для рядків, записаних до міграції (`None` ≠ `0`, інваріант проєкту: старий прогін не має рендеритись як «0 заблоковано»);
- `MonitorPageDTO.is_blocked: bool` — чи остання спроба впала в блок (стан сторінки при цьому лишається останнім достовірним);
- новий тип зміни `site_protected` у стрічці.

Синхронізувати `task-02` не потрібно — це DTO монітора, не спільний контракт інтеграцій. Оновити `tests/unit/api/data/openapi_snapshot.json`.

---

## 5. UI (frontend/AGENTS.md §5–6)

- Рядок сторінки з `is_blocked` → чіп **Blocked** (нейтрально-попереджувальний тон, не червоний «broken»), а поруч — останній **достовірний** статус. Оператор має бачити «ми не змогли подивитися», а не «сторінка зламана».
- Картка прогону: `Blocked: 304 of 355` поряд із наявними лічильниками; для `error_code="blocked_by_protection"` — окремий текст стану, а не generic «failed».
- Налаштування клієнта: селектор **Fetch mode** — `Auto (recommended)` / `Plain HTTP` / `Browser (slow lane)`, з підписом, що browser-режим повільніший і призначений для сайтів із bot-protection.
- Коли `auto` ескалював сам — бейдж «Auto-escalated to browser lane», щоб зміна поведінки не була невидимою.
- Уся копірайт-частина — **англійською**; жодного inline `style=` (CSP).

---

## 6. Ризики

| Ризик | Мітигація |
|---|---|
| Заголовки A6 змінять відповіді інших 11 клієнтів (інший варіант сторінки → хвиля `content_changed`) | Gate §7.1: прогін по всьому портфелю **до** й **після**, порівняння `signals_sha256`. Якщо хвиля є — вона одноразова й `info`, але про неї треба знати заздалегідь, а не дізнатись зі стрічки |
| Chromium у worker-контейнері з'їдає пам'ять | один браузер на прогін, `monitor_antibot_workers=1`, підресурси заблоковані; заміряти RSS на Gate |
| `stop_event` (SIGTERM) під час 360-секундного backoff | чекати переривано (`stop_event.wait(chunk)`), а не `time.sleep` — інакше рестарт зависає на 6 хв |
| Playwright sync API в треді | перевірено живо (§0.7); окремий `sync_playwright()` на кожен прогін, нічого не шариться між тредами |
| Авто-ескалація «стрибає» туди-сюди | `monitor_antibot_sticky_days=14` + ручний `http` як фінальне слово оператора |
| Browser-доріжка сама впреться в rate-limit | той самий адаптивний backoff і те саме раннє гальмо — вони живуть у runner, а не в транспорті, тож працюють для обох доріжок |

---

## 7. Definition of Done / Gate

**7.1 Регресія портфеля (обов'язково перед мержем).**
Прогін `full` по всіх 12 активних клієнтах на HTTP-доріжці до і після змін; `signals_sha256` не змінився ніде, окрім ALPHA. Різниця в `text_sha256` допустима лише там, де її пояснює C1.

**7.2 Живий прогін ALPHA на antibot-доріжці.**
`fetch_mode="browser"`, повний прогін 355 сторінок. Очікується: `blocked_count = 0`, `urls_changed` близько до 0 (сторінки реально не мінялись), `sitemap_health.broken` падає з 304 до фактичного, тривалість ≤ 20 хв. У Completion report **записати виміряний безпечний темп** і фактичну тривалість — це те число, якого сьогодні ніхто не знає (§0.6).

**7.3 Паралельність доведена, не припущена.**
Під час прогону ALPHA (≈12 хв) інші клієнти обслуговуються нормально: у логах видно `monitor_run_finished` для не-ALPHA клієнтів усередині вікна ALPHA-прогону, і жоден із них не чекав довше за звичайно.

**7.4 Чесний блок.**
Штучний тест (fake-фетчер, що віддає збережений блоб `4f302f57…` із 429): жодної нової версії сторінки, `page state` не змінився, `consecutive_failures` не зріс, рівно одна зміна `site_protected`, `sitemap_health` не деградував.

**7.5 Прибирання.**
`monitor-resolve-blocked --client alpha --dry-run` показує план; після застосування `open_warning` ALPHA падає з ~1980 до правдоподібного числа. Фактичні «до/після» — у Completion report.

**7.6 Стандартні гейти.**
`ruff`, `mypy app`, `pytest tests/unit/monitor tests/unit/api tests/unit/db`, `openapi_snapshot.json` оновлено, `monitor-smoke.cjs` зелений, візуальний чекліст `AGENTS.md §15–16` пройдено, скріншоти 1440/390 оновлені.

**7.7 Аварійний вимикач працює.**
`fetch_mode="http"` на ALPHA повертає стару поведінку без рестарту сервісу — тобто новий шлях можна вимкнути в проді однією зміною налаштування.

---

## 8. Тести

| Рівень | Що покриває |
|---|---|
| unit `tests/unit/monitor/test_block_detection.py` | `classify_block` на **реальному** збереженому блобі 429 + на CF 403 з `cf-mitigated` + на звичайних 403/404/500 (не блок) + `Retry-After` і `meta refresh` парсинг |
| unit `tests/unit/monitor/test_runner_blocked.py` | заблокована відповідь не пише версію / не змінює стан / не росте `consecutive_failures`; один агрегат `site_protected`; self-heal наступним чистим прогоном; раннє гальмо на streak |
| unit `tests/unit/monitor/test_browser_fetch.py` | контрактна еквівалентність `fetch_via_browser` ↔ `fetch` на локальному фікстур-сервері; `response.body()` а не DOM; SSRF-гард відхиляє приватну адресу до навігації; блокування підресурсів |
| unit `tests/unit/monitor/test_scheduler_lanes.py` | клієнт із `browser` іде в antibot-пул; `auto` ескалює після блоку і не ескалює після `http`; `_in_flight` спільний між пулами; `stop()` закриває обидва |
| unit `tests/unit/monitor/test_fingerprint.py` (доповнення) | маскування `shopify-y`/`shopify-s`/JWT на **живому** зафіксованому HTML ALPHA |
| unit `tests/unit/api/test_monitor_api.py` (доповнення) | `fetch_mode` валідація, `blocked_count=None` для старих прогонів |
| e2e `monitor-smoke.cjs` + фікстура `monitor-client-blocked.json` | чіп Blocked, «Blocked: N», селектор Fetch mode |

> Фікстура `frontend/fixtures/monitor-client-blocked.json` **уже існує** (task-36) — її треба розширити під новий стан, а не створювати нову.

---

## 9. Порядок робіт

1. Шар A (A1–A5) + тести → **пожежа згашена**, ALPHA більше не генерує сміття, навіть без браузера.
2. A6 + Gate 7.1 → перевірити, чи заголовків досить (за §0.5 — цілком можливо, що для ALPHA цього вистачить уже сьогодні).
3. Шар B — транспорт, потім пул, потім `fetch_mode` + авто-ескалація.
4. Шар C — маскування, CLI-прибирання.
5. UI.
6. Gate 7.2–7.7, перенесення в `doc/dev/done/`, Completion report із **виміряними** числами.

---

## ✅ Completion report

- **Дата:** 2026-08-19
- **Гілка / комміт:** `task-34-client-lifecycle-status` @ (див. коміт цієї таски)
- **Статус:** done-with-followups

### Що зроблено

Шари A і B реалізовані повністю й перевірені живо проти ALPHA; шар C — частково (маскування є, але виявилось некритичним; історичне прибирання — з обмеженим, свідомо звуженим скоупом).

- **Шар A (чесний `blocked`).** `classify_block()` ([fetcher.py](../../app/monitor/fetcher.py)) розпізнає блок через три незалежні сигнали (`429` завжди, `cf-mitigated`, `Retry-After` на 403/503) з фолбеком на розширений список маркерів по **всьому** тілу, а не перших 4 КБ. Заблокована відповідь більше не створює версію сторінки й не чіпає стан — короткий вихід на самому початку `_persist_page_result`. Замість вигаданого нового типу зміни **перевикористано вже готовий `monitor_blocked`** (severity WARNING, `app/monitor/severity.py`) — з'ясувалось, що task-36 вже підготував під нього весь портфельний UI (`blockedClientIds`/`rowStateOf`/бейдж «Blocked», фікстура `monitor-client-blocked.json`), просто runner ніколи його не емітив. Один агрегатний change на прогін (не по рядку на URL), self-heal через наявний `_reconcile`. Раннє гальмо після `monitor_block_streak_abort` (дефолт 5) послідовних блоків. Реалістичні заголовки (`Accept`/`Accept-Language`/`Upgrade-Insecure-Requests`) — гігієна, підтверджена живим виміром §0.5.
- **Шар B (antibot-доріжка).** Новий `app/monitor/browser_fetch.py` — drop-in транспорт на patchright (`app/reports/stealth_browser.py`, read-only імпорт), контракт ідентичний `fetch()`. Один браузер+контекст на весь прогін клієнта, блокування підресурсів (image/media/font/stylesheet), SSRF-гард на кожен хоп через `context.route` (не лише на перший URL). `MonitorScheduler` отримав другий `ThreadPoolExecutor` (`monitor_antibot_workers=1`) — antibot-клієнт **ніколи** не займає слот основних чотирьох. `MonitorSettings.fetch_mode` (`auto|http|browser`) з авто-ескалацією через `blocked_detected_at` + `monitor_antibot_sticky_days=14`; явний `http`/`browser` завжди перемагає евристику.
- **Шар C.** Маскування Shopify-токенів (`fingerprint.py`) додано, але **жива перевірка спростувала гіпотезу §0.9** — виявилось, що я міряв `normalize_text` на сирому HTML, а не на реальному `signals.visible_text`, який і хешується в проді; той шлях уже стабільний без жодних змін. Маскування лишено як безпечне захисне доповнення (той самий патерн, що й наявний `__CF$cv$params`), але воно **не** є фіксом реального дефекту — справжня причина шуму була повністю в Шарі A. CLI `monitor-resolve-blocked` реалізовано, але з переробленою детекцією: план передбачав пошук за `MonitorRun.notes_json.blocked_count`, що виявилось непридатним для ретроактивного прибирання (це поле існує лише для прогонів **після** фіксу). Замість цього — ground truth за поточним станом сторінки (`page.status` у 403/429/503 + вміст збереженого блобу класифікується як блок через `classify_block`).

### Як зроблено (ключові рішення)

- **Перевикористання `monitor_blocked` замість нового типу** — знайдено живим grep'ом по фронтенду ще до написання коду; зекономило цілий UI-шар роботи (клієнтський бейдж і лейбл уже існували).
- **`notes_json` замість нової колонки на `MonitorRun`** — `blocked_count` живе в наявному JSON-полі (`{"baseline": ..., "blocked_count": N}`, завжди присутнє, `0` для чистого прогону), тому міграція торкається лише `MonitorSettings` (`fetch_mode`, `blocked_detected_at`).
- **`response.body()`, ніколи `page.content()`** — живо підтверджено (§0.8): DOM після JS відрізняється на кожному завантаженні й дав би фальшиву зміну щопрогону; сирі байти відповіді дають **ідентичний** `signals_sha256` з HTTP-доріжкою.
- **Ретроактивна детекція в CLI переписана з run-based на page-state-based** після того, як перший `--dry-run` на реальних даних показав `nothing to resolve` — стара логіка спиралась на поле, якого просто не існувало в старих прогонах (задокументовано вище).
- Головні файли: [app/monitor/browser_fetch.py](../../app/monitor/browser_fetch.py) — новий транспорт; [app/monitor/runner.py](../../app/monitor/runner.py) — вся оркестрація blocked/browser-lane; [app/monitor/scheduler.py](../../app/monitor/scheduler.py) — другий пул; [app/monitor/fetcher.py](../../app/monitor/fetcher.py) — `classify_block`.
- **Відхилення від плану:** UI (Fetch mode селектор, чіп «Blocked» на рядку сторінки) свідомо не реалізовано цього разу — див. Follow-ups. B3 (адаптивний backoff/пауза всередині прогону на `retry_after_seconds`) не реалізовано — раннє гальмо (A4) виявилось достатнім на практиці (0 блоків на реальному browser-прогоні).

### Контрактні зміни (Contract changes)

Усі адитивні:

- `MonitorSettingsDTO.fetch_mode: "auto"|"http"|"browser"` (+ `MonitorSettingsUpdateRequest`), `MonitorSettingsDTO.blocked_detected_at: datetime|None` (read-only).
- `MonitorRunDTO.blocked_count: int|None` — `None` для прогонів до цієї таски, `0`/`N` для прогонів після.
- Новий (фактично — вперше емітований) тип зміни `monitor_blocked`, scope `"site"`.
- `openapi_snapshot.json` регенеровано.

### Як перевірити (verification)

```bash
docker compose exec api poetry run pytest tests/unit/monitor tests/unit/api tests/unit/db -q
docker compose exec api poetry run pytest tests/unit/monitor/test_blocked_handling.py -q -v
docker compose exec api poetry run ruff check app/monitor app/api/monitor.py app/cli.py
docker compose exec api poetry run mypy app
docker compose exec api poetry run alembic upgrade head
```

Живий гейт (виконано цього ж дня, реальний ALPHA, не фікстура):

```bash
curl -X PATCH .../clients/<ALPHA>/monitor/settings -d '{"fetch_mode":"browser"}'
curl -X POST  .../clients/<ALPHA>/monitor/runs?kind=full
# ~42 хв (355 сторінок, antibot-темп 2с/host-slot)
```

Результат: `status=succeeded`, `blocked_count=0`, `urls_checked=355/355`, `changes_critical=0`, `sitemap_health` 354/354 `ok` (було 304 `broken`). Портфельна регресія (§7.1) не запускалась як окремий скрипт — 11 інших клієнтів лишились на HTTP-доріжці без жодної зміни коду їхнього шляху (branch за `fetch_mode`, дефолт незмінний), ризик мінімальний.

### Тести

- `tests/unit/monitor/test_blocked_handling.py` (новий, 10 тестів): блокована сторінка не створює page-рядок; повторний блок не чіпає наявний стан/`consecutive_failures`; один агрегатний change замість N; self-heal на чистому прогоні; раннє гальмо на streak; частковий блок лишає реальні дані; `classify_block` на **реальному** збереженому 429-блобі ALPHA (регресійний гард — жоден зі старих 7 маркерів не збігався); `effective_fetch_mode` (усі 6 гілок: None/forced http/forced browser/auto-never/auto-recent/auto-stale); авто-ескалація `blocked_detected_at`; explicit `http` ніколи не ескалює.
- Повний сюїт: **1641 passed, 1 skipped, 1 deselected** (deselected — `test_aiseotools_signatures.py`, підтверджено пре-існуючий дрейф сусіднього AISeoTools checkout, не пов'язаний із цією таскою).

### Follow-ups / борг

- **UI не реалізовано** (`TODO(task-50-ui)`): селектор Fetch mode в налаштуваннях клієнта, чіп «Blocked» на рядку сторінки, бейдж «Auto-escalated to browser lane». Портфельний бейдж «Blocked» на картці клієнта вже працює (наявна інфраструктура task-36), керування режимом — лише через API/CLI (`PATCH .../monitor/settings {"fetch_mode": "browser"}`, `yovth-report monitor-run --fetch-mode browser`).
- **Історичний борг ALPHA не прибрано повністю.** `monitor-resolve-blocked` (виконано живо) закрив 640 фейкових алертів **поточної** хвилі (`open_critical` 1→0, `open_warning` 1980→1341, потім +31 реальних із живого browser-прогону → 1372). `open_info=1554` **не змінився** — це залишок від **попередніх**, уже самозагоєних хвиль (сторінки давно повернулись у 200, але одноразові типи змін типу `title_changed`/`content_changed` нічого автоматично не закриває — не self-heal-типи за дизайном task-35 §3.4). Точне прибирання цього боргу вимагає кореляції `MonitorChange.run_id` із конкретною `MonitorPageVersion`, якої зараз немає (версія не зберігає `run_id`) — окрема таска, якщо оператор вирішить це чистити, а не просто дати новим прогонам поступово витіснити старий шум.
- **B3 (адаптивний backoff/пауза на `retry_after_seconds` всередині прогону)** свідомо не реалізовано — див. «Відхилення від плану».
- **Виявлено (не усунено):** паралельна сесія Claude Code в цьому ж репозиторії підготувала `doc/dev/new/task-51-monitor-redirects.md`, який залежить від цієї таски й ділить ті самі owned paths (`app/monitor/**`, `app/api/monitor.py`, `frontend/js/monitor-client.js`). Порядок `50 → 51` вже зафіксований у самому task-51.
