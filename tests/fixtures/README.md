# Sanitized fixtures (task-13)

Спільні фікстури для бекенду й фронту (PLAN Етап 0). Володіє ними `task-13`;
решта тасок їх **читає**, не редагує.

## Що тут лежить

| Шлях | Що це |
|---|---|
| `aiseotools_source/` | Знеособлена копія дерева `--source` AISeoTools: `clients.json` + `out/` з артефактами всіх чотирьох родин, які розпізнає імпортер (task-07). |
| `aiseotools_source/out/.gsc_cache/` | Навмисне «сміття»: доводить, що сканер ігнорує кеш-директорії. |

`client_report_*.xlsx` **не комітиться** — генерується на льоту
(`harness.write_client_report`). Причина: xlsx — бінарник, у якому
неможливо очима перевірити відсутність секретів перед комітом, а це прямий
ризик із плану таски.

Фікстури фронту живуть окремо, у [`frontend/fixtures/`](../../frontend/fixtures/)
(власник — task-12), і навмисно не дублюються сюди: у них інший контракт —
це вже відповіді API (`DashboardDTO`), а не сирі провайдерські артефакти.

## Правила санітизації

- домени — лише з зарезервованого `example.com`;
- коди клієнтів вигадані (`ACME-DENTAL`, `BETA-CLINIC`);
- `connection_key` — це **email Google-акаунта**, не ключ; тут стоїть
  неіснуючий `seo@example.com`;
- жодних cookies, API-ключів, session-файлів, шляхів до browser-профілів.

Перевіряється автоматично: `tests/integration/test_security_hardening.py::test_committed_fixtures_carry_no_secrets`.
