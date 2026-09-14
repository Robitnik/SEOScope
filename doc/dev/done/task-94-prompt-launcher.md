# task-94 — Prompt Launcher: таблиця клієнтів + кнопка «скопіювати заповнений промт»

- **Пакет:** Tools hub (новий тул, `#/tools/prompt-launcher`)
- **Depends on:** task-27 ✅ (хаб інструментів, каталог `GET /tools`), task-06 ✅ (REST),
  task-71 ✅ (ролі), task-04 ✅ (клієнти/інтеграції)
- **Owned paths:**
  `app/prompts/__init__.py`, `app/prompts/registry.py`, `app/prompts/render.py`,
  `app/api/tools_prompts.py`, `app/schemas/prompts.py`,
  `app/api/tools.py` (**один** запис у каталозі — адитивно),
  `app/api/__init__.py` (**один** рядок реєстрації роутера — адитивно),
  `app/core/config.py` (**одне** поле `aiseotools_host_path` — адитивно),
  `frontend/js/prompt-launcher-tool.js`, `frontend/js/tools-page.js` (один запис реєстру),
  `frontend/js/api.js` (дві функції — адитивно), `frontend/css/prompt-launcher.css`,
  `frontend/index.html` (один `<link>` + cache-bust),
  `tests/unit/prompts/test_registry.py`, `tests/unit/api/test_tools_prompts.py`,
  `tests/e2e/frontend/tools-prompt-launcher.cjs`
- **Міграцій БД:** немає (див. §7).

---

## Мета (робота оператора, яку знімаємо)

Щопонеділка оператор запускає TechAudit для **14 клієнтів**. Кожен запуск — це та сама
ручна операція: згадати кодове ім'я клієнта в AISeoTools, згадати його домен, вписати
обидва в шапку промта, скопіювати, вставити в новий чат агента. Чотирнадцять разів,
з клавіатури, з єдиним джерелом помилки — переплутаний або застарілий домен, який
потім тихо аудитує не той сайт.

Тул не запускає аудит і не замінює агента. Він робить рівно одне: **дає таблицю
клієнтів і кнопку, яка кладе в буфер готовий, уже заповнений промт-лаунчер для
конкретного клієнта**, плюс тримає відмітку, кого на цьому тижні вже взяли в роботу.

Форма, яку бачить оператор:

```
Prompt:  [ TechAudit ▾ ]   docs/TechAudit.md · updated 2026-09-12     7 of 14 copied this week  [Reset]
─────────────────────────────────────────────────────────────────────────────────────────────
   Client            CLIENT        URL                                   Copied        Action
   Shoreline …       SHB           https://shorelinedentalstudio.com     10:14         [Copy] ✓
   Alpha Dental      ALPHA         https://alphadental.com               —             [Copy]
   …                 —             (no domain)                           —             [Copy] (disabled)
```

---

## Дослідження (заміряно 2026-09-14, не з документації)

1. **Плейсхолдери в `docs/*.md` — здебільшого ВИХІДНІ, не вхідні.** У `TechAudit.md`
   дванадцять `{{…}}`, і лише два з них (`{{CLIENT}}`, `{{URL}}`) оператор заповнює
   перед запуском; решта (`{{XLSX_PATH}}`, `{{PEAK}}`, `{{TABS}}`, `{{DDMMYYYY}}`…) —
   це шаблон **фінального звіту агента**. Автоматичне витягання плейсхолдерів із файлу
   дало б форму з десятком порожніх полів, з яких 10 ніхто не заповнює. **Вхідні поля
   оголошуються в реєстрі явно**, файл промта ніколи не парситься на предмет полів.
2. **У репо вже 25 промтів** (`AhrefsAudit.md`, `GSC_Audit.md`, `PageStrategy.md`,
   `RankMovement.md`, …), і майже всі мають ту саму пару вхідних полів «клієнт + домен».
   Тому реєстр шаблонів, а не тул під один TechAudit: другий щотижневий промт має
   коштувати один запис у кортежі.
3. **AISeoTools примонтований у контейнер read-only:**
   `${AISEOTOOLS_PATH:-../../seo/AISeoTools}:/seo/AISeoTools:ro` (compose.yaml:131/206), а
   `Settings.aiseotools_source_path = /seo/AISeoTools` ([config.py:34](app/core/config.py#L34)).
   Тобто бек може читати файл промта, але **шлях у контейнері ≠ шлях, яким користується
   агент**: Claude Code працює на хості, у `/home/elon/dev/seo/AISeoTools`. Лаунчер
   мусить нести **хостовий** шлях — інакше він посилається на директорію, якої в
   агента немає. Звідси адитивна настройка `aiseotools_host_path` (§1.3).
4. **Кодове ім'я клієнта в AISeoTools вже зберігається** — у
   `client_integrations.config_json`: `client_label` для `tech_audit`/`blogs`,
   `ahrefs_client_label`, `semrush_client_label`
   ([tech_audit.py:203](app/integrations/tech_audit.py#L203),
   [ahrefs.py:290](app/integrations/ahrefs.py#L290)). Окремої колонки «код клієнта» в
   `clients` немає і ця таска її не заводить.
5. **Тул читає й нічого не мутує**, тож усі три ролі (`admin`/`member`/`viewer`) працюють
   із ним однаково: `required_role` для safe-методів поза allowlist'ами повертає `viewer`
   ([policy.py:115](app/core/auth/policy.py#L115)). Admin-гейтів тут не треба.

---

## Рішення (одним абзацом)

Бек тримає **реєстр шаблонів у коді**: ключ, назва, шлях до файлу промта всередині
AISeoTools, текст лаунчера й перелік **вхідних** полів. Значення полів резолвляться з
БД для кожного клієнта, лаунчер **рендериться на беку** й приходить на фронт готовим
рядком — фронт ніколи не тримає копію шаблону і не вміє його склеювати. Фронт —
таблиця з кнопкою `Copy`, відмітка скопійованого живе в `localStorage` і сама
скидається на новому ISO-тижні.

---

## 1. Реєстр шаблонів — `app/prompts/registry.py`

### 1.1 Запис

```python
@dataclass(frozen=True)
class PromptField:
    key: str                  # "CLIENT" — ім'я плейсхолдера без дужок
    label: str                # "CLIENT" (UI-колонка, англійською)
    source: FieldSource       # як резолвиться (§2)

@dataclass(frozen=True)
class PromptTemplate:
    key: str                  # "techaudit" — у URL і в localStorage
    name: str                 # "TechAudit" (UI)
    summary: str              # один рядок англійською, що цей промт робить
    doc_relpath: str          # "docs/TechAudit.md" — ВІДНОСНО кореня AISeoTools
    launcher: str             # текст із {{…}} (§1.2)
    fields: tuple[PromptField, ...]
```

Перший (і поки єдиний) запис — `techaudit`. Додавання `AhrefsAudit`/`GSC_Audit` —
свідомий follow-up (§10), щоб ця таска закрилась на реальному понеділковому кейсі.

### 1.2 Текст лаунчера

```
Read {{DOC_PATH}} and run it end to end.

MODE:    tech
CLIENT:  {{CLIENT}}
URL:     {{URL}}
```

`{{DOC_PATH}}` — не поле оператора: його підставляє рендерер із
`aiseotools_host_path / doc_relpath`. Блок `MODE/CLIENT/URL` узятий дослівно з шапки
`docs/TechAudit.md`, бо це формат, який промт сам про себе оголошує.

**Чому лаунчер, а не повний текст промта (рішення користувача):** повний текст — 438
рядків, які застаріють у момент, коли AISeoTools оновить `TechAudit.md`. Лаунчер
посилається на файл, тож джерелом істини лишається AISeoTools, а тул ніколи не
роздає стару версію інструкції.

### 1.3 Настройка `aiseotools_host_path`

Адитивне поле в `Settings` (`app/core/config.py`), дефолт — `/home/elon/dev/seo/AISeoTools`,
перекривається змінною оточення. Це **рядок для тексту, не файловий доступ**: бек за
цим шляхом нічого не відкриває (його в контейнері не існує), він лише підставляється в
лаунчер. Читання файлу йде виключно через `aiseotools_source_path` (§3.3).

---

## 2. Резолвер значень — `app/prompts/render.py`

`FieldSource` — маленький замкнений перелік, кожне значення бере дані з уже наявних
полів, нічого не вигадуючи:

| source | звідки | якщо немає |
|---|---|---|
| `client_label` | `client_integrations.config_json["client_label"]` для `tech_audit`, далі `ahrefs_client_label`, далі `client.name` | значення завжди є (`name` обов'язковий) |
| `site_url` | `https://{client.domain}` | `domain` порожній → поле `None`, рядок **непридатний** (§6) |

**Інваріант:** відсутнє значення ніколи не підмінюється порожнім рядком і ніколи не
дає лаунчер із діркою. Рядок без домену віддається з `rendered: null` і причиною —
кнопка `Copy` вимкнена. Скопійований промт із порожнім `URL:` запустив би краул не по
тому сайту, і це саме той клас помилки, який тул мав прибрати.

Порядок рядків: `ClientRepository.list_collectable()` (активні, не-paused, з увімкненим
збором) за алфавітом. Перемикач `Show paused and dev clients` дорисовує решту з
`list_all()` — окремою групою з бейджем, а не змішаною в основний список.

---

## 3. REST — `app/api/tools_prompts.py`, префікс `/api/v1/tools/prompts`

### 3.1 `GET /api/v1/tools/prompts`

```json
{"templates": [{
  "key": "techaudit", "name": "TechAudit", "summary": "…",
  "doc_relpath": "docs/TechAudit.md",
  "doc_host_path": "/home/elon/dev/seo/AISeoTools/docs/TechAudit.md",
  "doc_available": true, "doc_updated_at": "2026-09-12T08:41:02Z", "doc_bytes": 24817,
  "fields": [{"key": "CLIENT", "label": "CLIENT"}, {"key": "URL", "label": "URL"}]
}]}
```

`doc_available: false` (файл не примонтований / перейменований) — не помилка запиту:
шаблон приходить, але з `doc_available=false`, і фронт показує попередження та вимикає
копіювання для всього шаблону. Лаунчер, що посилається на неіснуючий файл, гірший за
чесну відмову.

### 3.2 `GET /api/v1/tools/prompts/{key}/clients`

```json
{"template_key": "techaudit", "generated_at": "…",
 "rows": [{
   "client_id": "…", "client_name": "Shoreline Dental Studio", "slug": "shoreline",
   "status": "active", "is_dev": false,
   "values": {"CLIENT": "SHB", "URL": "https://shorelinedentalstudio.com"},
   "rendered": "Read /home/…/docs/TechAudit.md and run it end to end.\n\nMODE:    tech\nCLIENT:  SHB\nURL:     https://shorelinedentalstudio.com\n",
   "blocked_reason": null
 }]}
```

`rendered` приходить **готовим** — фронт кладе в буфер рівно цей рядок. `blocked_reason`
(`"client has no domain"`) заповнений ⇔ `rendered` є `null`; обидва поля разом, щоб UI
не вгадував причину з порожнього значення. Невідомий `key` → `404` з `ErrorDTO`.

### 3.3 `GET /api/v1/tools/prompts/{key}/source`

Повертає текст файлу промта (`{"text": "…", "truncated": false}`) для панелі перегляду
(§4.3). Шлях **не приходить від клієнта** — лише `key` з реєстру; резолвиться як
`(aiseotools_source_path / template.doc_relpath).resolve()` і **перевіряється на
належність кореню** (той самий захист, що в `tools_strategy_package.py::PathTraversalError`),
навіть попри те, що вхід уже замкнений реєстром. Ліміт 512 КБ, більше — `truncated: true`.

Реєстрація роутера — один рядок у `registered_routers()` ([api/__init__.py:19](app/api/__init__.py#L19)).

---

## 4. Фронтенд — `#/tools/prompt-launcher`

Обов'язково за `frontend/AGENTS.md`. Ключові вимоги, які тут реально кусають:

- **CSP:** жодних інлайнових `style=` і жодних JS-присвоєнь `.style.*` — стани тільки
  класами (`frontend/css/prompt-launcher.css`).
- **Cache-bust ланцюг:** новий `?v=` на `prompt-launcher-tool.js`, і разом із ним —
  на `tools-page.js` (реєстр), `api.js`, та на тезі в `index.html`, інакше тунель віддасть
  стару копію (пам'ять: ланцюг бастингу ES-імпортів).
- **Таблиця** — у `overflow-x:auto`-обгортці з `contain: paint`, інакше широка таблиця
  скролить усю сторінку.
- **api.js:** `request-key` для обох запитів — явні назви призначення
  (`"prompt-templates"`, `"prompt-clients"`), не похідні від параметрів.

### 4.1 Каркас

1. Селектор шаблону (`<select>`), поки один варіант — але вже селектор, щоб другий
   промт не вимагав переверстки.
2. Шапка шаблону: назва, `summary`, шлях до файлу, «updated <дата>», лічильник
   `N of M copied this week`, кнопка `Reset`.
3. Таблиця: `Client` · по колонці на кожне поле шаблону (`CLIENT`, `URL`) · `Copied` · `Copy`.
4. Панель `Prompt source` (згорнута) — §4.3.

### 4.2 Копіювання

`navigator.clipboard.writeText(row.rendered)`; успіх → кнопка на 2 с стає `Copied`,
рядок отримує клас `is-copied`, лічильник зростає. Провал (браузер без дозволу) →
показуємо `<textarea>` з текстом, уже виділеним, і підказку «Press ⌘/Ctrl+C» —
той самий fallback, що вже є в `char-counter-tool.js::pasteFromClipboard`.

### 4.3 Перегляд джерела

Згорнута панель із `GET …/source` — щоб оператор міг перечитати сам промт, не виходячи
з тула. Завантажується **лише при розгортанні** (438 рядків на кожен вхід у тул — зайвий
трафік на кожен понеділок).

---

## 5. Прогрес тижня — `localStorage`, без БД

Ключ: `yovth.prompt-launcher.<template_key>` → `{"week": "2026-W38", "clients": {"<id>": "2026-09-14T10:14:33Z"}}`.

- `week` — ISO-тиждень **за таймзоною браузера**. При завантаженні тула значення
  порівнюється з поточним: інший тиждень → запис скидається мовчки. Понеділок сам
  чистить дошку, руками нічого не треба.
- Читання/запис у `try/catch`: приватне вікно або заблокований storage не має ламати
  таблицю — тул просто працює без відміток.
- `Reset` чистить запис поточного шаблону і перемальовує лічильник.

**Чому не в БД (рішення користувача):** відмітка «я вже скопіював» — це стан однієї
людини за один ранок, а не факт про клієнта. Серверний чек-лист вимагав би міграції
(лише task-03) і породив би питання «хто зняв галочку» — рівно нуль користі за цю ціну.

---

## 6. Помилкові й порожні стани (жоден не мовчазний)

| Стан | Що бачить оператор |
|---|---|
| Клієнт без `domain` | Клітинка `URL` — «no domain», кнопка `Copy` вимкнена з `title`-поясненням |
| Файл промта не знайдено (`doc_available=false`) | Банер «Prompt file is not available at …», уся таблиця read-only |
| Порожній список клієнтів | «No collectable clients» + посилання на `#/clients` (не порожня таблиця) |
| `GET` впав | Стандартний `state-panel state-failed` + `Try again` (як у хабі тулів) |
| Clipboard заборонено | Fallback-`<textarea>` (§4.2), ніколи не «нічого не сталось» |

---

## 7. Чого ця таска НЕ робить

- **Не створює міграцій** і не додає колонку «код клієнта» в `clients` — код береться з
  наявного `config_json` (§2).
- **Не запускає аудит.** Запуск TechAudit із дашборду — це вже `app/api/tech_audit.py`
  (task-25), інша, машинна гілка без AI-оверлея. Тул віддає промт у буфер, і на цьому
  його відповідальність закінчується.
- **Не редагує промти в UI.** Джерело істини — файли в AISeoTools.
- **Не тримає історії копіювань** (§5).

---

## 8. Тести

- `tests/unit/prompts/test_registry.py` — рендер лаунчера з підставленими полями;
  `client_label` береться з `tech_audit`, потім з `ahrefs_*`, потім `client.name`;
  клієнт без домену дає `rendered=None` + `blocked_reason` (і **не** дає рядок із
  порожнім `URL:`); `DOC_PATH` бере хостовий шлях, а не контейнерний.
- `tests/unit/api/test_tools_prompts.py` — форма трьох відповідей; `404` на невідомий
  ключ; `doc_available=false`, коли файлу немає; `/source` не виходить за корінь
  AISeoTools; усі три ролі отримують `200` (`@pytest.mark.auth`).
- `tests/e2e/frontend/tools-prompt-launcher.cjs` (Node Playwright, `.cjs`/`require`) —
  тул відкривається з хаба, таблиця має рядки, клік по `Copy` пише в буфер саме
  `rendered`, відмітка переживає reload, `Reset` її чистить. **Пастка з пам'яті:**
  `127.0.0.1` під localhost-байпасом завжди автентифікує адміна — рольові перевірки
  роблять на беку (юніт), не через Playwright.

Прогін — тільки в Docker:
`docker compose exec api poetry run pytest tests/unit/prompts tests/unit/api/test_tools_prompts.py -q`,
`docker compose exec api poetry run ruff check .`, `docker compose exec api poetry run mypy app`.

---

## 9. Definition of Done / Gate

1. `GET /api/v1/tools/prompts` віддає `techaudit` з `doc_available=true` на живому стеку.
2. `GET /api/v1/tools/prompts/techaudit/clients` віддає рядок на кожного collectable-клієнта,
   у кожного заповнені `CLIENT` і `URL` **або** проставлений `blocked_reason`.
3. Картка `Prompt Launcher` є в хабі `#/tools` і шукається пошуком по слову «prompt».
4. У браузері: клік `Copy` кладе в буфер текст, який **без жодного редагування**
   запускає TechAudit для цього клієнта — перевірено живою вставкою в агента щонайменше
   для одного клієнта.
5. Відмітки переживають reload і зникають самі на новому ISO-тижні (перевірити підміною
   збереженого `week`).
6. Клієнт без домену не дає скопіювати порожній промт.
7. Lint + mypy + `pytest` модуля зелені **в контейнері**; фронт-чекліст `AGENTS.md §15–16`
   пройдено (CSP, теми, 400px, клавіатура).
8. План перенесено в `doc/dev/done/` з Completion report, рядок у `INDEX.md` позначено ✅.

---

## 10. Follow-ups

- Додати в реєстр решту щотижневих промтів (`AhrefsAudit`, `GSC_Audit`, `RankMovement`) —
  по одному запису, без нового коду. `TODO(task-94-follow-up)`.
- «Copy remaining as a list» — один блок із лаунчерами всіх ще не скопійованих клієнтів,
  якщо понеділковий ритуал виявиться зручнішим пачкою.
- Поле-редактор для шаблонів із вільними полями (`PageFramework.md` має `{{PRIMARY_KEYWORD}}`
  тощо) — окрема форма над таблицею; поза обсягом, поки всі потрібні промти мають лише
  «клієнт + домен».

---

## ✅ Completion report

- **Дата:** 2026-09-14
- **Гілка / комміт:** feat/task-93-authorship @ fed872d (ця таска додана поверх, окремим коммітом; не чіпала жодного файлу з owned paths task-93)
- **Статус:** done

### Що зроблено

`#/tools/prompt-launcher` — новий тул у хабі: селектор шаблону промта, таблиця
реальних клієнтів (11 collectable + 4 paused/hidden за перемикачем на живому
стенді), кнопка `Copy` кладе в буфер точний, готовий до вставки лаунчер, і
лічильник «N of M copied this week» з `Reset`. Панель `Prompt source`
довантажує реальний текст `docs/TechAudit.md` лише при розгортанні. Клієнт
без домену (`blocked_reason`) отримує вимкнену кнопку з поясненням у `title`
замість промта з порожнім `URL:`.

### Як зроблено (ключові рішення)

- **Реєстр шаблонів у коді** (`app/prompts/registry.py`) з одним записом
  `techaudit` — вхідні поля (`CLIENT`, `URL`) оголошені явно, а не витягнуті
  парсингом `{{…}}` з файлу: дослідження показало, що з 12 плейсхолдерів
  `TechAudit.md` вхідних лише 2, решта належить шаблону фінального звіту
  агента.
- **Резолвер значень** (`app/prompts/render.py`) — `CLIENT` бере
  `client_integrations.config_json["client_label"]` (`tech_audit`, потім
  `ahrefs_client_label`, потім `Client.name`); `URL` — `https://{domain}`.
  Якщо хоч одне поле не резолвилось — `rendered=None` + `blocked_reason`,
  ніколи проміжний рядок з діркою (PLAN §12).
- **Хостовий шлях окремо від контейнерного**: нове поле
  `Settings.aiseotools_host_path` (дефолт `/home/elon/dev/seo/AISeoTools`) —
  суто текстове значення для лаунчера; читання файлу (наявність/дата/розмір/
  вміст) завжди йде через наявний `aiseotools_source_path` (`/seo/AISeoTools`
  у контейнері, read-only). Без цього поля лаунчер посилався б на шлях,
  якого в агента (він працює на хості) немає.
- **Один REST-роутер, три read-only ендпоінти** (`app/api/tools_prompts.py`,
  автозареєстрований через `pkgutil` у `app/main.py` — рядок у
  `app/api/__init__.py` не знадобився, реєстрація вже автоматична для будь-
  якого нового файлу в `app/api/`): `GET /tools/prompts`,
  `GET /tools/prompts/{key}/clients`, `GET /tools/prompts/{key}/source`.
  `/source` іде через `safe_resolve`/`PathTraversalError`
  (`app/core/security.py`) навіть попри те, що `key` вже замкнений реєстром.
- **Каталог повертає ВСІХ клієнтів** (`ClientRepository.list_all()`), не
  лише collectable — кожен рядок несе прапорець `collectable` (`enabled AND
  status==active`) і `enabled`, фронт групує сам НЕ дериваючи семантику
  (PLAN §12): це відхилення від початкового чернетки плану (яка описувала
  окремий "дозавантажити paused" виклик) на користь простішого одного
  запиту + групування на фронті.
- **Прогрес тижня — лише `localStorage`**, ISO-тиждень рахується за
  **локальними** компонентами дати браузера (не UTC-нормалізацію
  підручника), бо суть — «який тиждень бачить оператор сьогодні».
- Головні файли: `app/prompts/{registry,render}.py` — джерело істини для
  шаблону й резолву; `app/api/tools_prompts.py` — REST;
  `frontend/js/prompt-launcher-tool.js` — уся клієнтська логіка (без API-
  фікстур, як і `schema-markup-tool.js`); `frontend/css/prompt-launcher.css`
  — лише наявні токени з `tokens.css`.
- Кеш-бастинг: `?v=20260914-t94` на нових/змінених файлах і на всьому
  ланцюжку, що на них посилається (`index.html` → `router.js` →
  `tools-page.js` → `prompt-launcher-tool.js`); `api.js`/`client-status.js`
  не бастилися цілком (адитивні зміни, старі імпортери й далі працюють на
  своїх тегах — підтверджено на прикладі task-90's комміту, що чіпав лише
  api.js без зачіпання 38 його імпортерів).

### Контрактні зміни (Contract changes)

Новий, суто адитивний контракт — нічого існуючого не змінено:

- `GET /api/v1/tools/prompts` → `PromptTemplatesDTO`
- `GET /api/v1/tools/prompts/{key}/clients` → `PromptClientsDTO`
  (`PromptClientRowDTO`: `client_id`, `client_name`, `slug`, `status`,
  `enabled`, `is_dev`, `collectable`, `values`, `rendered`,
  `blocked_reason`)
- `GET /api/v1/tools/prompts/{key}/source` → `PromptSourceDTO`
- Новий каталожний запис `prompt-launcher` у `GET /tools`
  (`app/api/tools.py`)
- Нове поле налаштувань `Settings.aiseotools_host_path` (env
  `AISEOTOOLS_HOST_PATH`)
- `tests/unit/api/data/openapi_snapshot.json` перегенеровано (355 рядків
  додано, 0 видалено — перевірено `git diff --stat`)

### Як перевірити (verification)

```bash
docker compose exec api poetry run ruff check app/prompts app/api/tools_prompts.py app/schemas/prompts.py app/api/tools.py app/core/config.py
docker compose exec api poetry run mypy app/prompts app/api/tools_prompts.py app/schemas/prompts.py app/api/tools.py app/core/config.py
docker compose exec api poetry run pytest tests/unit/prompts tests/unit/api/test_tools_prompts.py tests/unit/api/test_tools_catalog.py tests/unit/api/test_openapi_snapshot.py -q
```

E2E (host, Node — не в Docker, `frontend_verification_tooling` конвенція):

```bash
mkdir -p /tmp/yovth-e2e && cd /tmp/yovth-e2e && npm install --silent --no-save playwright-core
NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 CHROMIUM_PATH=/snap/bin/chromium \
  node tests/e2e/frontend/tools-prompt-launcher.cjs
```

Живо прогнано проти реального стенду (`docker compose restart api` після додавання коду):
`GET /api/v1/tools/prompts` і `.../techaudit/clients` відповіли реальними
даними (11 collectable + 4 paused/dev клієнтів), UI відкрився з хаба за
пошуком «prompt», `Copy` поклав у буфер точний рендер, позначка пережила
reload, `Reset` її прибрав, панель джерела довантажила реальний
`TechAudit.md`, мобільна ширина 390px не дає горизонтального скролу
сторінки (таблиця скролиться сама, `contain: paint`).

### Тести

- `tests/unit/prompts/test_registry.py` (5 тестів): резолв `CLIENT` за
  пріоритетом `tech_audit → ahrefs → name`; клієнт без домену блокує лише
  `URL`, лишає `CLIENT` резолвленим; невідомий ключ шаблону.
- `tests/unit/api/test_tools_prompts.py` (9 тестів, один `@pytest.mark.auth`):
  форма всіх трьох відповідей, `doc_available` true/false, 404 на невідомий
  `key` (усі три ендпоінти), `list_all()` + `collectable`-прапорець на
  реальних paused/disabled клієнтах, усі три ролі отримують 200.
- `tests/e2e/frontend/tools-prompt-launcher.cjs` — живий браузерний прогін
  проти реального стека й реальних клієнтів (сценарій у §"Як перевірити").
  Живий дефект, знайдений і виправлений під час написання самого тесту:
  `flashCopied` тимчасово вимикає щойно клікнуту кнопку на 1.6с, тож
  `.pl-copy-btn:not([disabled])` одразу після кліку резолвиться в ІНШИЙ
  рядок — тест адресує рядок за стабільним `data-client-id`, а не
  переоцінює «перша активна кнопка» після взаємодії.

### Follow-ups / борг

- Другий щотижневий шаблон (`AhrefsAudit`/`GSC_Audit`/`RankMovement`) —
  один запис у `TEMPLATES`, без нового коду (§10 плану вище).
- «Copy remaining as a list» — не запитувалось, залишено на майбутнє
  (§10).
- Поле-редактор для шаблонів із вільними полями — поза обсягом (§10).
