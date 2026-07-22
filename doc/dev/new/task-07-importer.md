# task-07 — Importer (імпорт історії з AISeoTools + CLI)

- **Пакет:** one-off ідемпотентний імпорт наявних артефактів AISeoTools у БД
- **Хвиля:** B
- **Depends on:** task-03 (репозиторії, `imported_sources`), task-04 (нормалізація/каталог)
- **Blocks:** можливість показати «попередній» період з першого дня (backfill)
- **PLAN.md:** §11 (імпорт наявних даних), §6 `imported_sources`, Етап 2 gate

## Мета

CLI, що read-only сканує дерево прогонів AISeoTools і **ідемпотентно** заливає історію в БД через versioned importers, щоб dashboard мав із чим порівнювати вже в день запуску. Source-файли не змінюються; повторний запуск не дублює дані.

## Обсяг

**Входить:**
- `app/importers/ai_seo_tools.py` — оркестрація імпорту + окремі versioned parser'и під кожен формат.
- `app/cli.py` — команда:
  ```bash
  poetry run yovth-report import-ai-seo-history --source /mnt/ai-seo-tools --dry-run
  ```
  (потім без `--dry-run`).
- Мапінг `clients.json` → client/integration записи **без копіювання секретів**.
- Розпізнавання dated run directories та форматів: `ahrefs_findings_machine.json`, `gsc_raw.json`, `gsc_findings_machine.json`, Semrush snapshots, rank movement artifacts, client report TSV/XLSX — **лише через versioned importers**.
- Показ конфліктів мапінгу `client code ↔ domain ↔ client name` **перед** записом.
- Checksum у `imported_sources` (sha256) → повторний запуск не дублює; невідомі/старі схеми — **skip з warning**, без вгадування полів.
- Часткове заповнення «попереднього» snapshot.

**НЕ входить:** живий збір (провайдери), зміни в самому AISeoTools (read-only!), UI.

## Owned paths
```
app/importers/__init__.py
app/importers/ai_seo_tools.py
app/importers/parsers/**       # versioned parser на кожен формат
app/cli.py                     # entrypoint import-ai-seo-history (+ реєстрація в pyproject scripts — запит до task-01)
```

## Consumes
- `ClientRepository`, `SnapshotRepository`, `MetricRepository`, `ImportRepository` — task-03.
- `normalize_result`/каталог, `MetricValueResult`, `Quality`, `Provider` — task-04/02.

## Provides
- Ідемпотентний backfill історичних snapshot/metric.
- Versioned parser'и (перевикористає задокументовані формати AISeoTools — джерела істини в `AISeoTools/docs/*` та python-модулях).

## Кроки реалізації
1. Сканер дерева `--source` (read-only), визначення dated runs і клієнтів із `clients.json`.
2. Versioned parser'и: кожен знає свою схему й версію; невідоме — skip+warning.
3. Нормалізація у `MetricValueResult` (task-04), присвоєння правильного `period_role`/`period` (snapshot vs range за типом метрики).
4. Dry-run звіт: що буде імпортовано, конфлікти мапінгу — **до** запису.
5. Запис: immutable snapshot+metrics, checksum у `imported_sources`; повтор пропускає вже бачене (sha256).

## Тести (PLAN §15 integration)
- dry-run нічого не пише; звіт коректний.
- ідемпотентність: другий прогін не додає дублів (перевірка через `imported_sources`).
- schema drift: невідома схема → skip+warning, не падіння, не вгадування полів.
- source read-only: файли не змінюються (перевірити mtime/checksum до/після).
- конфлікт `code↔domain↔name` показується, а не тихо перезаписує.
- snapshot/range розводяться коректно (Ahrefs/Semrush = snapshot; GSC/GA4 range).

## Definition of Done / Gate
- [ ] Клієнти й історичні snapshots імпортуються ідемпотентно; source-файли не змінені (PLAN Етап 2 gate).
- [ ] Секрети з `clients.json` не потрапляють у БД.
- [ ] Невідомі схеми — warning+skip, без вгадування.
- [ ] dry-run детермінований і показує конфлікти до запису.
- [ ] `--source` читається строго read-only.

## Ризики
- Формати артефактів можуть відрізнятися між прогонами → versioned parser'и + skip невідомого (не вгадувати).
- Мапінг клієнтів неоднозначний → показати конфлікти оператору, не вирішувати мовчки.
