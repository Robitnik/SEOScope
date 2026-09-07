# task-81 — Web Archive: replay не має залежати від того, чи виконався JS сторінки

Три незалежні причини того, що знімок у в'ювері виглядає гірше за живу сторінку, всі — на боці **рендера**, жодна не вимагає перекапчуру: ліниві картинки без промоушену (§0.1–§0.5), блоки, які назавжди лишились `visibility:hidden` (§0.6), і `blob:`-скрипти, зарізані нашим власним CSP (§0.7).

- **Хвиля:** post-63 (UX/вірність архіву) — та сама, що 70 і 80
- **Depends on:** **59** ✅, **60** ✅, **63** ✅ (код злитий комітом `2f703fb`; сам файл плану ще лежить у `new/`), **70** ✅. З **80** не конфліктує: різні owned paths (там капчур, тут replay-рендер).
- **Owned paths:**
  `app/webarchive/rewrite.py` — §2.1–§2.2,
  `app/api/archive_replay.py` — §2.4 (інжект fix-up-стилю поруч із шимом) і §2.5 (CSP),
  `app/core/config.py` — **два** адитивні прапорці (`archive_unlazy_html`, `archive_replay_fixup_css`) того ж класу, що `archive_live_iframes`, у тому ж блоці,
  `tests/unit/webarchive/test_rewrite.py`, `tests/unit/api/test_archive_replay_api.py`
- **Міграція:** **не потрібна.** Усі три перетворення відбуваються на рендері, збережені байти не змінюються (інваріант task-59 §1.5), тож фікс діє **ретроактивно на весь архів**.
- **Суміжне:** «погані копії» у LAXTON/SHB (сміттєвий фронтир, фальшиві версії) — це **капчур**, не рендер; винесено в окремий [task-82](task-82-archive-frontier-and-false-changes.md), бо `capture.py` зараз в оренді у task-80.

---

## §0. Виміряно на бойових даних (2026-09-07, стек піднятий, `127.0.0.1:6524`)

### §0.1 ⚠️ Зі скриптами увімкненими на сторінці немає **жодної** картинки

Оператор помітив, що знімок виглядає правильно **лише** з увімкненим чекбоксом «Disable page scripts». Перевірено на `https://www.shorelinedentalstudio.com/meet-the-team/` (`ts=20260904100333`, клієнт SDS) у контейнерному Chromium, у тому самому фреймі й з тим самим `sandbox`, що й у в'ювері ([`archive-viewer.js:450`](../../../frontend/js/archive-viewer.js#L450) — чекбокс не робить нічого іншого, лише прибирає `allow-scripts`):

| | скрипти **ON** | скрипти **OFF** |
|---|---|---|
| `document.images.length` | **124** | **248** |
| мережевих запитів на сторінку | **12** | **81** |
| `img[data-lazy-src]`, що лишились неперетвореними | 124 | 124 |
| зламаних `<img>` (`naturalWidth === 0`) | 0¹ | 0 |

¹ нуль оманливий: у лінивої копії `src` — це валідний SVG-плейсхолдер `data:image/svg+xml,…`, тому браузер вважає її «завантаженою». Логотип у шапці відсутній, на місці hero-фото — порожня біла плашка.

### §0.2 Корінь: сторінка везе картинки у **двох** копіях, і жодна з них не працює зі скриптами

Розмітка WP Rocket LazyLoad — 124 елементи, **всі 124** такої форми:

```html
<img … src="data:image/svg+xml,%3Csvg…%3E" data-lazy-src="/archive/r/<ts>/https://…/logo.png"
     data-lazy-srcset="…" data-lazy-sizes="…" />
<noscript><img … src="/archive/r/<ts>/https://…/logo.png" srcset="…" sizes="…" /></noscript>
```

- **Скрипти OFF** → браузер малює `<noscript>`-копію, у якої `src` справжній. Звідси «виглядає краще».
- **Скрипти ON** → `<noscript>` не рендериться взагалі, а промоутити `data-lazy-src → src` мала б бібліотека `lazyload.min.js`. Її тега в HTML **немає**; її вантажить власний відкладений лоадер сайту з `custom-css-js/14382.js`.

### §0.3 ⚠️ Лоадер сайту гине на **першому ж рядку** всередині нашого sandbox'у

```js
const loadOptimizationScripts = once(async function load(){
  let userCheck = localStorage.getItem('userCheck');          // ← SecurityError
  if (userCheck == null || userCheck == 'null') {
    const response = await fetch(`/wp-json/uverification/is_valid?cache_buster=${Date.now()}`);
    …
  }
  load_js("/wp-content/plugins/rocket-lazy-load/assets/js/16.1/lazyload.min.js");
  …  // ще ~20 load_js/load_css
});
document.addEventListener('DOMContentLoaded', …mousemove/touchstart/click/scroll…);
setTimeout(() => loadOptimizationScripts(), 0);
```

`localStorage` у нашому фреймі кидає `SecurityError: The document is sandboxed and lacks the 'allow-same-origin' flag` — це один із двох `pageerror`, які ловить проба. Функція обгорнута у `once()`, тож наступні `mousemove`/`scroll`/`click` її вже **не** викличуть. Тому й запитів рівно 12: жоден із ~20 файлів так і не був запитаний.

**Це ДРУГИЙ, окремий корінь, не той, що описаний у [task-63 §0.2](task-63-archive-replay-fidelity-and-urls.md#L40).** Там симптом той самий (`hearwellservices.com`, «скрипти ON = гірше»), але причина інша — LAB.js рахує базу від `location.href` і будує `https://seo.theyovth.space/wp-content/…`; фікс §3.1 уже стоїть у шимі ([`archive_replay.py:186-205`](../../../app/api/archive_replay.py#L186-L205)). На SDS він не рятує, бо **до** побудови URL справа не доходить. Пряме підтвердження: у бойовій `archive_replay_misses` по всьому домену `shorelinedentalstudio.com` є **рівно один** рядок — `cdn-cgi/zaraz/t`, і жодного `wp-content/…lazyload.min.js`. Промахів немає саме тому, що **немає запитів**.

І навіть якби `localStorage` працював, наступний рядок — `fetch('/wp-json/uverification/is_valid?cache_buster=' + Date.now())`, антибот-перевірка з кеш-бастером, яку архів не може мати за визначенням (task-63 §3.2 закриває кеш-бастери для **ассетів**, не для API): `response.json()` кинув би, і результат був би той самий.

### §0.4 Масштаб по портфелю — це не один клієнт

Скан **усіх** HTML-знімків (останній на кожен `(client, url)`), читання блобів напряму, без мережі:

```
pages scanned:               5638
lazy_img_attr                58885   ← атрибутів data-lazy-src
lazy_then_noscript           58785   ← з них одразу за якими йде <noscript>  (99.83%)
noscript_img_only            58812
noscript_any                 68570   ← решта ~9.8k — GTM-iframe'и, rocket-lazyload-nojs <style> тощо
lazy_srcset_attr             16039
source_lazy                      0   ← <source data-lazy-srcset> у портфелі НЕМАЄ
```

| Клієнт | сторінок | із `data-lazy-src` |
|---|---:|---:|
| 3XE | 665 | 665 |
| LAXTON | 1196 | 1195 |
| SAPHNY | 818 | 818 |
| SDS | 446 | 446 |
| HWELL | 304 | 304 |
| SHB | 134 | 134 |
| NYCHA | 94 | 94 |
| GRP | 32 | 32 |
| SWM | 27 | 27 |
| EFFYS | 17 | 17 |
| ALPHA / GOREPORT / 19CLV | 1905 | 0 |

**66% сторінок архіву (3732 із 5638) уражені.** Дефект не косметичний: архів існує рівно заради «як сторінка виглядала», а половина клієнтського портфеля показує сторінку без жодної картинки, якщо оператор не здогадався клацнути технічний чекбокс.

### §0.5 Перевірено, що фікс справді лікує — прямо на бойовому знімку

Замість гіпотези: завантажили **немодифікований** replay зі скриптами ON, дочекались, і застосували перетворення §2 прямо в DOM фрейма:

```
BEFORE:  {imgs: 124, lazyLeft: 124}   requests: 12
PROMOTE: {promoted: 252, dropped_noscript: 124}
AFTER :  {imgs: 124, lazyLeft: 0, broken: 0}   requests: 85
```

12 → **85** запитів, `broken: 0`. Тобто всі 124 картинки з'являються **зі скриптами увімкненими**, і жодного 404 — ассети в архіві були весь час, їх просто ніхто не запитував.

### §0.6 ⚠️ Другий, окремий дефект: блоки, які назавжди лишились `visibility:hidden`

Прохід по SHB / EFFYS / LAXTON (запит оператора) показав, що на цих клієнтах §2.1 **не рятує**: сторінка лишається порожньою навіть із промоутованими картинками. Корінь інший.

Elementor вішає клас `elementor-invisible` на кожен елемент із entrance-анімацією, а `elementor-frontend.js` знімає його, коли елемент входить у в'юпорт. CSS у самій сторінці:

```css
.elementor-invisible{visibility:hidden;}
```

В архіві цей JS не відпрацьовує ніколи (з тієї ж причини, що §0.3, або тому що його взагалі не завантажено), тож блок лишається `visibility:hidden` **назавжди**. Головна сторінка SHB через це — суцільний чорний прямокутник із самим лише меню (скрин `p4-shb.png`).

Важливо: **перемикач «Disable page scripts» тут не допомагає в жодному положенні** — зі скриптами вимкненими клас так само нікуди не дінеться. Це чистий дефект вірності архіву, а не наслідок пісочниці.

**A/B, виміряно на живому стеку** (`imgsPainted` = картинок із `naturalWidth > 1`, які реально видимі: ненульовий rect, не `visibility:hidden`, не `opacity:0`):

| клієнт | як зараз | тільки §2.1 (unlazy) | тільки §2.4 (unhide) | обидва |
|---|---|---|---|---|
| SHB | **29** / 61 | 29 | **50** | 50 |
| EFFYS | **7** / 111 | 94 | 110 | **110** |
| LAXTON | **13** / 26 | 13 | **24** | 24 |
| SDS | 54 / 124¹ | 54¹ | 54 | 54¹ |

¹ на SDS метрика `imgsPainted` сліпа: там лінива копія несе валідний SVG-плейсхолдер `viewBox="0 0 516 234"`, тобто `naturalWidth` = 516 і браузер вважає її «намальованою». Ефект §2.1 на SDS міряється запитами (12 → 85, §0.5), не цим лічильником.

Висновок читається прямо з таблиці: **два фікси незалежні й не замінюють один одного.** SHB і LAXTON лікує тільки §2.4, SDS — тільки §2.1, EFFYS потребує обох.

**Масштаб по портфелю** (той самий скан 5638 сторінок):

```
elementor-invisible на елементі:   1655 сторінок (29%),  2977 елементів
CSS-правило .elementor-invisible:  3396 сторінок
```

| Клієнт | сторінок | із `elementor-invisible` | елементів |
|---|---:|---:|---:|
| LAXTON | 1196 | **1189** | 1677 |
| ALPHA | 1623 | 361 | 724 |
| SHB | 134 | 51 | 293 |
| SAPHNY | 818 | 30 | 30 |
| EFFYS | 17 | 15 | 228 |
| GRP | 32 | 9 | 25 |

### §0.7 ⚠️ Третій дефект: наш власний CSP ріже `blob:`-скрипти сторінки

SHB віддає JS через кастомний відкладений лоадер («ECM»), який перетворює кожен інлайновий скрипт на `blob:`-URL:

```js
var url = URL.createObjectURL(new Blob([old.textContent], { type: 'text/javascript' }));
s.src = url;
```

Наш replay-CSP — `script-src 'self' 'unsafe-inline' 'unsafe-eval'` — схеми `blob:` не містить, тож **кожен** такий скрипт блокується:

```
Loading the script 'blob:null/…' violates the following Content Security Policy
directive: "script-src 'self' 'unsafe-inline' 'unsafe-eval'".   × 28 на одну сторінку
```

`createObjectURL` зустрічається на **3275 із 5638** сторінок архіву.

**Виміряно, що дає дозвіл `blob:`** (той самий знімок SHB, топ-левел-навігація, підмінений лише заголовок CSP):

| | CSP як зараз | `script-src … blob:` |
|---|---:|---:|
| CSP-блокувань `blob:` | 28 | **0** |
| елементів у `elementor-invisible` | 27 | 18 |
| `imgsPainted` | 29 | 39 |

Тобто дозвіл `blob:` **частково** лікує §0.6 сам по собі (JS добігає далі й знімає 9 із 27 класів), але до кінця не доводить — залишок закриває §2.4. І окремо варто зафіксувати: `script-src` **уже** несе `'unsafe-inline' 'unsafe-eval'`, тобто сторінка вже може виконувати довільний код; `blob:` не додає їй **жодної** нової можливості — це усунення непослідовності в самому CSP, а не послаблення межі.

---

## §1. Рішення в одному реченні

**Перестати залежати від того, чи виконався JS сторінки: довести розмітку до її кінцевого, «після-JS» стану прямо на рендері** — промоутити ліниві картинки (§2.1–§2.2), зняти `visibility:hidden` з анімаційних блоків (§2.4) і перестати різати власним CSP `blob:`-скрипти, які сторінка й так має право виконувати (§2.5). Усе — ретроактивно на весь архів, без перекапчуру й без послаблення пісочниці.

---

## §2. Що саме робить перетворення

§2.1–§2.3 — три кроки в `rewrite_html`, у цьому порядку, **після** того як `_stash` уже сховав тіла `<script>`/`<style>` під плейсхолдери, і **після** звичайного проходу `_rewrite_tag_attrs` (щоб промоутити вже переписану на `/archive/r/…` адресу, а не сиру):

**§2.1 Промоушен лінивих атрибутів на `<img>`.**

| з | у |
|---|---|
| `data-lazy-src` | `src` (перезаписує SVG-плейсхолдер) |
| `data-lazy-srcset` | `srcset` |
| `data-lazy-sizes` | `sizes` |

Лінивий атрибут при цьому **видаляється** — інакше, якщо `lazyload.min.js` колись усе ж завантажиться, він обробить елемент удруге; і, важливіше, `<noscript>`-стиль самого WP Rocket (`[data-lazy-src]{display:none !important}`) сховав би промоутований елемент при вимкнених скриптах. `<source data-lazy-srcset>` **не** обробляємо: у портфелі таких 0 (§0.4), а мертвий код у рерайтері — це мертвий регекс, який колись когось вкусить.

**§2.2 Зняття парної `<noscript>`-копії.** Інакше при вимкнених скриптах картинка задвоїться (промоутована + noscript). Правило навмисно **найвужче з можливих**: знімається лише `<noscript>`, що йде **безпосередньо** за щойно промоутованим елементом (`data-lazy-src[^>]*>\s*<noscript>`). Виміряне покриття — **99.83%** (58 785 із 58 885). Решта `<noscript>` (≈9.8 тис.: GTM-iframe'и, `rocket-lazyload-nojs` `<style>`, текстові фолбеки) не чіпається взагалі.

Побічний, але бажаний ефект: `<noscript><img src="…facebook.com/tr?id=…">`-піксель, що стоїть за лінивою картинкою, теж зникає — узгоджено з політикою task-60 §2.2 (не слати живий хіт у GA/Meta клієнта на кожному перегляді знімка).

**§2.3 Прапорець-вимикач.** `Settings.archive_unlazy_html: bool = True` — рівно того ж класу, що `archive_live_iframes`: не тюнінг, а kill-switch, щоб відкотити поведінку без деплою, якщо на якомусь клієнті знайдеться розмітка, якої ми не передбачили.

**Чого перетворення НЕ робить:** не чіпає збережені байти (тільки рендер), не чіпає тіла `<script>`/`<style>` (вони під плейсхолдерами — перевірено: 3 згадки `data-lazy-src` у SDS-документі, що лишаються після проходу, всі три саме там), не чіпає `<iframe>` (стеш task-60 §2), не додає жодного мережевого запиту на сервері.

**§2.4 Fix-up-стиль: показати блоки, які JS мав розкрити (закриває §0.6).**

Свідомо **не** редагуємо `class`-атрибути регексом. Замість хірургії по чужій розмітці — один інжектований `<style>` поруч із шимом, у той самий `_inject_shim`-хук:

```html
<style id="archive-replay-fixups">.elementor-invisible{visibility:visible!important}</style>
```

Чому саме так, а не зняття класу:

- **Оборотно й не руйнівно.** Байти документа лишаються собою; ми лише перекриваємо одне правило. Регекс по `class="…"` довелося б писати на всі форми лапок і порядки класів — рівно той клас коду, що колись мовчки зіпсує тег.
- **Специфічність гарантована.** Правило сторінки — `.elementor-invisible{visibility:hidden;}` **без** `!important` (перевірено в бойовому HTML), тож наше `!important` виграє незалежно від порядку підключення.
- **Одне місце для майбутніх fix-up'ів.** Наступний «клас, який JS мав зняти» додається рядком у той самий `<style>`, без нового проходу по HTML.
- **Не ламає нічого, якщо JS усе ж відпрацює.** Elementor зніме клас — правило просто перестане на щось поширюватись.

Прапорець `Settings.archive_replay_fixup_css: bool = True` вимикає інжект цілком.

**§2.5 `blob:` у `script-src` replay-CSP (закриває §0.7).** Один токен у наявному заголовку. Обґрунтування — у §0.7: `script-src` уже несе `'unsafe-inline' 'unsafe-eval'`, тож нової можливості сторінці це не дає, зате прибирає 28 блокувань на одній лише головній SHB і на 3275 сторінках портфеля, які взагалі користуються `createObjectURL`. `frame-src`, `img-src`, `connect-src` **не чіпаємо** — там `blob:` не потрібен і межу task-59 §1.6 не рухаємо.

---

## §3. Реалізація

Одна нова функція + два рядки у `rewrite_html`. Файл: `app/webarchive/rewrite.py`.

```python
_LAZY_ATTRS = (("data-lazy-src", "src"), ("data-lazy-srcset", "srcset"), ("data-lazy-sizes", "sizes"))
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_LAZY_NOSCRIPT_RE = re.compile(r"(?<=\x00LAZY\x00)\s*<noscript>.*?</noscript\s*>", re.IGNORECASE | re.DOTALL)


def unlazy_html(html: str) -> str:
    """Промоутить ліниві атрибути WP Rocket LazyLoad у справжні й знімає
    парну `<noscript>`-копію (task-81 §2).

    Викликається ПІСЛЯ `_stash` — тіла `<script>`/`<style>` на цей момент
    уже плейсхолдери, тож регекс не може зіпсувати рядок `"img[data-lazy-src]"`
    усередині конфігу lazyload'а. І ПІСЛЯ `_rewrite_tag_attrs` — `data-lazy-src`
    уже несе `/archive/r/<ts>/…` (він є в `_ATTR_RE`), тож промоушен переносить
    ГОТОВУ replay-адресу, а не сиру.
    """
```

Ключова деталь реалізації — **як знайти парну `<noscript>`**. Робити це другим незалежним регексом по всьому документу небезпечно (зачепить чужі `<noscript>`). Тому промоутований тег помічається сентинелом того ж класу, що вже використовує модуль (`\x00…\x00`, див. `_PLACEHOLDER`), сентинел знімається наприкінці разом із розстешуванням. Байт `\x00` не може легально трапитись у HTML-тексті — це вже інваріант модуля, не нове припущення.

Порядок у `rewrite_html`:

```python
    stashed = _SCRIPT_OR_STYLE_RE.sub(_stash, stashed)
    rewritten = _rewrite_tag_attrs(stashed, base_url=base_url, ts=ts)
+   if unlazy:
+       rewritten = unlazy_html(rewritten)
    …  # далі — наявне зняття <base>, integrity/crossorigin, розстешування
```

Сигнатура: `rewrite_html(..., unlazy: bool = True)` — адитивний keyword-only параметр із дефолтом, як `live_iframes`. Виклик у [`archive_replay.py:550`](../../../app/api/archive_replay.py#L550) передає `settings.archive_unlazy_html`. `rewrite_html` більше нізвідки не викликається (перевірено), тож жодного іншого споживача перетворення не зачіпає.

### §3.2 Fix-up-стиль (§2.4) — у той самий хук, що й шим

[`_inject_shim`](../../../app/api/archive_replay.py#L321) уже вміє єдину потрібну річ: знайти кінець `<head …>` (з фолбеком на `<html …>`) і вставити туди тег. Розширюємо його рівно на один рядок — стиль іде **після** тега шима, щоб порядок інжектів був стабільний і легко грепався в тестах:

```python
_FIXUP_CSS = (
    '<style id="archive-replay-fixups">'
    # Elementor вішає цей клас на кожен елемент з entrance-анімацією і знімає
    # його своїм frontend-JS при вході у в'юпорт. В архіві той JS не
    # відпрацьовує ніколи (task-81 §0.6), тож блок лишається
    # `visibility:hidden` НАЗАВЖДИ — головна SHB через це чорний прямокутник.
    # Правило сторінки йде без `!important`, тож наше виграє за будь-якого
    # порядку підключення.
    ".elementor-invisible{visibility:visible!important}"
    "</style>"
)
```

Свідомо **не** окремий файл `/archive/static/fixups.css`: це один рядок, а зайвий підресурс — це ще один запит на кожен перегляд знімка і ще один шлях, який має пройти через `AuthMiddleware`.

### §3.3 CSP (§2.5)

Один токен у наявному рядку заголовка, там же в `archive_replay.py`:

```diff
- script-src 'self' 'unsafe-inline' 'unsafe-eval'
+ script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:
```

Без прапорця: це не поведінка, яку колись захочеться відкотити окремо від решти (а якщо захочеться — це один рядок і деплой, як і будь-яка інша зміна CSP).

---

## §4. Definition of Done / Gate

1. `docker compose exec api poetry run pytest tests/unit/webarchive/test_rewrite.py -q` — зелено, **включно з усіма наявними тестами** (перетворення не сміє зачепити жодного з них).
2. `ruff check .` + `mypy app` — зелено.
3. **Жива перевірка №1 (SDS, WP Rocket).** `#/archive/view/...meet-the-team/`, чекбокс «Disable page scripts» **вимкнено** (тобто скрипти працюють): видно логотип у шапці, hero-фото коридору, всі фото команди. Кількість мережевих запитів фрейма ≥ 80 (було 12).
4. **Жива перевірка №2 (той самий знімок, скрипти вимкнені).** Картинок стільки ж, **без задвоєння** — `document.images.length` однакова в обох станах перемикача (сьогодні 124 проти 248).
5. **Жива перевірка №3 (клієнт без WP Rocket).** Будь-яка сторінка ALPHA або GOREPORT рендериться **байт-у-байт як раніше** — перетворення на них не спрацьовує (0 промоушенів) і нічого не ламає.
6. **Жива перевірка №4 (чужий `<noscript>`).** На сторінці з GTM-`<noscript><iframe>` цей блок лишається на місці (грепом по відповіді replay).
7. **Жива перевірка №5 (SHB, §2.4).** `https://smashhouseburgers.com/` у в'ювері: замість суцільного чорного прямокутника — hero з мавпою, заголовком «THE FIRST GLATT KOSHER…» і чотирма кнопками локацій. `document.querySelectorAll('.elementor-invisible')` у фреймі все ще повертає елементи (клас на місці — ми його не знімаємо), але **жоден із них не `visibility:hidden`**.
8. **Жива перевірка №6 (EFFYS, §2.1 + §2.4 разом).** `https://effyscafe.com/`: під заголовком «YOUR DAILY ESCAPE» видно каруселі з тарілками й бігучий рядок «EFFY'S CAFE». `imgsPainted` ≥ 100 зі 111 (сьогодні 7).
9. **Жива перевірка №7 (LAXTON, §2.4).** `https://www.laxton.se/` — `imgsPainted` ≥ 22 з 26 (сьогодні 13).
10. **Жива перевірка №8 (§2.5).** У консолі фрейма SHB **нуль** повідомлень `violates the following Content Security Policy` зі схемою `blob:` (сьогодні 28).
11. `archive_replay_misses` після перевірок №1–9 **не поповнюється** новими рядками: ассети вже в архіві, ми лише починаємо їх запитувати.

---

## §5. Тести

`tests/unit/webarchive/test_rewrite.py` (усі — чисті юніти, без мережі й БД):

- `test_unlazy_promotes_data_lazy_src` — `data-lazy-src` стає `src`, SVG-плейсхолдер зникає, значення вже у формі `/archive/r/<ts>/…`.
- `test_unlazy_promotes_srcset_and_sizes` — `data-lazy-srcset`/`data-lazy-sizes` → `srcset`/`sizes`; кожен URL у `srcset` переписаний, дескриптори (`516w`) збережені.
- `test_unlazy_removes_lazy_attributes` — після промоушену `data-lazy-*` у документі не лишається (інакше `[data-lazy-src]{display:none}` сховає елемент).
- `test_unlazy_drops_the_paired_noscript` — сусідній `<noscript><img></noscript>` знято; **рівно один** `<img>` у результаті.
- `test_unlazy_keeps_unrelated_noscript` — `<noscript><iframe src="googletagmanager…">` і `<noscript><style id="rocket-lazyload-nojs-css">` лишаються недоторканими.
- `test_unlazy_keeps_noscript_not_adjacent_to_a_lazy_image` — `<noscript><img>` без лінивого попередника не знімається (щоб не втратити єдину копію картинки).
- `test_unlazy_never_touches_script_bodies` — документ із `elements_selector: "img[data-lazy-src]"` усередині інлайнового `<script>` виходить із тілом скрипта байт-у-байт (регресія на головну пастку модуля).
- `test_unlazy_is_a_noop_without_lazy_markup` — сторінка без `data-lazy-*` повертається байт-у-байт (клас ALPHA/GOREPORT, 1905 сторінок портфеля).
- `test_unlazy_can_be_disabled` — `rewrite_html(..., unlazy=False)` віддає стару поведінку (kill-switch справді вимикає).
- `test_unlazy_handles_self_closing_and_bare_img` — і `<img … />`, і `<img …>` дають валідний тег (у прототипі саме тут був баг: лишався висячий `/` посеред тега).

`tests/unit/api/test_archive_replay_api.py`:

- `test_replay_injects_the_fixup_stylesheet` — у відповіді є `<style id="archive-replay-fixups">` із правилом `.elementor-invisible{visibility:visible!important}`, і він стоїть **у `<head>`**, поруч із тегом шима.
- `test_replay_fixup_stylesheet_can_be_disabled` — `archive_replay_fixup_css=False` прибирає інжект повністю.
- `test_replay_csp_allows_blob_scripts` — заголовок `Content-Security-Policy` містить `blob:` **тільки** в `script-src`; `frame-src`/`img-src`/`connect-src` лишаються як були (регресія на «не розширили межу ширше, ніж домовлялись»).
- `test_replay_non_html_is_not_touched` — CSS/JS/зображення віддаються без інжекту (вони й так не проходять через `rewrite_html`, але це та регресія, яку легко зламати).

---

## §6. Контрактні зміни

- `app/webarchive/rewrite.py`: новий публічний `unlazy_html(html)` + адитивний keyword-only `unlazy: bool = True` у `rewrite_html`. Наявні виклики не змінюються.
- `app/core/config.py`: адитивні поля `archive_unlazy_html: bool = True`, `archive_replay_fixup_css: bool = True`.
- `app/api/archive_replay.py`: у replay-відповідь HTML додається `<style id="archive-replay-fixups">`; у CSP-заголовок — токен `blob:` у `script-src`. Обидва — зміна **тільки** replay-рендера, не збережених даних.
- API-схеми/БД/фронт — **без змін**. `task-02` чіпати не треба.

---

## §7. Що свідомо НЕ робимо

| Відхилено | Чому |
|---|---|
| `allow-same-origin` у sandbox фрейма | Фрейм віддається з **нашого** origin, тож це дало б архівному сторонньому JS повний доступ до сесії інструменту — обмін реальної безпекової межі (task-59 §1.6) на зручність. Те саме рішення, що вже зафіксоване в task-63 §5. |
| Заглушка `localStorage`/`sessionStorage`/`document.cookie` у шимі | Спокусливо (закриває цілий клас «бутстрап гине на першому рядку»), але **виміряно не закриває спостережений кейс**: одразу за `localStorage` іде `fetch('…?cache_buster=' + Date.now())`, який архів не може мати за визначенням. А ціна — сторінковий JS починає доходити далі й робити те, чого в архіві робити не мав: A/B-тести, cookie-банери, перемальовування DOM. §2 знімає залежність від JS повністю й коштує дешевше. Лишається як `TODO(task-81 §7)`, якщо колись знайдеться сторінка, яку розблоковує **тільки** це. |
| Проставляти `.e-lazyloaded` на `.e-con.e-parent` (фонові зображення Elementor) | Виглядало як очевидний парний фікс: 82% сторінок портфеля везуть CSS `.e-con.e-parent:nth-of-type(n+4):not(.e-lazyloaded){background-image:none!important}`. **Але виміряний ефект — нуль на обох зразках**: SDS `bgPainted` 1 → 1, ALPHA 0 → 0 (фони, які реально є, стоять на перших трьох контейнерах, яких `nth-of-type(n+4)` не зачіпає). Додавати клас у чужу розмітку заради нуля виміряного виграшу — ні. |
| Промоушен `<source data-lazy-srcset>` усередині `<picture>` | У портфелі таких **0** (§0.4). Додамо, коли з'явиться клієнт, у якого вони є. |
| Перекапчур портфеля | Не потрібен і був би марною тратою: перетворення живе на рендері, старі знімки лікуються самі (інваріант task-59 §1.5). |
| Полагодити 59 зламаних `<img>` на ALPHA | Це **інший** клас: `wp-image…-100x100.png` реально відсутні в архіві (вони ж — верхівка `archive_replay_misses`), тобто питання бюджету ассетів task-60, а не лінивої розмітки. Сюди не тягнемо. |
| Знімати клас `elementor-invisible` з розмітки (замість CSS-перекриття §2.4) | Хірургія регексом по чужому `class="…"` заради ефекту, який дає один рядок CSS. Аргументи повністю — в §2.4. |
| Ініціалізувати Swiper-каруселі, які не стартували без JS | `swiper-slide` є на **4284 із 5638** сторінок, `.swiper-initialized` після §2.4 — все ще 0. Але це вже не «сховане», а «не анімоване»: слайди в DOM, перший видно, зміст знімка не втрачено. Емулювати чужу JS-бібліотеку на рендері — окремий клас складності з нульовим виграшем для питання «як сторінка виглядала». Якщо колись знадобиться — це кандидат на капчур через реальний браузер (`TODO(task-64)` з task-63 §5), а не на рерайтер. |
| `blob:` у `frame-src`/`img-src`/`connect-src` | §0.7 виміряв потребу **тільки** в `script-src`. Розширювати решту «за компанію» — рухати межу task-59 §1.6 без жодного виміряного приводу. |
| Сміттєвий фронтир LAXTON і фальшиві версії SHB («погані копії») | Це капчур, не рендер: `extract_links` не розекранює `&amp;`, фронтир ковтає `?add-to-cart=…`, а `content_fingerprint` рахує `value` прихованого інпута. Винесено в [task-82](task-82-archive-frontier-and-false-changes.md) — у тому числі тому, що `capture.py` зараз в owned paths активної task-80. |

---

## §8. Ризики

| Ризик | Мітигація |
|---|---|
| Регекс промоушену псує тег на екзотичній розмітці | Тіла `<script>`/`<style>` під плейсхолдерами; правило `<noscript>` — суто адьяцентне; тест на self-closing/bare `<img>`; kill-switch `archive_unlazy_html=False` без деплою. |
| Втрата єдиної копії картинки, якщо `<noscript>` знято помилково | `<noscript>` знімається **лише** одразу за промоутованим лінивим елементом. Якщо промоушену не було — не знімається нічого (тест `..._not_adjacent_...`). |
| Сторінка починає тягнути в 7 разів більше ассетів (12 → 85 запитів) | Це і є мета — ассети вже лежать в архіві й віддаються локально. Живого трафіку назовні не додається. |
| Розходження з тим, «як сторінка виглядала насправді» | Навпаки: сьогодні replay показує стан, якого в живого відвідувача не буває **ніколи** (лінива розмітка без лоадера; блок, який назавжди лишився `visibility:hidden`). Обидва фікси відтворюють саме кінцевий стан, який бачить справжній відвідувач. |
| §2.4 показує елемент, який на живому сайті мав лишитись схованим | `elementor-invisible` — **виключно** транспортний клас entrance-анімації Elementor: він завжди знімається при вході у в'юпорт і ніколи не використовується як «сховати назавжди» (для цього є `elementor-hidden-*`, які ми не чіпаємо). Ризик обмежений сторінками, де користувач не доскролив би — але для архіву «доскролено все» і є правильним станом. |
| §2.5 послаблює CSP | Не послаблює: `script-src` уже містить `'unsafe-inline' 'unsafe-eval'` (§0.7). `blob:`-скрипт створюється тим самим кодом сторінки, який уже виконується — нової можливості немає, є усунення непослідовності. Решта директив не змінюється, і тест `test_replay_csp_allows_blob_scripts` це фіксує. |

---

## ✅ Completion report

- **Дата:** 2026-09-07
- **Гілка / комміт:** `feat/task-80-archive-forced-recapture` @ `12a122d`
- **Статус:** done

### Що зроблено

Усі три виміряні фікси §2.1–§2.5 реалізовані й перевірені живцем на бойових знімках SDS, SHB, EFFYS, LAXTON, ALPHA, GOREPORT:

- **§2.1–§2.3 (unlazy).** `unlazy_html()` у [rewrite.py](../../app/webarchive/rewrite.py#L163) промоутить `data-lazy-{src,srcset,sizes}` у `src`/`srcset`/`sizes` і знімає суто адьяцентну `<noscript>`-копію. Викликається з `rewrite_html(..., unlazy=True)`.
- **§2.4 (fix-up CSS).** `_inject_shim` в [archive_replay.py](../../app/api/archive_replay.py#L346) додає `<style id="archive-replay-fixups">.elementor-invisible{visibility:visible!important}</style>` одразу після тега шима.
- **§2.5 (CSP `blob:`).** `_replay_csp` тепер несе `blob:` у `script-src`; решта директив без змін.
- Обидва прапорці (`archive_unlazy_html`, `archive_replay_fixup_css`) додані в `Settings` як kill-switch того ж класу, що `archive_live_iframes`.

### Як зроблено (ключові рішення)

- Ніяких відхилень від плану — реалізація йде §3.1–§3.3 буквально: `unlazy_html` — окрема публічна функція з сентинел-маркуванням (`\x00WEBARCHIVE-LAZY\x00`), а не другий незалежний регекс по всьому документу; fix-up — один інжектований `<style>`, а не хірургія по `class="..."`; CSP — один токен без нового прапорця (домовлено в §3.3 — це не поведінка, яку захочеться відкотити окремо від решти CSP).
- Одна деталь, якої план не деталізував: `data-lazy-src`/`data-lazy-srcset` уже потрапляють у `/archive/r/<ts>/…` форму ДО виклику `unlazy_html` — існуючий `_SRCSET_RE`'s `\bsrcset` вже випадково (і безпечно) матчить всередину `data-lazy-srcset` через відсутність провідного `\b`-захисту на самій назві атрибута. Тобто `unlazy_html` лише ПЕРЕНОСИТЬ уже коректне значення, ніколи не резолвить URL сам — підтверджено тестом і живою перевіркою.
- Головні файли: `app/webarchive/rewrite.py` (§2.1–§2.3), `app/api/archive_replay.py` (§2.4–§2.5), `app/core/config.py` (два прапорці).

### Контрактні зміни

`app/webarchive/rewrite.py`: новий публічний `unlazy_html(html)` + адитивний keyword-only `unlazy: bool = True` у `rewrite_html`. `app/core/config.py`: адитивні `archive_unlazy_html`, `archive_replay_fixup_css`. `app/api/archive_replay.py`: `_inject_shim` отримав адитивний keyword-only `fixup_css: bool = True`; replay-CSP несе `blob:` у `script-src` безумовно (без прапорця). API/схеми/БД/фронт — без змін.

### Як перевірити (verification)

```bash
docker compose exec api poetry run pytest tests/unit/webarchive/test_rewrite.py tests/unit/api/test_archive_replay_api.py -q
docker compose exec api poetry run ruff check .
docker compose exec api poetry run mypy app
```

Живо (стек піднятий, `AGENT_DEV_TOKEN` з `.env`):

- `curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:6524/archive/r/20260904100333/https://www.shorelinedentalstudio.com/meet-the-team/` зі скриптами ON у sandbox-фреймі: `document.images.length === 124`, той самий рахунок зі скриптами OFF (сьогодні 124 проти 248 до фіксу).
- `https://smashhouseburgers.com/` (`ts=20260906163749`): видно hero з мавпою й заголовком «THE FIRST GLATT KOSHER…» замість чорного прямокутника; `imgsPainted: 50/61` (було 29).
- `https://effyscafe.com/` (`ts=20260904083746`): `imgsPainted: 110/111` (було 7); `<noscript>`-очищення й промоушен разом.
- `https://www.laxton.se/` (`ts=20260906163044`): `imgsPainted: 24/26` (було 13).
- Консоль фрейма SHB: 0 повідомлень `violates the following Content Security Policy` зі схемою `blob:` (було 28).
- GTM-`<noscript><iframe>` на `goreport.com/` (`ts=20260904133527`) лишається байт-у-байт на місці.
- ALPHA/GOREPORT: `data-lazy-src=` — 0 входжень, `unlazy_html` — справжній noop; `elementor-invisible` фіксується так само (бонус, не регресія).

### Тести

`tests/unit/webarchive/test_rewrite.py` — 10 нових (`test_unlazy_*`): промоушен `src`/`srcset`/`sizes`, видалення lazy-атрибутів, зняття парної/незняття чужої/неадьяцентної `<noscript>`, недоторканність тіл `<script>`, noop без lazy-розмітки, kill-switch, self-closing/bare `<img>`.
`tests/unit/api/test_archive_replay_api.py` — 4 нові: інжект fix-up-стилю поруч із шимом, вимикання прапорцем, `blob:` лише в `script-src` (решта директив незмінні), non-HTML відповіді без інжектів.
Повний прогін `tests/unit/webarchive` + `tests/unit/api` — 737 тестів, 735 passed, 2 pre-existing failures (`test_openapi_snapshot`, `test_tech_audit::...open_errors`) — підтверджено відтворюваними на чистому `git stash`, до task-81 жодного стосунку не мають.

### Follow-ups / борг

- «Погані копії» (сміттєвий фронтир LAXTON, фальшиві версії SHB) — окрема таска [task-82](task-82-archive-frontier-and-false-changes.md), капчур-бік, не рендер.
- Swiper-каруселі, які не ініціалізуються без JS (0 `.swiper-initialized` навіть після §2.4) — свідомо поза обсягом (§7), кандидат на капчур через реальний браузер (`TODO(task-64)`).
- Заглушка `localStorage`/`document.cookie` у шимі — відхилено (§7), лишається `TODO(task-81 §7)` якщо колись знайдеться сторінка, яку розблоковує тільки це.
