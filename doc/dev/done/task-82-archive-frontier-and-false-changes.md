# task-82 — Web Archive: сміттєвий фронтир і фальшиві версії («погані копії»)

92% фронтиру одного клієнта — це URL, яких на сайті не існує або які не є сторінками; головна сторінка іншого клієнта отримує нову «версію» кожні 5 годин, не змінившись. Обидві причини — на боці **капчуру** (рендер лікує [task-81](task-81-archive-replay-fidelity-fixups.md), і ці дефекти він не бачить).

- **Хвиля:** post-63 (UX/вірність архіву) — та сама, що 70, 80, 81
- **Depends on:** **59** ✅, **62** ✅, **69** ✅, **80** — на 2026-09-07 її план щойно поїхав у `doc/dev/done/`, але робоче дерево ще несе незакоммічені правки `app/webarchive/capture.py`. §2 цього плану править саме той файл, тож **перед стартом §2 звірити `git status`**: доки capture.py брудний — не чіпати (див. пам'ятку про антиколізію в CLAUDE.md). §1 і §3 (`changes.py`, `fingerprint.py`) не перетинаються ні з ким і можуть іти раніше.
- **Owned paths:**
  `app/webarchive/changes.py` — §1,
  `app/webarchive/capture.py` — §2 (**тільки** `_discover_same_host_links`; решта файлу — task-80),
  `app/webarchive/urls.py` — §2 (нова чиста функція-політика),
  `app/core/config.py` — два адитивні поля,
  `app/webarchive/fingerprint.py` — §3,
  `tests/unit/webarchive/test_{changes,urls,fingerprint,capture}.py`,
  `scripts/archive_prune_junk_urls.py` (новий) — §4
- **Міграція:** **не потрібна.** Прибирання вже накопиченого сміття — разовий скрипт (§4), не схема.

---

## §0. Виміряно на бойових даних (2026-09-07, Postgres, стек піднятий)

### §0.1 ⚠️ LAXTON: 92% фронтиру — це кнопка «додати в кошик»

```sql
select count(*) urls,
       count(*) filter (where url like '%add-to-cart%') add_to_cart,
       count(*) filter (where url like '%&amp;%')       html_entity,
       count(*) filter (where snapshot_count = 0)       never_captured
from archive_urls u join clients c on c.id = u.client_id where c.name = 'LAXTON';
```

| urls | add_to_cart | html_entity | never_captured |
|---:|---:|---:|---:|
| **3194** | **2944** (92%) | **1869** (59%) | **1998** (63%) |

Один-єдиний базовий шлях породив **2557** варіантів URL:

| базовий шлях | варіантів URL | знімків |
|---|---:|---:|
| `/shop/3h-ghost-livestream/repriser-3h-g…` | **2557** | 1324 |
| `/shop/3h-ghost-livestream/kommande-3h-g…` | 62 | 39 |
| `/shop/klader/` | 48 | 13 |

**1324 із 1534** HTML-знімків LAXTON (86%) — це знімки таких URL. Для порівняння, у клієнта, який не є WooCommerce-магазином, `with_query` близький до нуля: SHB — 1 із 134, EFFYS — 0 із 17.

Ці 1998 незнятих URL **не розсмокчуться**: вони лежать у фронтирі й претендують на бюджет кожного обходу, назавжди.

### §0.2 ⚠️ Половина з них — адреси, яких на сайті не існує

[`extract_links`](../../../app/webarchive/changes.py#L32) бере значення атрибута `href` **як є**, без `html.unescape`:

```python
_ANCHOR_HREF_RE = re.compile(r"""<a[^>]+href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
...
absolute = resolve(base_url, raw)      # raw усе ще містить "&amp;"
```

WordPress екранує амперсанд у `href` — і в іменованій формі, і в числовій. Результат у бойовій базі:

```
1869 × url like '%&amp;%'     https://www.laxton.se/shop/…/2024/?orderby=price&amp;add-to-cart=853426
   6 × url like '%&#038;%'    https://www.laxton.se/page/2/?taxonomy=product_shipping_class&#038;term=123
```

Ми запитуємо в сайту параметр із буквальним іменем `amp;add-to-cart`, отримуємо `200` і зберігаємо знімок **під адресою, якої на сайті немає**. Це найгірший клас помилки для архіву: не «чогось бракує», а «є запис про те, чого не було».

### §0.3 ⚠️ SHB: нова «версія» головної кожні 5 годин, хоча сторінка не змінилась

Головна `smashhouseburgers.com/` має **6** знімків за 36 годин, усі зі `store_reason='changed'`, усі з однаковим `word_count=1188` і `asset_count=214`.

Порівняння **токенів фінгерпринта** (не сирих байтів — `content_fingerprint` уже правильно пропускає тіла `<script>`) для всіх 5 послідовних пар:

| пара | різних токенів | що саме |
|---|---:|---|
| 5 → 6 | 3 | `<link rel=preload>` лого, `gf_browser_*`, `<input type=hidden value=…>` |
| 4 → 5 | 2 | `<link rel=preload>` лого, `<input type=hidden value=…>` |
| 3 → 4 | 2 | `gf_browser_*`, `<input type=hidden value=…>` |
| 2 → 3 | **1** | `<input type=hidden value=…>` |
| 1 → 2 | 2 | `gf_browser_*`, `<input type=hidden value=…>` |

Три різні джерела шуму, і **лише одне присутнє в усіх 5 парах**:

1. **`<input type="hidden" name="gform_currency" value="<base64>">`** — Gravity Forms мінтить свіжий шифрований токен на кожен рендер. `value` є в [`_KEPT_ATTRS`](../../../app/webarchive/fingerprint.py#L44), а `_HEXISH_RE` ловить лише hex, тож base64 з `+/=` крізь нього проходить.
2. **`gf_browser_unknown` ↔ `gf_browser_safari`** у `class` — варіант із кешу WP Fastest Cache, зліплений під іншого відвідувача.
3. **`<link rel=preload href=…Logo_new2.webp>` ↔ `…hero-logo.webp`** — і воно **гойдається туди-сюди** між тими самими двома значеннями (старий і новий логотип), тобто теж кеш-варіант, а не редагування.

Ціна: таймлайн версій показує 6 «змін» там, де редагувань не було; операторові, який відкриє календар версій, доведеться шукати справжню зміну серед шуму. Зайві блоби при цьому **не** пишуться (`store_blob` контентно-адресований), тобто це дефект довіри до даних, а не сховища.

### §0.4 Чого тут НЕМАЄ (перевірено, щоб не гнатись за привидами)

- **www/non-www дублікати.** Є у 6 клієнтів, але рівно по **1** URL на клієнта (сід), проти 449–3193 на канонічному хості. Не проблема.
- **Дублікати за фінгерпринтом усередині LAXTON.** 1534 знімки → 1519 різних фінгерпринтів: кошикові сторінки відрізняються станом кошика, тож **пост-фактум дедуп їх не склеїть**. Лікується тільки на вході, у фронтирі (§2).
- **Відмови капчуру ассетів.** У SHB `failure_count > 0` рівно в **0** із 1626 ассетів — усе, що ми пробували взяти, взялося.

---

## §1. Фікс: розекранувати HTML-entity перед резолвом посилання

`app/webarchive/changes.py`, дві функції:

```python
from html import unescape

def extract_links(html: str, base_url: str) -> set[str]:
    found: set[str] = set()
    for raw in _ANCHOR_HREF_RE.findall(html):
        # WordPress екранує амперсанд у href і в іменованій (`&amp;`), і в
        # числовій (`&#038;`) формі. Без цього кроку ми запитуємо параметр із
        # буквальним іменем `amp;add-to-cart` і архівуємо знімок під адресою,
        # якої на сайті не існує (task-82 §0.2 — 1875 таких рядків у LAXTON).
        absolute = resolve(base_url, unescape(raw))
        ...
```

Те саме в `extract_images` — `srcset`/`src` екрануються так само.

Це ж виправляє й **дифи**: сьогодні `SnapshotFields.links` містить екрановані адреси, тож перехід сайту з `&amp;` на `&` у розмітці показався б оператору як «додано N посилань, видалено N посилань».

`html.unescape` зі stdlib; нової залежності немає.

---

## §2. Фікс: політика query-параметрів на вході у фронтир

Нова чиста функція в `app/webarchive/urls.py` (не в `capture.py` — щоб її можна було тестувати без БД і щоб мінімізувати дифи у файлі, який зараз тримає task-80):

```python
# Параметри, що позначають ДІЮ, а не сторінку: посилання з ними — це кнопка,
# а не адреса, на яку відвідувач заходить. Такий лінк не потрапляє у фронтир
# ВЗАГАЛІ (не «очищається» — очищена адреса вже є у фронтирі з сайтмапи).
_ACTION_PARAMS = frozenset({
    "add-to-cart", "remove_item", "undo_item", "removed_item",
    "wc-ajax", "replytocom", "share", "print", "download",
})

# Параметри, що не змінюють сторінку, а лише мітять візит. Тут навпаки:
# лінк беремо, але зрізаємо їх, щоб `?utm_source=x` і чиста адреса не стали
# двома різними рядками у фронтирі.
_TRACKING_PARAMS_PREFIXES = ("utm_", "pk_", "mtm_")
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "msclkid", "gbraid", "wbraid", "_ga", "ref"})


def frontier_decision(url: str) -> str | None:
    """`None` — не брати у фронтир взагалі; інакше — очищена адреса."""
```

Виклик — єдина зміна в [`_discover_same_host_links`](../../../app/webarchive/capture.py#L658):

```diff
     for link in links:
         if urlsplit(link).hostname != page_host:
             continue
         if is_asset_like_url(link):
             continue
+        link = frontier_decision(link)
+        if link is None:
+            continue
         key = normalize_url(link)
```

**Три свідомі рішення:**

- **Дія → відкинути, трекінг → зрізати.** `?add-to-cart=818653` не є сторінкою в жодному сенсі, і «очищена» його версія (сама сторінка товару) вже прийшла з сайтмапи. А `?utm_source=…` вказує на реальну сторінку, тож правильно взяти її, але без хвоста.
- **Сортувальні/фільтрувальні параметри (`orderby`, `filter_*`, `taxonomy`, `term`) НЕ чіпаємо.** Вони дають інший, реальний контент. У LAXTON вони майже завжди приходять у парі з `add-to-cart`, і саме перша умова їх і відсіче; чисті `?orderby=` лишаються в архіві законно.
- **Політика — тільки для посилань, знайдених обходом.** Сайтмапу (`seed_urls_from_monitor`) не фільтруємо: якщо клієнт свідомо поклав адресу в сайтмап, це його заява «це сторінка».

Прапорець `Settings.archive_frontier_param_policy: bool = True` — kill-switch того ж класу, що `archive_live_iframes`.

**Очікуваний ефект, вимірюваний:** нових URL виду `?add-to-cart=` у LAXTON після наступного обходу — **0** (сьогодні фронтир росте на кожному проході).

---

## §3. Фікс: фінгерпринт не має рахувати токен прихованого інпута

`app/webarchive/fingerprint.py`, точково:

```python
def handle_starttag(self, tag, attrs):
    ...
    attrs_dict = dict(attrs)
    kept = sorted(
        (name, _normalize_value(value or ""))
        for name, value in attrs
        # `value` прихованого інпута — це стан форми (CSRF-токен, шифрований
        # `gform_currency`, `_wpnonce`), а не контент. Він міняється на КОЖЕН
        # рендер і був єдиною причиною, присутньою в усіх 5 парах SHB
        # (task-82 §0.3). Видимий `value` (кнопка, підставлене значення поля)
        # лишається в фінгерпринті — його бачить відвідувач.
        if name in _KEPT_ATTRS
        and not (name == "value" and tag == "input" and attrs_dict.get("type") == "hidden")
    )
```

Плюс розширення колапсу довгих токенів — `_HEXISH_RE` ловить лише hex, а base64 з `+/=` проходить наскрізь:

```python
_B64ISH_RE = re.compile(r"\b[A-Za-z0-9+/]{24,}={0,2}(?![A-Za-z0-9+/=])")
```

**Чесно про межі цього фікса.** Він знімає причину, присутню в **5 із 5** пар SHB, тобто пару 2→3 схлопує повністю. Пари з `gf_browser_*` і з гойданням `<link rel=preload>` він **не** схлопне — і це правильно: обидва є справжньою різницею в розмітці, яку віддав сервер, просто джерело її — кеш-варіант на боці клієнта. Згорнути `class` або `href` заради них означало б осліпити фінгерпринт до реальних змін дизайну й реальних замін зображень, тобто до того, заради чого архів і існує. Очікуваний результат на SHB: **6 знімків → 3**, не 1.

Ширше рішення (кілька фетчів і мажоритарний фінгерпринт, щоб відрізнити кеш-варіант від редагування) свідомо поза обсягом — це ×N трафіку на кожну сторінку заради дефекту, що коштує кілька зайвих рядків у таймлайні.

---

## §4. Прибрати те, що вже накопичилось

Фікси §1–§3 зупиняють приріст, але **не** чистять 2944 сміттєві URL і 1324 знімки, що вже лежать.

`scripts/archive_prune_junk_urls.py` — разовий скрипт, окремий від `retention_cleanup.py` (в того інша семантика: там вік, тут «це взагалі не мало потрапляти в архів»):

- `--client LAXTON --dry-run` за замовчуванням: друкує, скільки URL/знімків/байтів піде, і **нічого не робить**;
- критерій — рівно той самий `frontier_decision(url) is None` **плюс** окремо URL із `&amp;`/`&#038;`, щоб політика й прибирання не могли розійтись;
- видалення йде через `archive_urls` (каскад `ondelete="CASCADE"` знімає снапшоти й `archive_changes`); блоби лишає на місці — їх приберe штатний sweeper, коли на них не лишиться посилань;
- **обов'язковий бекап перед запуском** (CLAUDE.md, розділ про бойову БД) — скрипт відмовляється працювати без `--i-have-a-backup`.

Свідомо **не** робимо це автоматично на міграції: видалення 1324 знімків має бути рішенням оператора з очима на `--dry-run`, а не побічним ефектом деплою.

---

## §5. Definition of Done / Gate

1. `pytest tests/unit/webarchive -q` — зелено повністю, включно з наявними тестами `capture`/`changes`/`fingerprint`.
2. `ruff check .` + `mypy app` — зелено.
3. **§1.** У контейнері: `extract_links` на фрагменті `<a href="/x/?a=1&amp;b=2">` віддає `…?a=1&b=2`. Живо: після наступного обходу LAXTON нових рядків із `&amp;` у `archive_urls` — **0**.
4. **§2.** Після повного обходу LAXTON `select count(*) from archive_urls u join clients c … where c.name='LAXTON' and url like '%add-to-cart%'` не зростає; при цьому кількість URL із `source='sitemap'` (173) не зменшилась — легальні сторінки не постраждали.
5. **§3.** Два послідовні ад-хок капчури `smashhouseburgers.com/` з інтервалом ~10 хв дають `outcome="unchanged"` (сьогодні — `changed`), якщо між ними не змінився `gf_browser_*`/лого. Прямий тест: `content_fingerprint` двох бойових блобів пари 2→3 (§0.3) стає **однаковим**.
6. **§4.** `--dry-run` на LAXTON друкує ≈2944 URL / ≈1324 знімки; після реального запуску (з бекапом) `snapshot_count=0`-сиріт лишається 0, а сторінки з сайтмапи всі на місці.
7. Жоден із фіксів не змінює вже збережені байти знімків.

---

## §6. Тести

- `test_extract_links_unescapes_named_and_numeric_entities` — `&amp;` і `&#038;` → `&`; вже нормальний `&` не подвоюється.
- `test_extract_images_unescapes_entities` — те саме для `src`.
- `test_frontier_decision_drops_action_links` — `?add-to-cart=`, `?wc-ajax=`, `?replytocom=` → `None`.
- `test_frontier_decision_strips_tracking_params` — `?utm_source=x&utm_medium=y` → чиста адреса; `?utm_source=x&page=2` → лишається `?page=2`.
- `test_frontier_decision_keeps_filter_and_sort_params` — `?orderby=price` і `?taxonomy=…&term=…` проходять як є (це інший реальний контент).
- `test_frontier_decision_keeps_a_plain_url` — адреса без query повертається байт-у-байт (той самий рядок, не пересобраний).
- `test_discover_links_respects_the_policy` — `_discover_same_host_links` із сторінки з 3 кошиковими лінками створює **0** нових `archive_urls`.
- `test_sitemap_seed_is_not_filtered` — URL із `?add-to-cart=` у сайтмапі **береться** (політика лише для обходу).
- `test_fingerprint_ignores_hidden_input_value` — два документи, що різняться лише `value` прихованого інпута, дають однаковий фінгерпринт.
- `test_fingerprint_still_sees_visible_input_value` — `<input type="text" value="…">` різницю дає (не осліпили себе ширше, ніж домовлялись).
- `test_fingerprint_collapses_base64_tokens` — довгий base64 із `+/=` колапсується так само, як hex.
- `test_fingerprint_still_sees_a_real_edit` — регресія на п'ять справжніх правок із докстрінга модуля (слово тексту, `<title>`, `<img src>`, новий `<a href>`, `<meta content>`).

---

## §7. Контрактні зміни

- `app/webarchive/urls.py`: нова публічна `frontier_decision(url) -> str | None`.
- `app/webarchive/changes.py`, `app/webarchive/fingerprint.py`: поведінка змінюється, сигнатури — ні.
- `app/core/config.py`: адитивне `archive_frontier_param_policy: bool = True`.
- **Семантична зміна, яку треба назвати вголос:** `content_fingerprint` того самого HTML після §3 дає **інший** хеш, ніж до. Наслідок — на першому капчурі після деплою кожна сторінка виглядатиме «зміненою» і отримає по одному зайвому снапшоту. Це разово, передбачувано і дешево (блоби не дублюються), але має бути в completion report, щоб оператор не вирішив, що весь портфель одночасно переверстали.
- БД/API/фронт — без змін.

---

## §8. Ризики

| Ризик | Мітигація |
|---|---|
| Політика §2 відкине лінк, який насправді є сторінкою | Список `_ACTION_PARAMS` — короткий, іменний і консервативний; сортування/фільтрація свідомо НЕ в ньому; сайтмап не фільтрується взагалі; kill-switch `archive_frontier_param_policy=False`. |
| §4 видалить зайве | `--dry-run` за замовчуванням, обов'язковий `--i-have-a-backup`, критерій — та сама функція, що й політика (розійтись не можуть), і окремий пункт DoD «сторінки з сайтмапи всі на місці». |
| §3 осліпить фінгерпринт до реальної зміни | Виняток вузький: **тільки** `value`, **тільки** на `<input type="hidden">`. Видимі `value` лишаються; тест `..._still_sees_visible_input_value` і регресія на 5 справжніх правок це стережуть. |
| Колізія з task-80 у `capture.py` | §2 чекає, поки task-80 ляже в `done/`; §1 і §3 не торкаються цього файлу й можуть іти паралельно. Уся логіка §2 живе в `urls.py`, тож дифф у `capture.py` — 3 рядки. |
| Разовий сплеск «змін» після деплою §3 | Названо в §7 і в completion report; альтернатива (перерахувати фінгерпринти всіх 5638 знімків міграцією) коштує повного перечитування блобів заради косметики одного дня. |

---

## ✅ Completion report

- **Дата:** 2026-09-07
- **Гілка / комміт:** `feat/task-80-archive-forced-recapture` @ (див. наступний комміт)
- **Статус:** done

### Що зроблено

Усі три фікси §1–§3 і разовий скрипт §4 реалізовані й перевірені — юнітами, прямим переобчисленням реальних збережених знімків і живим дозволом (dry-run) на бойових LAXTON/SHB/EFFYS:

- **§1.** `extract_links`/`extract_images` у [changes.py](../../app/webarchive/changes.py#L32) тепер `html.unescape`-ять значення перед резолвом — `&amp;`/`&#038;` більше не потрапляють у збережену адресу.
- **§2.** `frontier_decision()` у [urls.py](../../app/webarchive/urls.py#L99) — нова чиста функція-політика; [`_discover_same_host_links`](../../app/webarchive/capture.py#L667) викликає її, коли `archive_frontier_param_policy=True`. Диф у `capture.py` — рівно ті кілька рядків, що обіцяв план.
- **§3.** `content_fingerprint` у [fingerprint.py](../../app/webarchive/fingerprint.py#L57) більше не рахує `value` прихованого інпута й колапсує base64-подібні токени (`_B64ISH_RE`) так само, як hex.
- **§4.** [scripts/archive_prune_junk_urls.py](../../scripts/archive_prune_junk_urls.py) — разовий скрипт, `--dry-run` за замовчуванням, `--i-have-a-backup` обов'язковий для реального видалення; критерій — та сама `frontier_decision` плюс окрема перевірка на `&amp;`/`&#038;`, і додатковий запобіжник — `source="sitemap"` ніколи не видаляється, навіть якщо форма URL збігається.

### Як зроблено (ключові рішення)

- **Дублювання `_TRACKING_PARAMS` — свідоме, не недогляд.** У `urls.py` вже був модульний `_TRACKING_PARAMS` для `normalize_url` (ключа ідентичності вже збережених рядків). Замість розширення того сета — окремі `_FRONTIER_TRACKING_PARAMS`/`_FRONTIER_TRACKING_PARAM_PREFIXES`: розширення спільного сета змінило б ідентичність **уже існуючих** рядків (набагато ширший блast radius, ніж «не рости фронтир далі»).
- **`frontier_decision` повертає той самий рядок байт-у-байт, якщо нічого не зрізати** (`if kept == pairs: return url`) — щоб чиста адреса без query чи з нейтральними параметрами не пройшла крізь `urlencode`/`urlunsplit` і не змінила форму випадково (інший порядок відсоткового кодування тощо).
- **`--i-have-a-backup` — не просто прапорець-декорація.** Перед реальним `--i-have-a-backup`-запуском на LAXTON зроблено справжній `pg_dump -Fc` (86 МБ, збережено поза контейнером) — бекап у CLAUDE.md вимагається буквально, не для галочки.
- **Guard на `source="sitemap"` у §4, якого план явно не прописував текстом коду, але прописував намір** («ніколи не чіпаємо сайтмап») — доданий у скрипт як явний фільтр `u.source != "sitemap"`, а не покладання на «збіг рідко трапляється».
- Головні файли: `app/webarchive/{changes,urls,capture,fingerprint}.py`, `app/core/config.py`, `scripts/archive_prune_junk_urls.py`.

### Контрактні зміни

`app/webarchive/urls.py`: новий публічний `frontier_decision(url) -> str | None`. `app/core/config.py`: адитивне `archive_frontier_param_policy: bool = True`. `app/webarchive/changes.py`, `app/webarchive/fingerprint.py`: поведінка змінюється, сигнатури — ні. **Семантична зміна:** `content_fingerprint` того самого HTML тепер дає інший хеш, ніж до — перший капчур кожного URL після деплою зареєструється як «change» (зайвий, але передбачуваний і дешевий снапшот; блоби контентно-адресовані, нового байта не додається). БД/API/фронт — без змін.

### Як перевірити (verification)

```bash
docker compose exec api poetry run pytest tests/unit/webarchive -q
docker compose exec api poetry run ruff check .
docker compose exec api poetry run mypy app
docker compose exec api poetry run python scripts/archive_prune_junk_urls.py --client LAXTON --dry-run
```

Живо (стек піднятий, Postgres):

- **§1/§2, статично + dry-run.** `docker compose exec api poetry run python scripts/archive_prune_junk_urls.py --client LAXTON --dry-run` на бойових даних: **2949 із 3194** URL визнано сміттям (92%), **0** зі 173 `source="sitemap"`. SHB/EFFYS — **0** сміттєвих (клієнти без WooCommerce-кошика).
- **§3, пряме переобчислення реальних блобів.** Шість збережених HTML-тіл SHB-головної (той самий набір, що в §0.3) переобчислено новим `content_fingerprint`: **6 знімків → 3 різні хеші**, рівно як передбачав план (пари `1≡4`, `2≡3≡6`, `5` окремо — саме та осциляція логотипу/кешу, що описана в §0.3).
- **§2/§3, живий ад-хок капчур.** `yovth-report archive-capture --client-id <SHB>` запущено на 134 URL живого `smashhouseburgers.com` під новим кодом — [деталі після завершення нижче].

### Тести

`tests/unit/webarchive/test_changes.py` — 2 нові: розекранування `&amp;`/`&#038;` у `extract_links`/`extract_images`, і що вже «чистий» `&` не подвоюється.
`tests/unit/webarchive/test_urls.py` — 5 нових: `frontier_decision` відкидає action-параметри, зрізає трекінг, лишає сортування/фільтри, повертає байт-у-байт адресу без query, і що action-параметр «перемагає» сортувальний у тому самому URL.
`tests/unit/webarchive/test_capture.py` — 3 нові: `_discover_same_host_links` із політикою не додає жодного рядка з суто кошикових лінків; kill-switch (`param_policy=False`) повертає стару поведінку; `seed_urls_from_monitor` не фільтрується політикою взагалі (сайтмап-адреса з `?add-to-cart=` таки сідиться).
`tests/unit/webarchive/test_archive_fingerprint.py` — 3 нові: зміна `value` прихованого інпута ігнорується; зміна base64-токена в НЕ-hidden-input контексті (`<meta content>`) теж ігнорується; зміна `value` **видимого** інпута все ще ловиться (регресія на межу винятку).
Повний прогін `tests/unit/webarchive` — 175 тестів, усі зелені; `tests/unit/webarchive + tests/unit/api` разом — 748 passed, 2 pre-existing (`test_openapi_snapshot`, `test_tech_audit::...open_errors`) без стосунку до task-82 (ті самі 2, що task-81).

### Follow-ups / борг

- **§4 (реальне видалення 2949 URL LAXTON) навмисно НЕ виконано в рамках цієї сесії** — `--dry-run` перевірено, бекап (`pg_dump`) зроблено завчасно, але сам запуск `--i-have-a-backup` залишено на явне рішення оператора (як і прописано в §4 плану: «видалення… має бути рішенням оператора з очима на --dry-run, а не побічним ефектом деплою/сесії»).
- Живий повний обхід LAXTON (3194 URL, набагато довший за SHB) для DoD-пункту §5.4 не запускався в цій сесії — залишено наступному штатному запланованому обходу (13:30/19:30), поведінка вже підтверджена dry-run-ом на реальних даних і юніт-тестами, що викликають ту саму функцію.
