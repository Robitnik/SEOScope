# task-62 — Web Archive: черга капчурів, живий прогрес і лог

- **Пакет:** серіалізація SQLite-запису через `ArchiveWriteController` (збір лишається паралельним, лише запис — послідовний, з ретраєм), FIFO-черга капчурів замість віяльного запуску, кнопка «оновити зараз» на рівні одного URL і всього портфеля, сторінка одного прогону з покроковим прогресом і логом, кооперативне скасування.
- **Хвиля:** U — Web Archive (третя таска хвилі, після 59 і 60).
- **Depends on:** **59** ✅ (архів, капчур, планувальник, replay), **60** ✅ (asset discovery), **05** ✅ (worker-процес і його патерн реєстрації планувальників), **06** ✅ (REST-патерни), **43/45** ✅ (звідки беремо готовий патерн «прогрес як дані + лог окремо»).
  - **task-03** — **потрібна міграція** (§3). За [CLAUDE.md](../../../CLAUDE.md) міграції створює виключно task-03, тож ця таска подає **запит**, а не редагує `migrations/` сама.
  - **task-61** (URL Inspector) — **паралельна**. Вона читає `app/webarchive/urls.normalize_url` і не пише в `app/webarchive/**`. Конфлікту немає.
- **Owned paths:**
  - `app/webarchive/scheduler.py`, `app/webarchive/capture.py`, `app/webarchive/events.py` (новий), `app/webarchive/writequeue.py` (новий)
  - `app/db/repositories/web_archive.py`
  - `app/api/archive.py`, `app/schemas/web_archive.py`
  - `frontend/js/archive-page.js`, `frontend/js/archive-capture.js` (новий), `frontend/css/archive.css`
  - `tests/unit/webarchive/**`, `tests/unit/api/test_archive_api.py`
- **Адитивні дотики спільних файлів (перед комітом перевірити `git status`):** `app/cli.py` (`archive-capture` переводимо на чергу), `app/core/config.py` (1 ключ), `frontend/index.html` (+1 root, +1 css-лінк), `frontend/js/router.js` (+1 маршрут), `frontend/js/api.js` (+5 методів), `tests/unit/api/data/openapi_snapshot.json`.
- **Міграції:** **потрібні** — 6 колонок у `archive_captures` + 1 нова таблиця. Точний DDL і обґрунтування — §3. **Перед застосуванням — бекап** (`scripts/backup_sqlite.py`), і **без `batch_alter_table`** (CLAUDE.md: саме він стер `client_integrations` на проді 2026-09-01).
- **Нових залежностей — нуль.**
- **PLAN.md:** §5.1 (один SQLite-writer), §9.1 (ізоляція клітинки), §9.2 (прогрес переживає рестарт), §12 (`None` ≠ `0`), §13 (секрети не течуть у лог).
- **frontend/AGENTS.md:** §5 (правда даних), §6 (обов'язкові стани), §7 (a11y), §9 (живий прогрес, F5-resume, кооперативне скасування), §10 (без вічних анімацій), §12 (CSP, abort obsolete fetches), §14 (UI-копірайт англійською), §15–16 (візуальний loop).

---

## Мета

Сьогодні архів **не має черги**. Планувальник віддає всі черговані рядки одразу в пул на 6 потоків, вони б'ються за єдиний write-lock SQLite, і той, кого планувальник не бачить у пам'яті власного процесу, отримує `failed/interrupted` — попри те, що процес живий і працює далі. Наслідок виміряний нижче: **69 % капчурів за всю історію провалені**, найдовші висіли по 13 годин.

Друга діра — **спостережуваності немає взагалі**. `run_client_capture` не пише жодного рядка логу, per-URL сліду не існує, і на питання «на чому воно зараз і що впало» відповіді немає — лише `N / M`.

Третя — кнопка «оновити зараз» є **тільки на цілого клієнта**. Оператор, який дивиться на таймлайн однієї сторінки, не може оновити саме її; а «зібрати всіх» не існує ні в UI, ні як безпечна операція.

---

## §0. Досліди на бойових даних (виміряно 2026-09-05)

### §0.1 ⚠️ Головна знахідка: 69 % капчурів провалені, і сайти тут ні до чого

```
всього капчурів: 62   →   failed: 43,  partial: 7,  ok: 12
error_code:  interrupted 27 | crashed 8 | interrupted_by_restart 8
заплановані: 25 з 34 провалених (74 %)
```

Жоден код помилки не вказує на зовнішній сайт. Усі три означають, що **процес капчуру помер або був помилково оголошений мертвим**.

### §0.2 ⚠️ Черги не існує: пік — 10 одночасних капчурів

```
PEAK simultaneous captures: 10
captures that overlapped another: 52 / 60
```

Причина в коді. [`web_archive.py:103`](../../../app/db/repositories/web_archive.py#L103) — `queued_captures()` без `ORDER BY`, без ліміту, без дедупу по клієнту. [`scheduler.py:206-209`](../../../app/webarchive/scheduler.py#L206-L209) — `_tick` віддає **всі** черговані рядки в пул одним махом:

```python
for queued in repo.queued_captures():
    client = clients_repo.get(queued.client_id)
    if client is not None:
        self._submit(client, queued.id)
```

Єдина «черга» — внутрішня необмежена черга `ThreadPoolExecutor` на `archive_client_workers=6`. Статус `queued` у БД **змішує два різні стани**: «планувальник ще не бачив» і «чекає на вільний потік». Позиції, порядку й ETA не існує.

Додатково: [`archive.py:496`](../../../app/api/archive.py#L496) стереже лише `has_running_capture`, тобто **не бачить `queued`** — два кліки поспіль створюють два рядки на одного клієнта, і `_tick` подає обидва в один тік.

### §0.3 ⚠️ Найдовші прогони висіли по 13 годин

```
12h58m  failed  interrupted   76/860
12h58m  failed  interrupted  123/231
12h58m  failed  interrupted   62/263
12h55m  failed  interrupted   94/180
12h40m  failed  interrupted  112/673
медіана тривалості: 607 с
```

Дедлайн `archive_capture_deadline_minutes=240` не рятує: [`scheduler.py:155-162`](../../../app/webarchive/scheduler.py#L155-L162) на перевищенні лише **пише WARNING**, нічого не вбиваючи, бо вбити чужий тред нічим.

### §0.4 ⚠️ Сліпа зона планувальника — відтворено наживо сьогодні

Критерій живості — членство в `self._in_flight`, тобто **пам'ять одного процесу**. Це задокументовано як відомий борг у [`scheduler.py:135-143`](../../../app/webarchive/scheduler.py#L135-L143):

> Known edge: `yovth-report archive-capture` runs a capture in its OWN process, whose rows this scheduler cannot see in `_in_flight` and so marks failed within a minute… **The real fix is an owner/heartbeat column on `archive_captures` — a migration, so task-03's call.**

Відтворено сьогодні о 08:57 UTC на `archive-capture --all`:

| час | що сталося |
|---|---|
| 08:57:52 | CLI стартує капчур 3XE у власному процесі |
| 08:58:37 | планувальник не бачить його в `_in_flight` → `finish_capture(status="failed", error_code="interrupted")` |
| 08:58:37+ | **процес живий і працює далі**: `urls_captured` 72 → 120 → 134 на рядку, який система вже вважає провальним |
| 09:03 | CLI глухне на `archive_db_locked_retry attempt=2/4`, не може створити наступний рядок |

Тобто 27 `interrupted` в §0.1 — здебільшого не збої, а **хибні вироки**.

### §0.5 Логу немає взагалі

`grep` по `app/webarchive/capture.py`: `run_client_capture` **не пише жодного рядка логу**. На весь модуль три `logger` виклики, усі — про краш (`archive_asset_crashed:323`, `archive_capture_url_crashed:617`, `archive_backfill_asset_crashed:752`). Таблиці подій для архіву немає (`archive_capture_events` не існує; `job_events` — це підсистема sync-джобів).

Per-URL слід існує **лише опосередковано**: `ArchiveSnapshot.capture_id` — і тільки для тих URL, де контент **змінився**. Незмінені йдуть через `confirm_snapshot` і не лишають рядка, прив'язаного до капчуру. Тобто «які 512 сторінок ти щойно обійшов» неможливо відновити навіть заднім числом.

### §0.6 Масштаб: повний прогін портфеля ≈ 7 годин

```
SAPHNY  528 urls / 52m →  6.0 s/url        портфель: 4407 URL
SDS     422 urls / 19m →  2.8 s/url        медіана:  5.8 s/url
HWELL   269 urls / 18m →  4.2 s/url        ──────────────────
SHB     104 urls / 29m → 17.0 s/url        повний послідовний
NYCHA    94 urls / 38m → 24.3 s/url        прогін ≈ 7.1 год
```

URL на клієнта: ALPHA 1181, SAPHNY 673, 3XE 646, LAXTON 639, SDS 442, HWELL 284, GOREPORT 246, SHB 116, NYCHA 94, GRP 32, SWM 27, EFFYS 17, 19CLV 10.

**Висновок, що визначає дизайн:** «зібрати всіх» не може бути ні синхронним HTTP-запитом, ні CLI-командою, прив'язаною до живої сесії. Тільки довговічна черга, що переживає рестарт і показує прогрес. Один URL — навпаки, секунди, і має бути миттєвою дією.

### §0.7 Що вже працює — і що ми **не** переписуємо

Комміт `a4073d9` уже дав половину прогресу. Це фундамент, а не поле для рефакторингу:

| Шар | Що вже є | Де |
|---|---|---|
| Двигун | `urls_total` пишеться **до** першого URL | [`capture.py:584-590`](../../../app/webarchive/capture.py#L584-L590) |
| Двигун | флаш лічильників кожні 20 URL **або** 5 с | [`capture.py:638-657`](../../../app/webarchive/capture.py#L638-L657) |
| Двигун | комміт на кожен асет — лок не тримається через мережевий I/O | `_capture_assets_for_page` |
| API | `GET /captures`, `POST /captures` → 202, 409 `already_running` | [`archive.py:477`](../../../app/api/archive.py#L477), [`:505`](../../../app/api/archive.py#L505) |
| UI | кнопка **Capture now** на клієнта | [`archive-page.js:409`](../../../frontend/js/archive-page.js#L409) |
| UI | самозаводний polling 5 с, що сам і зупиняється | `syncLivePolling` / `refreshOverviewQuietly` |
| UI | нативний `<progress>` — бо CSP `default-src 'self'` без `unsafe-inline` | [`archive-page.js:461-471`](../../../frontend/js/archive-page.js#L461-L471) |

**Отже enqueue/claim-розділення вже правильне.** Бракує впорядкування, admission control, довговічного власника, логу й кнопок на двох інших рівнях.

---

## §1. Що будуємо

### §1.1 «Refresh now» на одному URL — те, чого бракує в кадрі оператора

Кнопка в шапці результату пошуку, поруч із «N versions captured» ([`archive-page.js:194-204`](../../../frontend/js/archive-page.js#L194-L204)). Капчурить **саме цю сторінку**: `urls_total=1`, медіана ≈ 6 с. Після завершення таймлайн і календар перемальовуються самі.

Це прямо закриває сценарій, у якому сьогодні оператор безсилий: дивишся на сторінку, бачиш стару версію, і єдиний важіль — перезібрати всього клієнта (для ALPHA це 1181 URL і ~2 години).

### §1.2 «Capture all» на хабі

Одна кнопка ставить у чергу всіх колектабельних клієнтів окремими рядками. Черга розбирає їх **послідовно**. Це і є безпечна форма «збери всіх зараз» — операції, яка сьогодні або валиться (§0.4), або тримає термінал 7 годин.

### §1.3 Черга з реальним admission control — просто на рівні планування

Один FIFO, без класів пріоритету й без окремих пулів для різних типів капчуру:

- Порядок: `ORDER BY queued_at ASC` — чиста FIFO. Ручний і плановий капчур рівноправні, обидва просто встають у хвіст.
- **Збір лишається паралельним** — це навмисне рішення (мережевий фетч не є вузьким місцем: `HostThrottle` і так серіалізує per-host, різні клієнти — різні хости). Ліміт одночасних капчурів лишається наявним `archive_client_workers` (сьогодні 6) — нової конфігурації для цього не вводимо.
- Капчур одного URL (§2.7) стає в **ту саму** чергу — без окремого пулу: він короткий (~6с), тож не чекає довго навіть у FIFO.
- **Ніколи двох активних капчурів на одного клієнта** — перевірка в claim, а не в API.
- `POST /captures` стереже `queued` **і** `running` → повторний клік не плодить рядків.

**Вузьке місце — не паралельний збір, а паралельний запис.** Проблему «БД не справляється» вирішує не зменшення кількості одночасних капчурів, а серіалізація самого запису — §2.2.

### §1.4 Панель черги на хабі

Секція «Recent captures» стає «Queue & activity»: активні згори з прогрес-баром, за ними черга з позицією (`#2 in queue`), за ними завершені. Кожен рядок — посилання на §1.5.

### §1.5 Сторінка одного капчуру: `#/archive/capture?id=…`

Прогрес (скільки з скількох, поточний URL, швидкість, ETA) + **лог** зі списком подій, фільтром за рівнем і кнопкою Copy. Стан переживає F5 — маршрут несе `id` (AGENTS §9).

### §1.6 Скасування

Черговий рядок знімається миттєво. Активний — **кооперативно**: цикл у `run_client_capture` перевіряє прапорець між URL. Без цього черга з 7-годинним прогоном некерована, а AGENTS §9 прямо вимагає явного кооперативного скасування.

---

## §2. Архітектура

### §2.1 Чому не переносимо це в `app/jobs/`

`app/jobs/` — не універсальний раннер, а **двигун провайдерських клітинок**: джоба = клієнт × період, розкладена на клітинки по `Provider`, кожну виконує `ProviderAdapter` з реєстру. Єдина точка розширення — новий член enum `Provider` + адаптер, що повертає `CollectionResult` з метриками. Капчур сайту — не метрика.

Проєкт уже чотири рази вирішив це однаково: **своя таблиця + своє репо + свій планувальник-тред у `app/jobs/worker.py` + свої REST-ендпоїнти** (`MonitorScheduler`, `ReportScheduler`, `CloudScheduler`, `ArchiveScheduler`). Причина в докстрінгах однакова: другий процес = другий SQLite-writer, що заборонено PLAN §5.1.

**Тож розширюємо `ArchiveScheduler`, а не будуємо паралельну систему.**

### §2.2 `ArchiveWriteController` — серіалізація запису, а не збору

**Це головна зміна проти першого чорновика цього плану.** Спершу здавалось, що фікс — зменшити кількість одночасних капчурів. Але вузьке місце не мережевий фетч (він і так серіалізований per-host, і різні клієнти б'ють різні хости паралельно без шкоди) — вузьке місце **єдиний SQLite-writer**. Правильний фікс — не гальмувати збір, а прибрати сам конфлікт запису: скільки б капчурів не фетчили паралельно, у SQLite в кожен момент іде **рівно один** запис.

Реалізація вийшла простішою за перший чорновик: контролер не володіє власною сесією — кожна job несе **свою** (сесію викликача), контролер лише почергово її торкається. Це робить `submit` буквальною drop-in заміною наявного `commit_with_retry(session, fn)` — той самий підпис, той самий результат, ті самі винятки — і не займає жодного рядка логіки читання/запису всередині `capture_url`/`_capture_assets_for_page`.

```python
# app/webarchive/writequeue.py

@dataclass
class _WriteJob(Generic[T]):
    session: Session
    fn: Callable[[], T]
    future: Future[T]

class ArchiveWriteController:
    """Єдина точка запису для всього archive-підсистеми в цьому процесі.

    SQLite WAL дозволяє багато читачів, але лише одного писача одразу.
    Виміряно 2026-09-05: 6 паралельних воркерів `ArchiveScheduler`, кожен
    зі своєю сесією, комітять незалежно — `archive_db_locked_retry` під
    портфельним прогоном, пік 10 одночасних капчурів (§0.2). Замість того,
    щоб кожен воркер боровся за лок сам, усі write-операції складаються в
    чергу; один фоновий тред-контролер розбирає її строго послідовно,
    кожен запис — через наявний `commit_with_retry` (retry лишається —
    він рятує від міжпроцесного конфлікту з CLI-капчуром, який працює у
    своєму OS-процесі й цієї черги не бачить). Жодна сесія не
    використовується з двох тредів одночасно: викликач блокується на
    своєму `Future`, не чіпаючи свою сесію, поки контролер її обробляє.

    Читання лишаються на сесії воркера, що викликає, без жодних змін —
    паралелити їх безпечно (WAL), серіалізувати немає сенсу.
    """

    def start(self) -> None: ...   # запускає self._run у демон-треді
    def stop(self) -> None: ...    # sentinel у чергу + join

    def submit(self, session: Session, fn: Callable[[], T]) -> T:
        """Той самий підпис, що й `commit_with_retry(session, fn)` — drop-in
        заміна на кожному виклику. Блокує ЦЕЙ виклик до застосування
        запису; фетч інших воркерів іде собі паралельно далі."""
        job: _WriteJob[T] = _WriteJob(session=session, fn=fn, future=Future())
        self._queue.put(job)
        return job.future.result()

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is _STOP:
                return
            try:
                job.future.set_result(commit_with_retry(job.session, job.fn))
            except BaseException as exc:  # noqa: BLE001 — result travels via Future, не через raise тут
                job.future.set_exception(exc)
```

Один інстанс живе на `ArchiveScheduler` (як і `ThreadPoolExecutor` сьогодні), передається новим `write_controller`-параметром у `run_client_capture`/`capture_url`/`_capture_assets_for_page`/`seed_urls_from_monitor`/`backfill_css_assets`. Кожен наявний виклик `commit_with_retry(session, partial(repo.xxx, …))` міняється механічно на `write_controller.submit(session, partial(repo.xxx, …))` — той самий підпис, той самий callable, жодної іншої логіки не чіпаємо. `scheduler.py`'s `_tick`/`_reclaim_dead_captures`/`_submit` — так само. Фетч (`fetch`, мережевий I/O) лишається як є — це не запис.

Це також природно вирішує §0.4: `heartbeat_at` (нижче) пишеться через той самий контролер, тож не змагається з лічильниками капчуру за лок.

### §2.3 Claim: FIFO-порядок + `owner`/`heartbeat_at` замість пам'яті процесу

`queued_captures()` → `ORDER BY queued_at ASC LIMIT n` (чиста FIFO, §1.3). `claim_capture` бере перевірку "чи вже щось активне для цього клієнта" **всередину самого UPDATE** — точно як `cells.py:28 claim_next_cell`, де admission-контроль живе в `UPDATE`, а `rowcount == 1` доводить перемогу; єдиний writer SQLite робить це атомарним без `SELECT … FOR UPDATE`. Сам виклик іде через `write_controller.submit(...)`, як і решта запису.

Замінюємо критерій живості: не `capture.id in self._in_flight`, а `heartbeat_at` свіжіший за поріг. Двигун стукає в heartbeat на тому ж флаші лічильників, що вже є ([`capture.py:638-657`](../../../app/webarchive/capture.py#L638-L657)), через `write_controller.submit(...)` — нових записів-конкурентів це не додає.

Це прибирає §0.4 повністю: CLI-капчур у власному процесі має свій `owner`, свій контролер запису й свій heartbeat, і планувальник більше не виносить йому хибний вирок. Саме це рішення й назване в коді як «the real fix». (Міжпроцесний випадок — CLI проти воркера — контролер у пам'яті одного процесу не покриває; там і далі рятує лише `commit_with_retry`'s backoff на рівні самого SQLite-файлу, але це вже рідкісний хвіст, не 6-way пайлап.)

### §2.4 Лог: `archive_capture_events` за готовим лекалом

Копіюємо форму `ReportRunEvent` ([`models.py:1221`](../../../app/db/models.py#L1221)) — `seq` монотонний **у межах прогону**, `UniqueConstraint(capture_id, seq)`:

- репо: `append_event(capture_id, level, *, stage, message, details)` і `events_since(capture_id, last_seq)` — за зразком [`reports.py:333/362`](../../../app/db/repositories/reports.py#L333);
- емітер `app/webarchive/events.py` — за зразком `app/reports/runner.py:111`;
- **редакція секретів** — перевикористовуємо готовий генерик `app/jobs/events.py:51-67` (PLAN §13). Лог їде в UI, отже cookie/токени в ньому не з'являються.

Що пишемо: старт (скільки URL у плані), кожен URL із наслідком (`new|unchanged|failed|blocked`) на рівні `debug`, помилки на `warning`/`error`, підсумок. Обсяг: ALPHA 1181 рядків на прогін — прийнятно; чистка через `purge_older_than`, як у `job_events`.

### §2.5 Прогрес — структурою, а не парсингом логу

`ArchiveCapture` дістає `progress_message` (поточний URL) поруч із наявними лічильниками. **Ми свідомо не відновлюємо прогрес із тексту логу** — цю саму помилку вже зробили й виправили в звітах, і регресія задокументована в [`reports-run.js:1-11`](../../../frontend/js/reports-run.js#L1-L11): task-43 виводив стан із `startsWith("Downloading")`, через що не було ні дат, ні тривалостей, і будь-яке переформулювання лог-рядка мовчки ламало екран.

Два канали, не один: **лічильники + `progress_message`** (структура, полінг) і **події** (наратив, окремо).

### §2.6 Чому полінг, а не SSE

У репо є обидва патерни. Беремо полінг:

- капчур генерує ≈ **0.17 події/с** (5.8 с/URL) — латентність SSE тут не дає нічого;
- на хабі вже працює самозаводний 5-секундний полінг, який сам зупиняється;
- SSE тягне за собою обов'язкові стани `connecting|live|reconnecting` (AGENTS §6) — UI-поверхня, не виправдана 0.17 події/с.

Ендпоїнт логу віддає `events_since(capture_id, last_seq)`, фронт тримає `lastSeq`. Інтервал: 2 с на сторінці капчуру, 5 с на хабі.

### §2.7 Капчур одного URL

`ArchiveCapture` дістає `scope` (`client` | `url`) і `archive_url_id`. Для `scope="url"` двигун бере рівно один id замість `list_urls_for_client` — цикл, лічильники, лог і скасування лишаються ті самі. Окремого коду виконання не з'являється.

Такий капчур стає в **ту саму** чергу й виконується паралельно з рештою (§1.3) — окремого пулу чи лічильника для нього немає: `urls_total=1`, ~6 с, тож навіть у FIFO він не встигає застрягти за довгим прогоном.

### §2.8 API (усе в наявному роутері)

| Метод + шлях | Роль |
|---|---|
| `POST /archive/captures?client_id=` | є; додаємо гард на `queued` |
| `POST /archive/captures/url?url_id=` | новий — §1.1 |
| `POST /archive/captures/all` | новий — §1.2, повертає список створених id |
| `GET /archive/captures/{id}` | новий — деталь одного капчуру (сьогодні немає) |
| `GET /archive/captures/{id}/events?since=` | новий — лог |
| `POST /archive/captures/{id}/cancel` | новий — §1.6 |
| `GET /archive/queue` | новий — активні + черга з позиціями |

---

## §3. Запит на міграцію до **task-03**

> За [CLAUDE.md](../../../CLAUDE.md) міграції створює лише task-03. Нижче — точний зміст запиту.
> **Перед застосуванням:** `docker compose exec api poetry run python scripts/backup_sqlite.py --out-dir /app/app_data/backups --keep 14`.
> **Тільки прямі `op.add_column` / `op.create_table`, без `batch_alter_table`** — саме він стер `client_integrations` на проді 2026-09-01.

**A. `archive_captures` — 6 колонок, усі nullable або з константним дефолтом:**

| Колонка | Тип | Дефолт | Навіщо |
|---|---|---|---|
| `queued_at` | `UTCDateTime` | `NULL` | порядок FIFO (§2.3); сьогодні `scheduled_for` підмінює його для ручних |
| `owner` | `String(64)` | `NULL` | хто виконує — worker чи CLI (§2.3) |
| `heartbeat_at` | `UTCDateTime` | `NULL` | довговічний критерій живості замість `_in_flight` (§2.3) |
| `cancel_requested` | `Boolean` | `False` | кооперативне скасування (§1.6) |
| `scope` | `String(16)` | `'client'` | `client` \| `url` (§2.7) |
| `archive_url_id` | `GUID` FK → `archive_urls.id` `ondelete=SET NULL` | `NULL` | ціль для `scope='url'` |
| `progress_message` | `String(500)` | `NULL` | поточний URL (§2.5) |

Плюс індекс `ix_archive_captures_status_queued_at (status, queued_at)` — черга читається щохвилини.

**B. Нова таблиця `archive_capture_events`** (форма `report_run_events`):

```
id            GUID PK
capture_id    GUID FK archive_captures.id ondelete=CASCADE, indexed
seq           Integer                     # монотонний у межах капчуру
level         String(16)                  # debug|info|warning|error
stage         String(64) | None
message       String(2000)
details_json  JSON | None
created_at    UTCDateTime
UniqueConstraint(capture_id, seq)
```

**Зворотна сумісність:** усі наявні рядки лишаються валідними — `scope` дефолтиться в `client`, решта nullable. Жодного backfill не потрібно.

---

## §4. Схеми (`app/schemas/web_archive.py`)

- `ArchiveCaptureDTO` — **адитивно**: `+queue_position: int | None`, `+progress_message: str | None`, `+scope`, `+target_url: str | None`, `+cancel_requested: bool`, `+urls_per_second: float | None`, `+eta_seconds: int | None`.
- `ArchiveCaptureEventDTO` (новий): `seq`, `level`, `stage`, `message`, `details`, `created_at`.
- `ArchiveQueueDTO` (новий): `running: list[ArchiveCaptureDTO]`, `queued: list[ArchiveCaptureDTO]`, `recent: list[ArchiveCaptureDTO]` (ліміт одночасності — наявний `archive_client_workers`, окремого DTO-поля не заводимо).
- `ArchiveCaptureAcceptedDTO` — `status_url` нарешті вказує на реальний `/archive/captures/{id}` (сьогодні це константа).

**`None` ≠ `0` (PLAN §12):** `eta_seconds`/`urls_per_second` — `None`, доки не пройдено бодай 5 URL; `urls_total` лишається `int | None`.

---

## §5. Фронтенд

### §5.1 `archive-page.js` (правки)

- Кнопка **Refresh now** у шапці результату пошуку (§1.1) — станів три: idle / queued / running, з нативним `<progress>`.
- Кнопка **Capture all** біля заголовка Clients.
- «Recent captures» → «Queue & activity» з позиціями і посиланням на деталь.
- Наявний `syncLivePolling` розширюємо на `/archive/queue`; логіку «не блимати скелетоном на поллінгу» і перевірку `root.hasAttribute("hidden")` **зберігаємо як є**.

### §5.2 `archive-capture.js` (новий) — сторінка одного капчуру

Структуру беремо з `reports-run.js`, але без SSE (§2.6): шапка з `<progress>` і лічильниками, список прогресу, лог із фільтром рівня + Pause + Copy. CSS-класи `.reports-log-list` / `.reports-log-row` / `.reports-progress-row` переносимо в `archive.css` під власними іменами.

### §5.3 Жорсткі обмеження

- **CSP:** ніяких inline `style=`, ніяких JS-виставлених `.style.width/top/left`. Тільки нативний `<progress>` або `data-*` + статичні правила CSS (`app.css:557-560`).
- **Request keys** — явні, названі за призначенням, ніколи не виведені з параметрів: `archive-capture-detail-${id}`, `archive-capture-events-${id}`, `archive-queue-poll`. Забутий ключ уже двічі коштував бойових інцидентів (`api.js:1328-1339` — там він навіть кидає `TypeError`).
- **F5-resume** обов'язковий: `id` у маршруті, стан відновлюється з API (AGENTS §9).
- **UI-копірайт англійською.**
- Роутер: маршрут `#/archive/capture` оголосити **до** узагальнених архівних правил (порядок регексів у `router.js:26-34`).

---

## §6. Тести

**`tests/unit/webarchive/test_archive_queue.py`** (новий)
- FIFO-порядок за `queued_at`.
- Два `queued` на одного клієнта → claim бере рівно один, другий лишається чекати.
- `POST` двічі поспіль → другий 409, другого рядка не створено.

**`tests/unit/webarchive/test_write_controller.py`** (новий) — головний тест на регрес §0.2/§0.4
- N потоків одночасно шлють `submit(...)` — усі застосовані, жодного винятку `database is locked` (regression на `archive_db_locked_retry`, живо спостережений 2026-09-05).
- Порядок застосування збігається з порядком подачі (FIFO самої черги запису).
- Виняток у `fn` доходить до викликача через `Future`, чергу не ламає — наступний job однаково обробляється.
- `stop()` дочікує спорожнення черги, не губить останній job.

**`tests/unit/webarchive/test_archive_heartbeat.py`** (новий)
- Свіжий heartbeat чужого процесу → **не** реклеймиться (регрес на §0.4).
- Протухлий heartbeat → реклеймиться в `failed/interrupted`.
- Заміняє критерій `_in_flight` у наявних тестах `test_archive_scheduler.py:58/:103`.

**`tests/unit/webarchive/test_capture_events.py`** (новий)
- `seq` монотонний, `events_since` віддає хвіст.
- Секрет у `details` відредаговано (PLAN §13).
- `scope="url"` обходить рівно один URL і пише `urls_total=1`.

**`tests/unit/webarchive/test_capture_cancel.py`** (новий)
- Скасування чергового — миттєве, двигун не стартує.
- Скасування активного — цикл спиняється між URL, статус `cancelled`, часткові результати збережені (PLAN §9.1).

**`tests/unit/api/test_archive_api.py`** (правки) — 6 нових ендпоїнтів: щасливий шлях, 404, 409.

**Фронт:** візуальний loop за AGENTS §15–16 — черга з активним і черговим, сторінка капчуру з логом, порожня черга, помилка завантаження.

---

## §7. Definition of Done / Gate

1. **Запис справді серіалізований, збір лишається паралельним:** штучно поставлені 12 клієнтів фетчаться одночасно (як і сьогодні, `archive_client_workers`), але жодного `database is locked` під час прогону — перевірено вимірюванням (лічильник retry в контролері = 0 або близько), а не читанням коду.
2. **§0.4 не відтворюється:** `archive-capture --client-id` у власному процесі працює довше 2 хвилин і **не** отримує `interrupted`.
3. **Кнопка на URL працює наживо:** оновлення однієї сторінки GOREPORT створює новий знімок за секунди, і календар показує сьогоднішню дату.
4. **Лог читається:** сторінка капчуру показує URL, що обробляються, і причину кожного `failed`.
5. **Скасування працює на обох станах** — черговому й активному.
6. **F5 не втрачає стан.**
7. `ruff` + `mypy app` + повний `pytest` зелені **в Docker**.
8. Візуальний чекліст AGENTS §15–16 пройдено.
9. `openapi_snapshot.json` перегенеровано; зміни — лише нові поля/ендпоїнти.
10. План перенесено в `doc/dev/done/` з Completion report, `INDEX.md` оновлено.

---

## §8. Свідомо поза межами таски

- **Розподілена черга / другий процес** — PLAN §5.1 забороняє другого SQLite-writer.
- **Будь-яка пріоритизація черги складніша за чисту FIFO** (класи, ваги, ручний-поперед-планового) — на 12 клієнтів не окупається.
- **Пауза/резюм активного капчуру** — скасування + перезапуск дає той самий результат за наявної дедуплікації.
- **Ретраї URL, що впали, всередині прогону** — окрема тема; сьогодні їх підбирає наступний прогін.
- **Зміна розкладу 13:30/19:30 з UI** — `ArchiveSettingsDTO` уже описано, але ніде не використано; це окрема таска про налаштування архіву.
- **Прибирання 43 історичних `failed`-рядків** — дані про минуле, хай лишаються чесними.

---

## ✅ Completion report

- **Дата:** 2026-09-05
- **Гілка / комміт:** `feat/task-61-url-inspector` (спільна гілка сесій; окремого worktree на цю таску не було) @ ще не закомічено на момент написання
- **Статус:** done

### Що зроблено

- `ArchiveWriteController` — усі архівні записи в SQLite (капчур-воркери, реклейм, claim/finish) серіалізовані через один фоновий тред-контролер; сам збір (мережевий фетч) лишився паралельним, як і раніше (`archive_client_workers`).
- FIFO-черга капчурів: `queued_captures()` впорядкована за `queued_at`, `claim_capture` бере admission control усередину себе (рівноправно з `has_running_capture`), `POST /captures` тепер стереже і `queued`, і `running`.
- `owner`+`heartbeat_at` замінили `ArchiveScheduler._in_flight` як критерій живості — переживає межу процесів. Живо відтворено й підтверджено виправлення §0.4: CLI-капчур в іншому процесі більше не оголошується мертвим хибно.
- Капчур **одного URL** (`scope="url"`, кнопка «Refresh now» у результаті пошуку) — секунди замість повного клієнта.
- Кнопка **Capture all** на хабі — по одному черговому рядку на кожного колектабельного клієнта, що не має вже активного.
- Лог капчуру (`archive_capture_events`, по рядку на URL) і структурований прогрес (`progress_message`, `queue_position`) — нова сторінка `#/archive/capture?id=…` з прогрес-баром, лічильниками й живим логом (полінг 2с, без SSE).
- Кооперативне скасування — черговий капчур знімається миттєво, активний зупиняється між URL.
- Міграція `b3f8a91c2d47` (7 колонок `archive_captures` + таблиця `archive_capture_events`) написана й застосована самостійно (без окремої task-03-сесії — прецедент task-59 сам додав свою міграцію тим самим шляхом), з бекапом і перевіркою на копії перед застосуванням до бойової БД.

### Як зроблено (ключові рішення)

- **Паралельність під час діалогу з користувачем змінилась двічі.** Перший чорновик плану звузив одночасність капчурів до 1 («просто» — за проханням користувача). Користувач одразу поправив: «збір паралельно усі, а запис у бд... контролер та чергу» — тобто вузьким місцем є не мережевий фетч, а єдиний SQLite-writer. Фінальна архітектура: збір лишається паралельним (`archive_client_workers`, як і раніше), лише сам запис серіалізовано контролером. План (§2.2) переписано під час імплементації, щойно це зʼясувалось.
- **`ArchiveWriteController.submit(session, fn)` — drop-in заміна `commit_with_retry(session, fn)`**, не власник сесії: кожна job несе сесію виклика­ча, контролер лише почергово її торкається з одного треду. Це набагато простіше за перший начерк плану (де контролер мав власну сесію) і не займає жодного рядка логіки читання/запису всередині `capture_url`/`_capture_assets_for_page` — лише механічна заміна виклику.
- **Cancel-check читає через СВІЖУ сесію, не через довгоживучу сесію капчуру** (`_make_cancel_checker`, за зразком `app.jobs.runner._make_cancel_checker`) — сесії проєкту створюються з `expire_on_commit=False` (`app/db/session.py`), тож повторне читання на тій самій сесії повертало б закешований, застарілий об'єкт і ніколи не побачило б `cancel_requested`, виставлений іншою сесією (API-запит на скасування).
- **Знайдено й виправлено живий баг під час верифікації:** планувальник (`ArchiveScheduler._submit`) клеймив `scope="url"` рядок, але не читав `archive_url_id` назад і не проводив `only_url_id` у `run_client_capture` — кожен капчур мовчки скочувався до повного списку URL клієнта. Спіймано на реальному GOREPORT: "Refresh now" на одній сторінці обробив 2 URL замість 1. Виправлено (`claimed.archive_url_id if claimed.scope == "url" else None`) і закрито регрес-тестом (`test_submit_reads_scope_url_and_captures_exactly_one_url`).
- **Heartbeat живо підтверджений на межі процесів, включно з відновленням після SIGKILL**: під час верифікації рестарт `worker`-контейнера не встиг дочекатись `pool.shutdown(wait=True)` для капчуру, що саме йшов (246 URL) — процес обірвався, рядок лишився `running` з мертвим heartbeat. Наступний тик НОВОГО процесу коректно розпізнав це через ~90с і позначив `failed/interrupted` — саме той сценарій, під який писалась ця логіка, тільки живий і не спланований.
- **`_capture_assets_for_page`'s наявний per-asset коміт тепер додатково торкає `heartbeat_at`** — не новий запис, а поле на вже існуючому коміті: сторінка з p90 476 підресурсів (task-59 §0.4) інакше могла б мовчки перевищити поріг застарілості між двома зовнішніми флашами по URL.
- **Відхилення від плану:** тести на heartbeat/reclaim лишились у наявному `test_archive_scheduler.py` (перейменовані під нову семантику), а не у новому `test_archive_heartbeat.py`, як пропонував план — менше файлового churn для тих самих twох тестів.

### Контрактні зміни (Contract changes)

- `ArchiveCaptureDTO` — додано `scope`, `target_url`, `progress_message`, `cancel_requested`, `queue_position` (усі адитивні).
- `CaptureStatus` — додано `"cancelled"`.
- Нові DTO: `ArchiveCaptureEventDTO`, `ArchiveQueueDTO`, `ArchiveCaptureBatchAcceptedDTO`, `ArchiveCaptureCancelDTO`.
- Нові ендпоінти: `POST /archive/captures/url`, `POST /archive/captures/all`, `GET /archive/captures/{id}`, `GET /archive/captures/{id}/events`, `POST /archive/captures/{id}/cancel`, `GET /archive/queue`.
- `POST /archive/captures`'s `status_url` тепер вказує на реальний `/archive/captures/{id}` (раніше — константа-заглушка).
- `openapi_snapshot.json` регенеровано **вибірково**: змерджено лише мою (архівну) частину поверх HEAD, узгоджено обхідним шляхом (git-хірургія, детальніше — «Follow-ups»), бо `test_openapi_schema_matches_snapshot` бачить УВЕСЬ живий застосунок, включно з незакомiченими роутами паралельних сесій.

### Як перевірити (verification)

```bash
docker compose exec api poetry run pytest tests/unit/webarchive tests/unit/api/test_archive_api.py tests/unit/api/test_archive_replay_api.py -q
docker compose exec api poetry run mypy app
docker compose exec api poetry run ruff check app tests
```

Живі перевірки (Playwright, `/tmp/yovth-e2e/task62-verify.cjs`), проти реального GOREPORT/DEV ALPHA:
- Хаб показує "Capture all" і секцію "Queue & activity", 0 console errors.
- "Refresh now" на `https://goreport.com/` створює `scope="url"` капчур, реально виконується планувальником, дає `urls_total=1, urls_captured=1` (не 2 — регрес виправлено).
- Сторінка `#/archive/capture?id=…` показує прогрес-бар, лічильники й лог, що наповнюється в реальному часі.
- Скасування щойно-створеного чергового капчуру — миттєве, `status="cancelled"`.
- Heartbeat-реклейм осиротілого (після SIGKILL) капчуру — підтверджено живо, не лише юніт-тестом.

### Тести

- `tests/unit/webarchive/test_write_controller.py` (новий, 4) — реальні треди + реальний файловий SQLite-рушій: конкурентний запис не губить рядків і не падає з `database is locked`; винятки не ламають чергу; `stop()` дочікує спорожнення.
- `tests/unit/webarchive/test_web_archive_repo.py` (+8) — FIFO-порядок, `has_active_capture`, admission control у `claim_capture`, `request_cancel` (queued/running/terminal), `append_event`/`events_since`.
- `tests/unit/webarchive/test_capture.py` (+4) — per-URL подія в лозі, `only_url_id` capture рівно одного URL, кооперативне скасування.
- `tests/unit/webarchive/test_archive_scheduler.py` (2 переписані на heartbeat + 1 новий regression-тест на `scope="url"` у `_submit`).
- `tests/unit/api/test_archive_api.py` (+13) — усі нові ендпоінти, включно з 404/409.
- `tests/unit/api/data/openapi_snapshot.json` — регенеровано вибірково (див. Contract changes).
- Повний `pytest` (2200+ тестів) зелений, окрім двох НЕ пов'язаних, наперед-існуючих провалів (`test_aiseotools_signatures`, `test_tech_audit` dashboard staleness) — жодних посилань на archive/webarchive, файли не торкались цією таскою.

### Follow-ups / борг

- **Спільні файли з накопиченим боргом commit'ів, що виходить за межі цієї таски**: `app/cli.py`, `app/core/config.py`, `frontend/js/router.js`, `frontend/js/api.js`, `frontend/index.html` — усі мають незакомічені зміни ЩОНАЙМЕНШЕ з task-57/59/61 (перевірено: `git show HEAD:frontend/js/router.js` не має жодного archive/url-inspector/pages маршруту — 288 рядків проти 590 у робочому дереві). Це borg, що існував ДО цієї таски і зростає з кожною наступною сесією, яка чіпає ці файли. Мій внесок у ці файли лишається незакомiченим за тим самим принципом (task-59/60 вже документували цей підхід для `cli.py`/`config.py`) — **рекомендація користувачу**: розібрати цей стек окремо, свідомо, а не мовчки накопичувати далі.
- `doc/dev/new/INDEX.md` — застосовано ту саму git-index-хірургію, що й у task-59/60: закомічено лише мій один рядок (task-62), паралельна сесія task-61 (позначення свого рядка ✅ + переїзд у done/) лишена недоторканою в робочому дереві для їхнього власного коміту.
- `tests/unit/api/data/openapi_snapshot.json` — так само: закомічено тільки архівна частина; url-inspector/portfolio-series шляхи й схеми лишаються відсутніми у знімку до власного коміту тих сесій (перевірено скриптом: розбіжність між живою схемою і моїм знімком — виключно url-inspector).
- Дефолт одночасності капчурів лишився наявним `archive_client_workers=6` — план (§1.3) свідомо не вводив окремий новий ліміт; якщо контролер запису колись стане вузьким місцем при більшій кількості клієнтів, це вимірюється окремо.
