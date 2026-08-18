# task-41 — Site Monitor: чесні алерти (sitemap fetch-failure ≠ lost), www/non-www, diff-лінк зі стрічки змін

- **Пакет:** три дефекти, знайдені живим переглядом клієнта **hearwellservices.com** 2026-08-14: (1) `sitemap_lost` (critical) відкривався на разовому фетч-збої, не на підтвердженому 404 — «Sitemap lost» вночі 13.08, саморозв'язалось наступного прогону 14.08; (2) немає збереженого канонічного www/non-www хоста клієнта, тож редіректи в стрічці читаються неоднозначно; (3) рядки зміни `content_changed`/`title_changed`/... у стрічці змін — глухий кут: без `old_value`/`new_value` і без лінку на вже наявний diff-переглядач (task-36).
- **Хвиля:** M — спостережуваність сайтів (продовження task-35/36/37/38).
- **Depends on:** **35** ✅, **36** ✅, **37** ✅, **38** ✅
- **PLAN.md:** §12 (чесні стани — `None`/відсутність даних ≠ `0`, ніколи не підміняти нулем/помилкою)
- **frontend/AGENTS.md:** §5 (правда даних), §6 (стани)
- **AISeoTools:** змін не потребує

---

## 1. `sitemap_lost` vs `sitemap_unavailable`

### Першопричина

[app/monitor/runner.py](../../../app/monitor/runner.py) (`_process_sitemaps`) конфлатував у `missing = doc.kind == "unknown"` **чотири різні причини**: справжній 404, timeout, connection error, WAF-блок і навіть 200-відповідь, що не розпарсилась як XML — усе це йшло в [diff_sitemap_resource](../../../app/monitor/diff.py#L276) як `new_status=None`, а звідти — прямо в `sitemap_lost` (**critical**), без жодного підтвердження.

Порівняй з `app/monitor/dns.py`: DNS свідомо розділяє «резолвер не відповів» (`dns_unavailable`, warning, «never silently treated as the records are gone») від справжньої зміни записів. У sitemap такого поділу не було взагалі.

Живий патерн у клієнта hearwellservices.com: `/sitemap.xml` редіректить на реальний sitemap плагіна (нормально, так на всіх сайтах) — один разовий фетч-збій на цьому редіректі відкрив **Critical**, який «розв'язався сам» на наступному запланованому прогоні ~12 год потому.

### Виправлення

- `diff_sitemap_resource` тепер приймає `confirmed_gone: bool` — `True` лише коли сервер сам відповів 404. Будь-яка інша причина відсутності (`new_status is None` без підтвердженого 404) → `sitemap_unavailable` (**warning**, self-healing), а не `sitemap_lost`.
- Новий severity-запис `"sitemap_unavailable": WARNING` ([app/monitor/severity.py](../../../app/monitor/severity.py)), новий tracked-тип у `_SITEMAP_RESOURCE_TRACKED` ([app/monitor/runner.py](../../../app/monitor/runner.py)).
- Лейбл на фронті: `"Sitemap could not be checked"` ([frontend/js/formatters.js](../../../frontend/js/formatters.js)) — той самий стиль, що й `dns_unavailable`.

## 2. Канонічний www/non-www

### Проблема

`_base_url` будував `https://{client.domain}` напряму — жодного збереженого сигналу, чи сайт канонічно на `www.`, чи ні. Кожен запит транзитом ішов через зайвий редірект-хоп, а операторові в стрічці змін неможливо було відрізнити «це нормальна канонікалізація» від «щось зламалось».

### Виправлення

- Нове поле `MonitorSettings.canonical_www: bool | None` (`NULL` = невідомо — поведінка як і раніше цього поля) — [migrations/versions/5c3b4a7bda87_monitor_settings_canonical_www.py](../../../migrations/versions/5c3b4a7bda87_monitor_settings_canonical_www.py).
- `_base_url(client, override, canonical_www)` бере правильний хост напряму, якщо оператор його задав; `override` (`base_url_override`) як і раніше має пріоритет.
- `PATCH /clients/{id}/monitor/settings` приймає `canonical_www` (у т.ч. явне обнулення — додано в allow-list поруч із `base_url_override`).
- Бейдж «Canonical host: …» у шапці клієнта, якщо значення задано ([frontend/js/monitor-client.js](../../../frontend/js/monitor-client.js), [frontend/css/client-page.css](../../../frontend/css/client-page.css)).
- **Свідомо не зроблено:** автовизначення значення з фактичного редіректу — поле лишається операторським (сеттеться через API, форми в UI немає, за тим самим прецедентом, що й досі не заведена форма для `base_url_override`). Задокументовано як `TODO(task-42)` — розумний наступний крок, не потрібний для усунення поточної плутанини.

## 3. Diff-лінк зі стрічки змін

### Проблема

`MonitorChange.related_id` існував у моделі від task-35 («a plain, non-FK pointer... purely a 'show me more' link»), але **ніколи не заповнювався**. Рядки `content_changed`/`title_changed`/... у стрічці змін не несли `old_value`/`new_value` (диф контенту — це unified diff, не короткий рядок) і не мали способу дістатись до вже наявного page-diff-переглядача (task-36) — глухий кут, саме той рядок «Page content changed», на який показував користувач.

### Виправлення

- `DetectedChange.related_id` (нове поле, `dataclasses.replace` після діффа — сам `diff.py` рядків БД не знає).
- `runner.py` проставляє `related_id = page.id` для page-scope змін і `resource.id` для sitemap-resource змін.
- `MonitorChangeDTO.related_id` — прокинуто в API.
- Стрічка змін: кнопка **View diff** на рядках `scope="page"` з `related_id` → той самий `page-open`/diff-переглядач, що вже є в таблиці Pages.

---

## Definition of Done

- [x] `pytest` модуля зелений (`tests/unit/monitor`, `tests/unit/api/test_monitor_api.py`, `tests/integration/test_monitor_end_to_end.py`) — у Docker.
- [x] `ruff check` / `mypy app` — чисто на змінених файлах.
- [x] Міграція лінійна (`alembic heads` — один head), застосовується на чистій БД.
- [x] OpenAPI snapshot оновлено й проходить.
- [x] Фронт-тести (`node tests/unit/frontend/*.test.mjs`) зелені без правок.

---

## ✅ Completion report

- **Дата:** 2026-08-14
- **Гілка / комміт:** task-34-client-lifecycle-status (робота велась поверх; не конфліктує з owned paths task-34)
- **Статус:** done

### Що зроблено

Усі три пункти пакета реалізовано й покрито тестами (див. вище). Живий кейс hearwellservices.com («Sitemap lost» на разовому фетч-збої) більше не відтворюється: тест `tests/unit/monitor/test_sitemap_availability.py::test_fetch_failure_opens_sitemap_unavailable_warning_not_lost` — пряма регресія на цей сценарій.

### Як зроблено (ключові рішення)

- `sitemap_unavailable` — точна калька принципу `dns_unavailable`, а не нова абстракція: той самий severity-рівень, той самий self-healing-механізм через `_reconcile`.
- `canonical_www` — навмисно **операторське** поле, без автовизначення й без нової форми в UI (немає її і в `base_url_override`) — мінімальна зміна, що вирішує саме названу плутанину.
- `related_id` — не нова колонка (вона вже існувала в `MonitorChange` з task-35, просто не заповнювалась) — нуль контрактних змін на рівні БД для цієї частини.
- Головні файли: `app/monitor/{diff,runner,severity}.py`, `app/db/models.py` (+`canonical_www`), `app/db/repositories/monitor.py`, `app/schemas/monitor.py`, `app/api/monitor.py`, `frontend/js/{monitor-client,formatters,api}.js`, `frontend/css/{monitor,client-page}.css`, `migrations/versions/5c3b4a7bda87_*.py`.

### Контрактні зміни (Contract changes)

Адитивні, без ламання:
- `MonitorChangeDTO.related_id: str | None = None` (нове поле).
- `MonitorSettingsDTO`/`MonitorSettingsUpdateRequest.canonical_www: bool | None = None` (нове поле).
- Новий change_type `sitemap_unavailable` (warning) поруч із наявним `sitemap_lost` (critical) — консьюмери, що матчать `change_type` по строгому списку (а не за замовчуванням "unknown → сіра іконка"), варто звірити.

### Як перевірити (verification)

```
docker compose exec api poetry run pytest tests/unit/monitor tests/unit/api/test_monitor_api.py tests/integration/test_monitor_end_to_end.py -q
docker compose exec api poetry run ruff check app/monitor app/api/monitor.py app/db/models.py app/db/repositories/monitor.py app/schemas/monitor.py
docker compose exec api poetry run mypy app/monitor app/api/monitor.py app/db/models.py app/db/repositories/monitor.py app/schemas/monitor.py
node tests/unit/frontend/formatters.test.mjs
```
UI: відкрити клієнта в Site Monitor → рядок `content_changed` у стрічці змін тепер має кнопку **View diff**; `PATCH .../monitor/settings {"canonical_www": true}` → бейдж «Canonical host: www.…» у шапці клієнта.

### Тести

- `tests/unit/monitor/test_diff.py` (новий) — `diff_sitemap_resource`: baseline, підтверджений 404, фетч-збій без 404, healthy.
- `tests/unit/monitor/test_sitemap_availability.py` (новий) — інтеграційний рівень `_process_sitemaps`: 404→critical, фетч-збій→warning, self-heal на наступному здоровому прогоні.
- `tests/unit/monitor/test_base_url.py` (новий) — `_base_url` × `canonical_www` × `override`.
- `tests/unit/monitor/test_change_related_id.py` (новий) — `related_id` на `page_new`/`content_changed`.
- `tests/unit/monitor/test_severity.py` — додано `sitemap_unavailable` до параметризації WARNING.
- `tests/unit/api/test_monitor_api.py` — `canonical_www` set/clear через PATCH, `related_id` у відповіді `/monitor/changes`.

### Follow-ups / борг

- `TODO(task-42)`: автовизначення `canonical_www` з фактичного `final_url` baseline-прогону (щоб поле не лишалось `NULL` без ручного API-виклику).
- `related_id` для `sitemap_url_*` (per-URL записи в межах sitemap) уже проставляється (`page.id`), але фронт поки не малює для них кнопку diff — лише для `scope="page"`. Розширити при потребі.
