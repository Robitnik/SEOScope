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
- [x] Клієнти й історичні snapshots імпортуються ідемпотентно; source-файли не змінені (PLAN Етап 2 gate).
- [x] Секрети з `clients.json` не потрапляють у БД.
- [x] Невідомі схеми — warning+skip, без вгадування.
- [x] dry-run детермінований і показує конфлікти до запису.
- [x] `--source` читається строго read-only.

## Ризики
- Формати артефактів можуть відрізнятися між прогонами → versioned parser'и + skip невідомого (не вгадувати).
- Мапінг клієнтів неоднозначний → показати конфлікти оператору, не вирішувати мовчки.

---

## ✅ Completion report

- **Дата:** 2026-07-22
- **Гілка / комміт:** main @ (uncommitted at time of writing — див. фінальний коміт)
- **Статус:** done-with-followups

### Що зроблено

CLI `poetry run yovth-report import-ai-seo-history --source <path> [--dry-run]` read-only сканує дерево AISeoTools і ідемпотентно імпортує історичні snapshot/metric у БД. Реалізовано versioned parser на кожен реальний формат, знайдений у фактичному `out/`-дереві (не лише за описом задачі): Ahrefs (обидва collection paths — API/CSV і поточний browser-based, де `health_score`/`crawl_date` часто `null`), GSC (дві несумісні "родини" файлів під одним суфіксом `_gsc`), Semrush snapshot, client-report XLSX (лише "Historical Data") та rank-movement (розпізнається, але свідомо не імпортується — немає відповідних метрик у каталозі). Мапінг `clients.json` → `Client`/`ClientIntegration` з детекцією конфліктів `code↔domain` **до** запису.

### Як зроблено (ключові рішення)

Формати артефактів дослідив через Explore-агента (реальні файли в `/home/elon/dev/seo/AISeoTools/out/**` + сам код AISeoTools, а не тільки опис задачі) — реальність виявилась суттєво брудніша, ніж передбачав план задачі. Кожне рішення нижче — наслідок цього дослідження, не здогадка:

- **GSC.** Дві несумісні "родини" ховаються за одним суфіксом `_gsc`, розпізнаються за наявністю файлів, не за версією (її просто немає): Family A (`gsc_findings_machine.json`, вже проімпортований `gsc-import`) — у **12/12** реальних семплів `totals.has_date_dimension=false` і `date_range_start/end` порожні; range-метрику без точного діапазону імпортувати — це і є те "вгадування", яке заборонено CLAUDE.md, тож Family A **пропускається зі skip+warning**, коли діапазону нема (перевіряється, а не вважається завжди відсутнім). Family B (`performance_chart.csv`, browser-scrape, який ніколи не проганяли через ingest) має справжні щоденні дати — саме звідти реально імпортується GSC-історія.
- **Ahrefs.** `health_score: null` і `totals.urls_crawled: 0` — це **нормальний** стан на поточному browser-collection шляху (Ahrefs API units вичерпані до 2026-08-01, підтверджено в `docs/AhrefsAudit/RETRO_SAPHNY_2026-07-21.md` самого AISeoTools), не помилка; для `urls_crawled` є fallback на `verify_summary.total`.
- **Semrush.** У власному UI Semrush немає експорту Authority Score — `overview`/`authority_score` часто `null`; кожна метрика емітиться окремо як `None` (не 0), а не блокує весь артефакт.
- **client-report XLSX.** Парситься **лише** аркуш `Historical Data` (реальні файли мають title-рядок перед заголовком — знайдено й виправлено під час manual verification, див. нижче). `Current Month Data`/`Import Data` і `.tsv`-близнюк навмисно **не** парсяться: їхня колонка "This Month" означала різне до/після фіксу в самому AISeoTools 2026-06-19 (git-log підтверджує), і в файлі немає маркера, з якої він ери — вгадування тут так само заборонене.
- **Rank movement.** Розпізнається сканером (щоб не губився мовчки), але це derived diff-артефакт (два довільні дати порівняння, не період) без відповідника в поточному каталозі метрик — свідомо не імпортується, `level="info"`.
- **Синтетичний `SyncJob`.** `ProviderSnapshot.job_id` — обов'язковий FK; для backfill-даних (не live-запуск) додано `JobRepository.create_imported_run()`, що створює вже завершений `SyncJob` (`mode="imported_backfill"`) **разом з** одним `SyncJobProvider`-осередком, а не голий job без клітинок — щоб `CellRepository.latest_for_client_provider` (dashboard "last successful") бачив бекфіл так само, як живий успіх.
- **Одна транзакція на артефакт**, за зразком `worker_session_scope` — один битий файл не валить увесь прогін (той самий принцип ізоляції клітинки, що й у live collection, PLAN §9.2). Перевірено вживу: один навмисно зіпсований JSON серед валідних артефактів не завадив решті імпортуватись.
- **Ідемпотентність через `ImportRepository`**, ключ — sha256 усього файлу; recognized-but-skipped артефакти теж записуються в `imported_sources`, інакше кожен прогін заново шумів би тими самими warning'ами.

### Контрактні зміни (Contract changes)

- **`pyproject.toml`** (task-01-owned): додано `openpyxl = "^3.1"` у `[tool.poetry.dependencies]` (раніше був лише транзитивно через опціональну групу `local`, якої немає в production-образі — підтверджено по `Dockerfile`: `poetry install --no-root` без `--with local`) і `[tool.poetry.scripts] yovth-report = "app.cli:main"` (сама задача явно позначає цей рядок як "запит до task-01"). `poetry.lock` перегенеровано в Docker.
- **`app/db/repositories/jobs.py`** (task-03-owned): додано `JobRepository.create_imported_run()` — див. вище.
- **`app/db/repositories/clients.py`** (task-03-owned): додано `ClientRepository.get_by_domain()` — потрібен, щоб кілька артефактів одного домену (напр. `_ahrefs` і `_gsc` прогони) резолвились в один і той самий `Client`, а не дублювались.

Обидва додавання йдуть за вже усталеним у репо патерном (`# --- Added by task-NN ...`), як у task-06.

### Як перевірити (verification)

```bash
docker compose build api
docker compose exec api poetry run pytest tests/unit/importers -q   # 54 passed
docker compose exec api poetry run ruff check app/importers app/cli.py
docker compose exec api poetry run mypy app/importers app/cli.py
# Проти реального сусіднього checkout (compose.yaml бінд-моунтить AISeoTools у /seo/AISeoTools):
docker compose run --rm api poetry run yovth-report import-ai-seo-history --source /seo/AISeoTools --dry-run
```

Вручну перевірено проти **реального** `/home/elon/dev/seo/AISeoTools` (53 артефакти в `out/`), в ізольованій тимчасовій БД (не спільний `app_data` том):
- dry-run: 36 imported / 17 skipped_recognized, конфліктів мапінгу немає.
- реальний прогін: ті самі 36/17, без винятків.
- ідемпотентність: другий прогін → 53/53 `skipped_already_seen`, нуль нових рядків у `provider_snapshots`/`metric_values`.
- read-only: mtime/вміст усіх торкнутих source-файлів (`clients.json`, `semrush_*.json`, `performance_chart.csv`) не змінились до/після.

### Тести

`tests/unit/importers/` (54 тести): `test_scanner.py` (розпізнавання патернів, виключення cache-директорій/чужих pipeline), `test_client_mapping.py` (нормалізація домену/коду, конфлікти, unmappable), по одному файлу на кожен parser (синтетичні фікстури, не реальний checkout — тести герметичні), `test_ai_seo_tools.py` (dry-run без записів, ідемпотентність, конфлікт блокує лише свій домен, provisional client для немапленого домену, read-only гарантія, один битий артефакт не валить прогін, **регресійний тест на multi-month client-report bug**, знайдений під час manual verification — див. нижче), `test_cli.py`.

### Follow-ups / борг

- **GSC Family A завжди пропускається на цьому датасеті** (12/12 реальних `gsc_findings_machine.json` без `has_date_dimension`) — це коректна поведінка (не вгадувати період), але означає, що більшість історичних GSC-снепшотів у поточних даних не бекфілиться. Джерело кращої якості — Family B (`performance_chart.csv`), коли є.
- **`client_report` Current Month Data / Import Data не парсяться** через нестабільну семантику "This Month" (зміна 2026-06-19 в самому AISeoTools). Якщо буде потрібно — можна додати era-detection за mtime файлу відносно відомого git-timestamp фіксу, але це прив'язка до історії стороннього репо, свідомо не робив цього зараз.
- **Ahrefs/Semrush не отримують авто-`ClientIntegration`** при бекфілі (`clients.json` взагалі не містить ahrefs/semrush конфігів — підтверджено). Дашборд може показати `not_configured` для клієнта з реальною історією, доки не буде живого sync.
- **`ImportRepository.seen()` ігнорує `importer_version`** (унікальність лише по sha256) — апгрейд парсера не переімпортує вже пропущені файли автоматично; потребує ручного видалення відповідних рядків `imported_sources`. Існуючий дизайн task-03, не змінював.
- **`out/pdfs/*.pdf`** (Reporting Ninja PDF-звіти з точними датами в імені файлу) — альтернативне джерело тих самих метрик, не реалізовано (поза обсягом задачі, TSV/XLSX explicitly в scope, PDF — ні).
- Під час manual verification знайшов і виправив реальний баг: перша версія `client_report`-парсера пакувала всі місяці "Historical Data" в **один** snapshot, що падало на `UNIQUE constraint (snapshot_id, metric_key, period_role)` при реальному записі (спрацювало тільки на реальному XLSX з 14 рядками історії — юніт-тести з 1-2 рядками цього не ловили). Виправлено: один snapshot на `(provider, місяць)`. Додано і регресійний DB-тест, і unit-тест на title-рядок перед заголовком (теж знайдений тільки на реальних файлах).
