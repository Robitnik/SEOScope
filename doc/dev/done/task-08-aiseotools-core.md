# task-08 — AISeoTools Core (additive dashboard namespace)

- **Пакет:** адитивний namespace `seo_audit/dashboard/` у зовнішньому репо AISeoTools + фіксація commit-pin і regression baseline
- **Хвиля:** A (розблоковує провайдер-таски 09–11)
- **Depends on:** —
- **Blocks:** task-09, task-10, task-11
- **Репозиторій:** `/home/elon/dev/seo/AISeoTools` (ОКРЕМИЙ від застосунку)
- **PLAN.md:** §2.2 (точки перевикористання), §2.3 (проблеми контрактів), §8 (безпечне розширення AISeoTools — повністю)

## Мета

Створити **спільне ядро** dashboard-namespace всередині AISeoTools **суворо адитивно**: source-neutral моделі результату, `ProgressEvent`+safe emitter, конфігуровані шляхи/сесії. Це фундамент, на який провайдер-таски (09–11) чіпляють свої wrapper'и. Плюс — зафіксувати commit/tag для pin і зелений baseline старої test suite.

> **Головний інваріант (PLAN §8.1):** не перейменовувати наявні функції/CLI, не міняти return shapes, не міняти семантику старого `on_progress: Callable[[str],...]`. Нові аргументи — тільки optional keyword-only з дефолтами. Перед merge — уся чинна test suite зелена + signature/contract snapshot.

## Обсяг

**Входить:**
- `seo_audit/dashboard/__init__.py`.
- `seo_audit/dashboard/events.py` — `ProgressEvent` (стабільні поля, як у застосунку task-02: provider/stage/completed/total/percent/message/details) + safe emitter (redaction, best-effort). **Логи не є транспортом прогресу.**
- `seo_audit/dashboard/models.py` — source-neutral Pydantic result-моделі (period ranges, observed time, quality complete/partial/truncated, warnings, provenance, usage/units). Дзеркалять сенс `CollectionResult` застосунку, але живуть у AISeoTools.
- `seo_audit/dashboard/config.py` — конфігуровані шляхи через optional args/env з поточними шляхами як default: `NINJA_SESSION_PATH`, download/profile/cache paths, browser profile locations.
- Точкові **адитивні** правки сигнатур наявних функцій (лише optional keyword-only), де це знадобиться спільно (напр. загальний патерн `on_event=None`, `session_path=None`), **без** зміни поведінки за замовчуванням.
- Фіксація: обраний commit/tag для pin + прогін повної старої test suite (baseline) + signature/contract snapshot-тести.

**НЕ входить:** самі провайдер-wrapper'и (`reporting_ninja.py`/`gsc.py` — task-09, `ahrefs.py` — task-10, `semrush.py` — task-11), будь-що в застосунку.

## Owned paths (у репо AISeoTools)
```
seo_audit/dashboard/__init__.py
seo_audit/dashboard/events.py
seo_audit/dashboard/models.py
seo_audit/dashboard/config.py
tests/dashboard/ (core-тести namespace)
# + мінімальні additive kwargs у наявних функціях, узгоджені з провайдер-тасками
```

## Consumes
- Наявні перевикористовувані точки AISeoTools (PLAN §2.2): fetchers, collect-функції, ingest, client'и — **read-only** з погляду контракту (обгортаємо, не переписуємо).

## Provides — споживають task-09/10/11
- `ProgressEvent` + emitter (AISeoTools-side), який app-адаптери мапитимуть у застосунковий `ProgressEvent`.
- source-neutral result-моделі, які app-адаптери мапитимуть у `CollectionResult`.
- Конфігуровані шляхи/сесії.
- **Зафіксований commit/tag** для pin у застосунку (task-01 compose/pyproject).

## Кроки реалізації
1. Зафіксувати чинний стан: прогнати всю наявну test suite → зелений baseline; занотувати незакомічені зміни (PLAN §2.3.7) і привести до чистого стану під pin.
2. `events.py`, `models.py`, `config.py` — адитивно, з докстрінгами про сумісність.
3. Signature/contract snapshot-тести на публічні функції (щоб зміни shape падали в CI).
4. Мінімальний спільний additive-патерн для optional `on_event`/`session_path` там, де його ділитимуть провайдери (узгодити інтерфейс, реалізацію окремих функцій лишити відповідним провайдер-таскам).
5. Обрати commit/tag, задокументувати для застосунку.

## Тести (PLAN §15 AISeoTools contract/regression)
- Уся чинна CLI/test suite проходить **без змін**.
- Snapshot існуючих public signatures (стабільні).
- `on_progress`/callbacks не змінюють результат, якщо `None`.
- emitter не тече секретами; events структуровані.

## Definition of Done / Gate
- [x] Стара test suite зелена; публічні signatures не змінені (snapshot-тест).
- [x] `seo_audit/dashboard/{events,models,config}.py` доступні провайдер-таскам.
- [x] Жодного rename/зміни return shape/зміни семантики `on_progress`.
- [x] Обрано і задокументовано commit/tag для pin.
- [x] Прогрес не будується з парсингу лог-рядків.

## Ризики
- Незакомічені зміни в AISeoTools (PLAN §2.3.7) → спочатку привести репо до чистого, відтворюваного стану, лише потім pin.
- Спокуса «підправити» стару функцію → тільки additive; спільну логіку виносити так, щоб старий CLI кликав той самий service (PLAN §8.1).

---

## ✅ Completion report

- **Дата:** 2026-07-22
- **Гілка / комміт (AISeoTools):** `main` @ `0ec56fcc4d1e7692fb2f2183fb82bcc30e2c980a` (tag `dashboard-core-v1`)
- **Гілка / комміт (застосунок):** `main` (переміщення таск-файлу + INDEX.md — цей комміт)
- **Статус:** done

### Що зроблено
- **Незакомічений стан AISeoTools (PLAN §2.3.7) прибрано перед пином**, за явним рішенням користувача: наявні зміни на `feat/ahrefs-page-value-antislop` розбито на 2 логічні комміти (`e700b47` link-fix-ignore для Google auth/consent посилань, `f7743d2` фікс порожніх agent-значень в OG-рекомендаціях), branch запушено, fast-forward злито в `main`, `main` запушено. Два debug-скретч файли (`ahrefs_logic/keywords_export.txt` — порожній, `scratch_home.html` — 328KB HTML-дамп сторінки) свідомо залишені некомічені як сторонній сміттєвий артефакт, не частина фічі.
- **Зелений baseline старої test suite** зафіксовано на `main` до і після додавання namespace: 1396 passed (стара suite незмінна), 1416 passed після додавання `seo_audit/dashboard/` (1396 + 20 нових).
- **`seo_audit/dashboard/{__init__,events,models,config}.py`** — додано суворо адитивно, жодного існуючого файлу поза `seo_audit/dashboard/`/`tests/dashboard/` не торкнуто.
- **Signature/contract snapshot-тест** (`tests/dashboard/test_contract_snapshot.py` + `tests/dashboard/data/aiseotools_contract_snapshot.json`) — пінить і JSON-схему нового namespace, і `inspect.signature()` усіх reuse-точок з PLAN §2.2/§8.2 (`fetch_ga4_period`, `fetch_gsc_period`, `build_report`, `fetch_pdfs`, Ahrefs `collect`/`verify_issues`, усі публічні методи `SemrushClient`, `ingest_semrush`).
- **Commit/tag для pin обрано й задокументовано:** `dashboard-core-v1` → `0ec56fcc4d1e7692fb2f2183fb82bcc30e2c980a` на `main` AISeoTools.

### Як зроблено (ключові рішення)
- **Жодних правок існуючих функцій.** Крок 4 плану ("мінімальний спільний additive-патерн для optional on_event/session_path") прочитано буквально: task-08 узгоджує *інтерфейс* (`OnEvent = Callable[[ProgressEvent], None]`, `make_emitter()`, `emit_progress()`), а не чіпляє `on_event=None` до конкретних старих функцій (`fetch_pdfs`, Ahrefs `collect`, browser Semrush/GSC runner) — це явно залишено провайдер-таскам (09/10/11), які єдині володіють цими файлами. Тому жоден pre-existing файл у AISeoTools не змінено.
- **`events.py`** — `ProgressEvent` дзеркалить застосунковий `app/schemas/adapter.py::ProgressEvent` поле-в-поле (`provider/stage/completed/total/percent/level/message/details`). `make_emitter(None)` — чистий no-op (той самий контракт, що й старий `on_progress=None`). `make_emitter(callback)` редагує `message`/`details` (ключі, що виглядають як секрети — `password/token/secret/api_key/cookie/session/auth/credential/bearer` — редагуються цілком; текст додатково фільтрується проти `Bearer <token>`/`key=value`/JWT-подібних патернів) **до** виклику callback і ковтає будь-який exception з callback (progress — best-effort, ніколи не валить collect()).
- **`models.py`** — `DashboardResult`/`DashboardMetricValue`/`DateRange`/`RawArtifactRef`, поле-в-поле дзеркалять `CollectionResult`/`MetricValueResult`/`DateRange`/`RawArtifactRef` застосунку, але **без жодної залежності на app** (мапінг — робота майбутніх app-side адаптерів). `value_numeric: Decimal | None` — `None` ніколи не колапсує в `0`.
- **`config.py`** — свідомо мінімальний. Перевірено код (не лише PLAN-опис): `Ahrefs collect(cache_dir=None)` і `Semrush ingest_semrush(downloads, ...)` **вже** приймають шлях явним параметром без прихованого дефолту — конфігурувати нема чого, вигадувати дефолт для них не стали (щоб не порушувати "не додавай гіпотетичні вимоги"). Реальна прогалина — лише `pdf_exporter.SESSION_PATH` (хардкод-константа) → `ninja_session_path(override=None)` з пріоритетом arg > env (`SEOAUDIT_DASHBOARD_NINJA_SESSION_PATH`) > той самий поточний дефолт. `profiles_root()` — тонкий re-export вже конфігурованого через `SEOTOOLS_ROOT` (`seo_audit.browser.profile.PROFILES_DIR`), лише для зручності одного місця пошуку шляхів; `pins.json` не читається/не пишеться і не чіпається (заборона з `AISeoTools/CLAUDE.md` §5).
- **Snapshot-тест** побудовано за патерном task-02 (`tests/unit/schemas/test_contract_snapshot.py` у застосунку): JSON-файл + `if __name__ == "__main__"` для свідомого regen. Два незалежні розділи в одному знімку — `models` (наш новий namespace, очікувано змінюється) і `legacy_signatures` (чужий існуючий код, зміна тут = заборонений §8.1 дрейф).

### Контрактні зміни (Contract changes)
- **AISeoTools:** новий namespace `seo_audit/dashboard/` — чисто адитивний, нічого не ламає для існуючого CLI/тестів (1396 старих тестів без жодної правки, усі зелені).
- **Застосунок (`pyproject.toml`, власність task-01):** pin **не** вписаний у `pyproject.toml` цією таскою — цей файл поза owned paths task-08. `pyproject.toml` вже містить власний коментар-заглушку ("task-08+ має запінити AISeoTools... `[tool.poetry.group.local]`"), яка досі актуальна. **Follow-up-запит до task-01** (за патерном, яким task-07 запросив `openpyxl`): замінити/доповнити `seo-audit = { path = "../../seo/AISeoTools", develop = true }` на git-залежність, запінену на tag `dashboard-core-v1` (commit `0ec56fcc4d1e7692fb2f2183fb82bcc30e2c980a`), напр. `seo-audit = { git = "git@github.com:Robitnik/AISeoTools.git", rev = "dashboard-core-v1" }`, коли застосунок реально починає імпортувати `seo_audit.dashboard` (тобто разом із task-09/10/11).

### Як перевірити (verification)
```bash
cd /home/elon/dev/seo/AISeoTools
git log --oneline -3                        # 0ec56fc feat(dashboard)... на main
git tag --list dashboard-core-v1
.venv/bin/python -m pytest -q                # 1416 passed
.venv/bin/python -m pytest tests/dashboard -q -v   # лише новий namespace, 20 passed
```
Свідоме оновлення снепшоту (тільки після review зміни): `.venv/bin/python -m tests.dashboard.test_contract_snapshot`.

### Тести
- `tests/dashboard/test_events.py` (8) — дефолти `ProgressEvent`, `extra=forbid`, `make_emitter(None)` — no-op, доставка події через `emit_progress`, callback-exception не пропагується, редакція секретних ключів у `details` (включно з nested dict), редакція `Bearer …`/JWT-подібного тексту в `message`.
- `tests/dashboard/test_models.py` (6) — `value_numeric=None` не колапсує в `0`, partial-результат без exception, snapshot-метрика без дат періоду, range-метрика з `DateRange`, `extra=forbid`, точність `Decimal`.
- `tests/dashboard/test_config.py` (5) — `ninja_session_path()` пріоритет arg > env > дефолт (звірено з реальною `pdf_exporter.SESSION_PATH`), `profiles_root()` звірено з реальним `browser.profile.PROFILES_DIR`.
- `tests/dashboard/test_contract_snapshot.py` (1) — JSON-схема namespace + сигнатури 8 legacy reuse-точок (включно з усіма публічними методами `SemrushClient`) проти закомiченого знімку.

### Follow-ups / борг
- `TODO(task-01)`: вписати git-pin (`dashboard-core-v1`) у `pyproject.toml` `[tool.poetry.group.local]`, коли застосунок почне імпортувати `seo_audit.dashboard` (разом із task-09/10/11) — до того часу коментар-заглушка в `pyproject.toml` лишається чинним.
- Два debug-артефакти (`ahrefs_logic/keywords_export.txt`, `scratch_home.html`) свідомо залишені некомічені в AISeoTools working tree — не частина цієї таски, користувачу варто самому прибрати або закомітити за потреби.
- `on_event=None`-кплюг у самі старі функції (`fetch_pdfs`, Ahrefs `collect`, semrush/gsc browser runner) — свідомо НЕ зроблено тут, лишається task-09/10/11 (кожен у своєму owned-файлі), як прямо написано в кроці 4 плану.
