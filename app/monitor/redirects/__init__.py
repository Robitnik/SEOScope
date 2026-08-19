"""Моніторинг редіректів сайту (task-51): щогодинний знімок правил →
diff за фінгерпринтом → вікно появи для стрічки змін.

- `contract.py` — спільний тип `RedirectRule`/`RedirectSourceResult`, проти
  якого кодяться обидва джерела.
- `wp_redirection.py` — основне джерело: плагін Redirection (`redirection/v1`).
- `probe.py` — фолбек: blackbox-трасування відомих URL для сайтів без нього.
- `detect.py` — автовизначення джерела з `/wp-json/`.
- `fingerprint.py` — що вважається «зміною» правила (без `last_access`/`last_count`).
- `diff.py` — чистий діф двох знімків (added/changed/removed).
- `severity.py` — класифікація серйозності зміни.
- `credentials.py` — Application Passwords файлом поза БД.

Жоден модуль тут не знає про БД/FastAPI: інтеграція з `app/monitor/runner.py`,
`app/db/repositories/monitor.py`, `app/api/monitor.py` і рештою спільної зони
монітора йде окремим кроком (task-51 план, шапка — не паралелити з task-50,
спільна зона `app/monitor/**`).
"""
