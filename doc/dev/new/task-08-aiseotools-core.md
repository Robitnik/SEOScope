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
- [ ] Стара test suite зелена; публічні signatures не змінені (snapshot-тест).
- [ ] `seo_audit/dashboard/{events,models,config}.py` доступні провайдер-таскам.
- [ ] Жодного rename/зміни return shape/зміни семантики `on_progress`.
- [ ] Обрано і задокументовано commit/tag для pin.
- [ ] Прогрес не будується з парсингу лог-рядків.

## Ризики
- Незакомічені зміни в AISeoTools (PLAN §2.3.7) → спочатку привести репо до чистого, відтворюваного стану, лише потім pin.
- Спокуса «підправити» стару функцію → тільки additive; спільну логіку виносити так, щоб старий CLI кликав той самий service (PLAN §8.1).
