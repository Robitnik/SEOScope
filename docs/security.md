# Безпека

Що вже забезпечено, чим це перевіряється і що **обов'язково** зробити перед
виставленням дашборду в публічну мережу. Джерело вимог — [PLAN.md §13](../PLAN.md).

## 1. Модель загроз і scope розгортання

MVP розрахований на **локальну/VPN-інсталяцію для команди агенції**: дані —
метрики клієнтів, не PII кінцевих користувачів; довірена аудиторія. Звідси
рішення: на MVP достатньо auth reverse proxy перед `api`, app-level логіну
немає (див. §5 — що змінюється перед public exposure).

## 2. Секрети

**Правило:** API-ключі, паролі й session-cookies не потрапляють у БД, події,
логи та у помилки, віддані в UI.

Як це тримається:

- ключі провайдерів живуть у `.env` **AISeoTools** і монтуються в контейнер
  лише в рантаймі; в образ вони не запікаються;
- `client_integrations.config_json` зберігає лише **несекретні** посилання:
  домен, `account_id`, назву профілю, `unit_budget`. `connection_key` — це
  email Google-акаунта (ідентифікатор з'єднання Reporting Ninja), не
  credential;
- browser-профілі та `.ninja_session.json` — окремий volume, не в образі;
- усе, що адаптер емітить у `ProgressEvent`, проходить редакцію в
  [`app/jobs/events.py`](../app/jobs/events.py): маскуються ключі
  (`authorization`, `cookie`, `password`, `api_key`, `token`, `secret`,
  `session*`) і патерни (`Bearer …`, `api_key=…`);
- помилка збору перетворюється на безпечну пару
  `(тип винятку, "collection failed; see server logs for detail")` —
  сирий текст винятку в UI не потрапляє ніколи.

**Перевірка:** `tests/integration/test_security_hardening.py` реєструє навмисно
«протікаючий» адаптер (Bearer у повідомленні, `api_key`/`cookie` в
`details`, секрет у тексті винятку) і сканує весь файл БД на canary-значення.
Той самий тест сканує закомічені фікстури й `.env.example`.

## 3. Транспорт і застосунок

- security-заголовки на кожній відповіді: `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
  `Content-Security-Policy: default-src 'self'`, обмежувальний
  `Permissions-Policy`;
- CORS за замовчуванням порожній (same-origin); `CORS_ALLOW_ORIGINS`
  розширює список свідомо, `allow_credentials=False`;
- фронт не має інлайнових скриптів і зовнішніх хостів — CSP виконується
  без послаблень;
- UI не приймає довільних shell-команд, CLI-прапорців, локальних шляхів чи
  URL провайдерів.

## 4. Файли й artifacts

- `GET /api/v1/artifacts/{id}` віддає лише шлях, який
  [`safe_resolve`](../app/core/security.py) вже підтвердив як такий, що
  резолвиться **всередині** artifacts root. Абсолютний шлях або `../`
  відхиляється і на записі, і на видачі; назовні це виглядає як `404`,
  без натяку на структуру ФС;
- завантаження логотипа валідує MIME/сигнатуру/розмір і зберігає під
  згенерованим безпечним іменем;
- імпорт з AISeoTools **read-only**: `--source` ніколи не змінюється
  (перевіряється хешем усього дерева в
  `tests/integration/test_importer_backfill.py`).

## 5. Перед виставленням у публічну мережу — обов'язково

Поточний стан для приватної мережі достатній. Для public exposure **до**
релізу потрібно (PLAN §13):

- [ ] app-level login + secure `HttpOnly` `SameSite=Strict` cookie;
- [ ] CSRF-захист на всіх мутуючих ендпоінтах (`POST/PUT/PATCH/DELETE`);
- [ ] audit trail: хто запустив збір, хто змінив інтеграцію, хто завантажив
      artifact;
- [ ] ownership-перевірка на `GET /artifacts/{id}` (зараз шлях безпечний,
      але доступ не персоналізований — усі автентифіковані бачать усе);
- [ ] HTTPS + HSTS на reverse proxy, rate limit на `/api/v1/sync-jobs`.

Сейм для `app/api/deps.py::current_actor` — це свідома заглушка (`"system"`),
єдиний шов, у який вмикається справжня автентифікація; міняти доведеться
саме її, а не кожен роутер.

## 6. Що робити при підозрі на витік

1. Відкликати ключ у провайдера й перелогінитись у browser-профіль.
2. Перевірити, чи справді щось осіло в БД:
   ```bash
   docker compose exec api poetry run python - <<'PY'
   import sys; sys.path.insert(0, "tests")
   from harness import scan_sqlite_for_secrets
   print(scan_sqlite_for_secrets("app_data/app.db", extra=("<підозріле значення>",)))
   PY
   ```
3. Якщо знайшлось — це баг редакції: додати ключ/патерн у
   `app/jobs/events.py` (власник — task-05) і закрити тестом.
4. Події з витоком прибираються `scripts/retention_cleanup.py --days 7`
   (мінімально допустиме вікно) або точковим `DELETE` за `job_id`.
