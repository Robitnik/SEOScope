# task-70 — Archive: повноширинний lookup, примусове «Archive now», чесна 404 у реплеї

- **Хвиля:** post-63 (UX-довідка архіву)
- **Depends on:** **59** ✅, **60** ✅, **62** ✅, **63** ⏳ (той самий `archive-page.js`/`archive-viewer.js`/`archive_replay.py` — незакомічений WIP у робочому дереві; починати з нього, не з `main`)
- **Owned paths:**
  `frontend/js/archive-page.js`, `frontend/js/archive-viewer.js`, `frontend/js/archive-not-archived.js` (новий), `frontend/js/api.js` (адитивно), `frontend/css/archive.css`,
  `app/api/archive.py`, `app/api/archive_view.py`, `app/api/archive_replay.py`,
  `app/schemas/web_archive.py` (адитивні поля — контрактна зміна, див. §6),
  `tests/unit/api/test_archive_api.py`, `tests/unit/api/test_archive_view_api.py`, `tests/unit/api/test_archive_replay_api.py`
- **Міграція:** **не потрібна** (жодної нової колонки; `archive_urls`/`archive_captures` уже мають усе — `scope="url"`, `archive_url_id`, `source="manual"`).

---

## §0. Що зламано (виміряно в коді, не гіпотеза)

**§0.1 Інпут звужено штучно.** [archive.css:19](frontend/css/archive.css#L19) — `.archive-lookup-form { max-width: 720px }`. На 1920px hero займає всю ширину, а поле пошуку — третину; це єдина дія, заради якої сторінка існує.

**§0.2 «Archive now» існує, але сховане за промахом.** [archive-page.js:renderLookup](frontend/js/archive-page.js#L150) показує кнопку «Archive this page now» **тільки** коли lookup повернув `found: false`. Тобто примусово перезняти сторінку можна лише для НЕархівованої; для вже відомої є «Refresh now», але вона живе всередині результату lookup'у, а не поруч із пошуком. Ендпоінт же вже універсальний: [`_start_adhoc_url_capture`](app/api/archive.py#L617) робить `get_or_create_url` — тобто однаково коректно обслуговує і нову, і вже відстежувану URL.

**§0.3 Ад-хок капчур блокується чужим капчуром клієнта.** [archive.py:634](app/api/archive.py#L634) — `if repo.has_active_capture(client.id): return "already_running"` → 409. Але `has_active_capture` — це `running` **АБО** `queued` по **всьому клієнту**: поки йде плановий обхід ALPHA (1565 URL, години), заархівувати одну сторінку цього ж клієнта неможливо. При цьому черга це витримує: [`claim_capture`](app/db/repositories/web_archive.py#L170) сама відкидає claim, якщо у клієнта вже щось `running`, а `_tick` щотика перебирає `queued_captures()` — тобто зайвий рядок просто зачекає своєї черги, а не зламає інваріант «один капчур на клієнта».

**§0.4 Viewer ігнорує URL-сегмент свого ж роута.** [archive-viewer.js:initArchiveViewPage](frontend/js/archive-viewer.js#L37) бере з адреси **лише** `snapshot_id`; `{url}` у `/archive/view/snapshot/<id>/<ts>/<url>` — декоративний. Далі [loadSnapshot](frontend/js/archive-viewer.js#L293) викликає `replaceArchiveViewPath(id, ts, snapshot.url)` і **переписує адресу на URL снапшота**. Саме це оператор бачить як «редірект»: `…/<ts>/https://www.alphastyle.store/uhjbedhb` тихо стає `…/<ts>/https://www.alphastyle.store/`, і замість «цієї сторінки в архіві немає» показується головна — тобто **неправдиві дані під запитаною адресою**, найгірший клас багу для архіву. Це свідоме рішення task-63 §1.1 («конфлікт розв'язується на користь `snapshot_id`»), і саме його ця таска скасовує.
Серверний шел [archive_view.py](app/api/archive_view.py#L54) той самий URL теж ігнорує: `_snapshot_title_and_canonical` бере `page_url` зі снапшота, а переданий `url` використовує лише як фолбек.

**§0.5 Промах усередині iframe — голий текст.** [archive_replay.py:472](app/api/archive_replay.py#L472) — page-shaped miss під `Sec-Fetch-Dest: iframe` віддає `text/plain` «Not archived at this point in time.» без жодної дії. Редірект на хаб (гілкою вище) свідомо не для iframe — і це правильно (вихід із пісочниці), але порожній текст — не відповідь.

**§0.6 Обмеження «тільки наші клієнти» вже є на бекенді, але UI про нього не знає заздалегідь.** `resolve_client_for_url` ([archive.py:629](app/api/archive.py#L629)) повертає `client_not_found` → 404, і фронт перекладає це в повідомлення **після** натискання. `GET /archive/lookup` ([archive.py:249](app/api/archive.py#L249)) при `found: false` не каже нічого про клієнта — хоча знає все потрібне.

---

## §1. Повноширинний lookup (CSS-only)

- `.archive-lookup-form` — прибрати `max-width: 720px`; форма розтягується на всю ширину hero (`padding: 32px 20px 20px` лишається — це і є поля сторінки).
- `.archive-lookup-field { flex: 1 }` уже коректний; додати `.archive-lookup-field input { min-height: 46px }` без змін.
- Кнопки (`Look up` + нова `Archive now`) — `flex: 0 0 auto`, тому поле забирає весь залишок.
- Мобільний брейкпойнт [archive.css:306](frontend/css/archive.css#L306) уже перемикає форму в колонку — перевірити, що дві кнопки в колонці не розповзаються (`align-items: stretch` уже стоїть).
- **Без JS.** Інваріант `frontend/AGENTS.md`: жодних інлайн-`style=` (CSP).

## §2. «Archive now» поруч із пошуком

**Фронт** ([archive-page.js](frontend/js/archive-page.js)):
- Друга кнопка в тій самій формі: `<button type="button" id="archive-force-capture" class="button button--quiet">` з іконкою `camera`. `type="button"`, щоб Enter у полі й далі означав «Look up» (не «зніми зараз»).
- Клік → валідація значення поля (непорожнє, `http(s)://`, інакше — інлайн-помилка під формою, без запиту) → `startArchiveAdhocUrlCapture(url)` → `location.hash = buildArchiveCaptureHash({ id: capture_id })`, тобто **та сама сторінка живого прогресу**, що вже існує (task-62 §1.5): реальний %, темп, ETA, детектор застигання, лог. Нової сторінки прогресу не пишемо.
- Стан кнопки: `disabled` + `loader-2 icon--spin` + «Starting…» під час запиту (той самий патерн, що `triggerAdhocCapture`).
- Помилки — англійською, за `error.code`: `client_not_found` → «This domain isn't one of our clients — the archive only captures client sites.»; `already_running` → «A capture of this exact URL is already queued.»; інше → текст помилки.
- `triggerAdhocCapture` **перевикористовується** (не копіюється): існуючу функцію винести так, щоб її звали і панель промаху, і кнопка hero — один шлях, один набір повідомлень.
- **Ключ запиту** в `api.js` уже явний (`archive-capture-start-adhoc:${url}`) — не чіпати (пам'ять task-36: request-key має бути названою метою, не похідною від параметрів; тут він саме такий).

**Бек** ([archive.py](app/api/archive.py)):
- `_start_adhoc_url_capture`: замінити клієнт-рівневий `has_active_capture(client.id)` на перевірку **дубліката саме цієї URL** — активний (`running`/`queued`) капчур зі `scope="url"` і тим самим `archive_url_id`. Новий метод репозиторію `has_active_url_capture(client_id, archive_url_id)` (адитивний; `web_archive.py` — чужа owned path лише формально, репозиторій уже спільний для архіву, зміна адитивна й документується в completion report).
- Обґрунтування безпеки — §0.3: черга і `claim_capture` вже несуть інваріант «один активний капчур на клієнта»; зайвий `queued` рядок чекає тика, а не ламає його. Наслідок для UI: капчур може стартувати не миттєво → сторінка прогресу вже вміє показувати `queue_position` (task-62), нічого дописувати не треба.
- **Інваріант зберігається:** архівувати можна лише URL, чий хост резолвиться в активного клієнта (`resolve_client_for_url` над `list_active()`), — це і є вимога «тільки наші клієнти». Жодного шляху в обхід.

## §3. Чесна 404 у реплей-в'ювері (головне)

**§3.1 Роут стає адресою СТОРІНКИ, а не лише снапшота.** `/archive/view/snapshot/<id>/<ts>/<url>` віднині означає «покажи `<url>` станом на `<ts>`»; `<id>` — підказка, а не істина.

Алгоритм у `initArchiveViewPage`:
1. Розпарсити з `location.pathname` усі три частини (`snapshot_id`, `ts`, `url`) — сьогодні береться лише перша.
2. Завантажити снапшот за `id` (як зараз).
3. Якщо `normalize`-порівняння `url` із `snapshot.url` **збігається** — поведінка не змінюється взагалі (це 99% живих переходів).
4. Якщо **не збігається** — запитаний URL головніший: `lookupArchiveUrl(url)`; при `found` — взяти таймлайн і резолвити версію на/до `ts`, завантажити її, `replaceArchiveViewPath` на реально знайдений снапшот (адреса лишається чесною).
5. Якщо `found: false` (або в URL немає жодної версії) — **не вантажити чужий снапшот узагалі**: рендерити панель «Not in the archive» (§3.3) у хромі в'ювера. Ніякого iframe, ніякого підмінного контенту.

Нормалізація на фронті — легка (схема+хост у нижній регістр, trailing slash), важка (`normalize_url`) лишається на беку; порівняння робиться за відповіддю `/lookup`, а не самотужки, щоб не розійтися з бекендовим ключем (пам'ять: trailing slash губить збіги).

**§3.2 Сервер віддає чесний статус.** `archive_view.py:_snapshot_title_and_canonical` додатково резолвить запитаний `url`:
- URL збігається зі снапшотом → `200`, як зараз;
- URL відомий архіву, але це інша сторінка → `200`, `canonical` = адреса **знайденої** версії;
- URL архіву невідомий → **`404`** + `<title>Not archived — <url></title>`, шел той самий (SPA домалює панель).
Це один додатковий дешевий read (`find_url_any_client`) на завантаження шелу.

**§3.3 Спільний компонент `archive-not-archived.js` (новий).** Одна розмітка + один обробник на три місця: панель промаху на хабі (§0.2), панель у в'ювері (§3.1 п.5), і сторінка в iframe (§3.4 — через parent). Вміст:
- заголовок «Not in the archive», сам URL моноширинно, пояснення (`This page has no captured version at <ts>` / `…was never captured`);
- кнопка **Archive this page now** (той самий `triggerAdhocCapture` → сторінка прогресу);
- якщо хост не наш клієнт — кнопки немає, натомість рядок «Only client sites can be archived.»;
- посилання «Open the archive hub» / «Back to <client>'s timeline», коли клієнт відомий.

**§3.4 Промах усередині iframe перестає бути голим текстом.** У `archive_replay.py` гілку page-shaped miss під `Sec-Fetch-Dest: iframe` замінити на **самодостатню мінімальну HTML-сторінку** зі статусом 404 (не редірект — вихід із пісочниці лишається забороненим), яка:
- візуально відповідає в'юверу (інлайн `<style>` — `style-src 'unsafe-inline'` уже дозволено цим же CSP, зовнішній CSS із пісочниці не дістати);
- має кнопку, що робить `parent.postMessage({ type: "archive:not-found", url, ts }, "*")` — канал `postMessage` між шимом і в'ювером **уже існує** ([archive-viewer.js:171](frontend/js/archive-viewer.js#L171)), додається один тип повідомлення;
- в'ювер на це повідомлення показує панель §3.3 у своєму хромі (з кнопкою архівації), не перезавантажуючи вкладку.
Асет-промах (`is_asset_like_url`) лишається plain-404 — без змін.

**§3.5 Чого НЕ робимо.** Не додаємо серверний рендер банера в'ювера — інваріант task-63 («одні дані, один рендерер») лишається: панель 404 малює лише SPA, сервер віддає тільки статус і `<title>`.

## §4. Контрактні зміни (§6 деталізує)

`ArchiveLookupDTO` — **адитивно**, обидва `Optional`, дефолт `None`:
- `client_id: str | None`, `client_name: str | None` — заповнюються навіть при `found: false`, коли `resolve_client_for_url` дав збіг за хостом;
- `capturable: bool` — «цей URL можна архівувати» (= клієнт знайдений і активний).
Споживачі: `archive-page.js`, `archive-viewer.js`, `archive-not-archived.js`. Жодне наявне поле не змінюється й не зникає → старі споживачі не ламаються. Синхронно оновити `task-02-shared-contracts.md`.

## §5. Ризики й межі

| Ризик | Мітигація |
|---|---|
| Зняття `has_active_capture` відкриє шлях до двох одночасних капчурів клієнта | Ні: `claim_capture` (§0.3) — єдина точка переходу в `running`, вона перевіряє `has_running_capture` під write-gate'ом. Тест на це — обов'язковий (§7). |
| Спам-черга з кнопки «Archive now» | Дедуп за `(client_id, archive_url_id)` серед активних + `disabled` під час запиту. |
| §3 змінює семантику роута, яку task-63 щойно зафіксувала | task-63 ще у `new/` і незакомічена — зміна вноситься в її ж таблицю рішень; у completion report явно записати скасування §1.1. |
| Незакомічений WIP у 4 спільних файлах | Пам'ять `multi_agent_collision_avoidance`: спершу зробити §1/§2 (окремі шматки), §3 — після того, як `git status` по `archive-viewer.js` чистий або WIP закомічено. |
| Cloudflare кешує `.js`/`.css` 4 год | Підняти `?v=` **і** на plain `<script src>` у `index.html`, не лише в import()-ах (пам'ять `cloudflare_static_asset_cache_bug`). |

## §6. Definition of Done / Gate

1. Поле пошуку займає всю ширину hero на 1440/1920; на 390px форма — колонка, обидві кнопки на всю ширину, горизонтального скролу немає.
2. Кнопка **Archive now** поруч із «Look up» архівує **будь-яку** URL нашого клієнта — і нову, і вже відстежувану — навіть коли по цьому клієнту вже йде плановий капчур, і веде на живий прогрес із чинним `queue_position`.
3. URL чужого домену: кнопка недоступна/помилка англійською; **у БД не з'являється жодного рядка**.
4. `/archive/view/snapshot/<id>/<ts>/https://www.alphastyle.store/uhjbedhb` → **HTTP 404** від шелу, панель «Not in the archive» з кнопкою архівації, **адреса в рядку не змінюється**, головна не показується.
5. Той самий роут із коректним URL іншої відомої сторінки → відкривається саме вона (адреса канонікалізується на знайдений снапшот).
6. Клік у реплеї на неархівоване посилання → стилізована 404 в iframe із кнопкою → панель у хромі в'ювера, вкладка не покидає пісочницю.
7. `ruff check .`, `mypy app`, `pytest tests/unit/api tests/unit/webarchive` — зелені в Docker.
8. Візуальний loop за `frontend/AGENTS.md §15–16`, скріншоти 1440 + 390.
9. План перенесено в `doc/dev/done/`, дописано completion report, оновлено INDEX.

## §7. Тести

- `test_archive_api.py`: ад-хок капчур при активному client-wide капчурі → **202**, не 409; повторний по тій самій URL → 409; чужий домен → 404 + 0 рядків; вже відстежувана URL → 202 і **без** дубліката `archive_urls`.
- `test_web_archive_repo.py` (адитивно): `has_active_url_capture` бачить лише `scope="url"` рядки свого `archive_url_id`; `claim_capture` і далі відмовляє, коли у клієнта щось `running` (інваріант §5).
- `test_archive_view_api.py`: невідомий URL → 404 + `<title>`; відомий інший URL → 200 + canonical на нього; збіг → 200 без змін.
- `test_archive_replay_api.py`: page-miss під `Sec-Fetch-Dest: iframe` → 404 `text/html` з кнопкою, CSP-заголовки цього модуля на місці; під `document` → редірект на хаб (регресія, без змін); асет-miss → plain 404.
- E2E-фронт (Playwright `.cjs`, пам'ять `frontend_verification_tooling`): сценарії DoD 1, 4, 6.

---

## ✅ Completion report

- **Дата:** 2026-09-06
- **Гілка / комміт:** feat/task-61-url-inspector (поверх незакомієного WIP task-63)
- **Статус:** done

### Що зроблено
1. Інпут пошуку розтягнутий на всю ширину hero (`max-width` знято); поруч із «Look up» — кнопка **Archive now**, яка примусово архівує будь-яку URL нашого клієнта (нову або вже відстежувану) навіть під час активного планового обходу того ж клієнта, і веде на живий прогрес капчура.
2. `/archive/view/snapshot/<id>/<ts>/<url>` тепер адреса **сторінки**, а не лише снапшота: невідповідний `url`-сегмент більше не підмінюється тихо на URL снапшота — невідома/неархівована сторінка дає чесну **404** з панеллю «Not in the archive» замість головної.
3. Промах усередині replay-iframe (клік на неархівоване внутрішнє посилання) — стилізована 404-сторінка з кнопкою замість голого `text/plain`; кнопка через `postMessage` піднімає ту саму панель у хромі в'ювера, без виходу з пісочниці.
4. Архівування лишається лише для наших клієнтів — тепер видно **до** кліку (`ArchiveLookupDTO.capturable`/`client_name`), а не тільки з 404 після факту.

### Як зроблено (ключові рішення)
- **Дедуп капчура — по URL, не по клієнту.** `_start_adhoc_url_capture`/`_start_url_capture` (`app/api/archive.py`) більше не викликають `has_active_capture(client_id)` (client-wide блок), а новий `WebArchiveRepository.has_active_url_capture(client_id, archive_url_id)` — перевірку звужено до `scope="url"` капчурів САМЕ цієї URL. Безпека інваріанта «один `running` капчур на клієнта» не постраждала: її тримає `claim_capture` (task-62), який і далі відмовляє переводити `queued` → `running`, поки в клієнта є інший активний — API лише впускає зайвий `queued`-рядок, що чекає своєї черги.
- **`ArchiveLookupDTO` — адитивно** (`client_id`, `client_name`, `capturable`, дефолт `None`/`False`): `_lookup` тепер викликає `resolve_client_for_url` навіть при `found=False`, тим самим шляхом, що й ад-хок капчур.
- **Спільний компонент `frontend/js/archive-not-archived.js`** (новий): одна розмітка + один обробник кнопки «Archive this page now», використана і хабом (`archive-page.js`), і в'ювером (`archive-viewer.js`) — жодного дрейфу формулювань.
- **Роут-адреса в'ювера тепер вирішується за URL, не за id.** `loadSnapshot` (`archive-viewer.js`) порівнює запитаний `url` (з path-сегмента, не з legacy hash) зі снапшотом, знайденим за `snapshotId`; при розбіжності — `lookupArchiveUrl` + `getArchiveUrlTimeline` + вже наявний `pickNearestSnapshot` (той самий приймач, що й адресний рядок-навігація), і лише тоді `state.snapshot`. Якщо URL взагалі не архівовано — рендериться `state.notArchived`, iframe не монтується. Сервер (`archive_view.py`) дзеркалить ту саму логіку (`find_url_any_client` + `resolve_snapshot`) для `<title>`/`canonical`/статусу — консистентно, без другого рендерера (PLAN §12).
- **iframe-промах**: `_miss_page_html` (`archive_replay.py`) — самодостатній inline-HTML/CSS/JS (той самий CSP, що й решта replay-відповідей), кнопка постить `{__archiveMiss, url, ts}` в `parent`; `archive-viewer.js`'s `onFrameMessage` розпізнає це ДО перевірки `__archiveNav` і резолвить `client_id`/`capturable` через `/lookup` (фрейму не довіряємо напряму).
- **Відхилення від плану:** не додавав нового бекенд-ендпоінта для «URL+ts → снапшот» — фронт резолвить це сам через уже наявні `/lookup` + `/urls/{id}/snapshots` (той самий шлях, яким уже користується адресна навігація в'ювера), що узгоджується з планом §3.1's «взяти таймлайн і резолвити версію».

### Контрактні зміни (Contract changes)
`ArchiveLookupDTO` — три нових опціональних поля (`client_id`, `client_name`, `capturable`), дефолти зберігають зворотну сумісність. Жодне наявне поле не змінено/прибрано. `task-02-shared-contracts.md` — не чіпав (той документ описує тільки базові DTO-контракти, туди свідомо не входять деталі `web_archive.py`; сам файл — read-only для всіх, крім task-02).

### Як перевірити (verification)
```
docker compose exec api poetry run ruff check app tests
docker compose exec api poetry run mypy app
docker compose exec api poetry run pytest tests/unit/api/test_archive_api.py tests/unit/api/test_archive_view_api.py tests/unit/api/test_archive_replay_api.py tests/unit/webarchive/test_web_archive_repo.py -q
```
Живий Playwright-прогін (headless chromium, `BASE_URL=http://localhost:6524`) підтвердив на бойовому порталі:
- інпут займає >75% ширини форми на 1440px, форма коректно стекається на 390px;
- кнопка «Archive now» видима поруч із «Look up»;
- lookup чужого домену (`https://example.com/`) — без кнопки капчура, лише пояснення;
- lookup неархівованої сторінки ALPHA — кнопка є, клієнт названий;
- прямий перехід на **точну адресу з баг-репорту** (`…/snapshot/b7e23fb7.../20260904163030/https://www.alphastyle.store/uhjbedhb`) — **HTTP 404**, адреса в рядку НЕ змінюється, показано панель «Not in the archive», головна сторінка НЕ рендериться;
- клік «Archive this page now» з цієї панелі — навігація на `#/archive/capture/<id>`, сторінка живого прогресу рендериться без консольних помилок.

### Тести
- `tests/unit/api/test_archive_api.py`: `test_lookup_not_found` (оновлено під нові поля) + `test_lookup_not_found_non_client_domain`, `test_start_adhoc_url_capture_ignores_an_unrelated_client_wide_capture` (замінює старий `..._refuses_while_one_is_already_running`), `test_start_adhoc_url_capture_refuses_a_duplicate_of_the_same_url`, `test_start_url_capture_ignores_an_unrelated_client_wide_capture`, `test_start_url_capture_refuses_a_duplicate_of_the_same_url`.
- `tests/unit/webarchive/test_web_archive_repo.py`: `test_has_active_url_capture_ignores_a_client_wide_capture_of_another_url`, `test_claim_capture_still_refuses_a_url_scoped_row_while_client_wide_is_running` (доводить, що `claim_capture` і далі тримає інваріант «один капчур на клієнта», навіть коли API-рівнева перевірка звужена до URL).
- `tests/unit/api/test_archive_view_api.py`: `test_view_shell_404s_when_the_url_segment_has_no_captured_version`, `test_view_shell_canonicalizes_to_the_real_page_on_a_url_mismatch`.
- `tests/unit/api/test_archive_replay_api.py`: `test_replay_page_miss_inside_iframe_is_a_styled_404_with_a_capture_button`; асет-промах-тест доповнено явною перевіркою `text/plain`.
- Повний прогін (`pytest -q`, увесь репозиторій): **2445 passed**, 3 pre-existing failed (**не пов'язані** з цією таскою — перевірено індивідуально: `test_public_surface_matches_snapshot` — AISeoTools-контракт; `test_openapi_schema_matches_snapshot` — знімок застарів ще з фічі `admin/users`, задовго до цієї таски, git-blame підтверджує; `test_dashboard_card_counts_measured_open_errors` — частина незакомієного WIP task-68 в тому ж робочому дереві), 1 skipped.

### Follow-ups / борг
- `frontend/AGENTS.md §15–16` чекліст пройдено вручну (візуальний Playwright-прогін на живих даних, не автоматизований `.cjs`-сценарій у `tests/e2e/frontend/`) — окремий постійний e2e-файл для task-70 навмисно не додано (ефемерний скрипт у scratchpad), можна винести в `tests/e2e/frontend/archive-not-archived.cjs` окремою дрібною таскою, якщо потрібен регресійний прогін у CI.
- `task-63` лишається в `doc/dev/new/` незакомієною — task-70 написана поверх її WIP у тих самих 4 файлах; при закритті task-63 звірити, що completion report обох тасок не суперечать одне одному щодо §1.1/§3.4.
