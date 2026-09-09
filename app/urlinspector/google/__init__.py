"""Прямі запити до Google API про ОДИН URL (task-88).

Межа з `app/integrations/google_api/**` (володіє task-83) проведена свідомо:
там живе транспорт — HTTP, retry, refresh токена, редакція секретів; тут живе
**семантика одного URL**, якої транспорт не знає і знати не має:

- який фільтр ловить обидва написання шляху (`/a` і `/a/`) — task-88 §0.4;
- які рядки GSC насправді синтетичні й мусять стати `None` — §0.3;
- де межа 16-місячної історії GSC і чому її треба показувати як межу, а не
  як «немає даних» — §0.7;
- які метрики GA4 несумісні з якими дименсіями — §0.8.

Змішування цих двох шарів означало б покласти знання про trailing slash у
модуль, який приймає готовий `siteUrl` і нічого про URL не вирішує.
"""

from __future__ import annotations

from app.urlinspector.google.ga4 import Ga4UrlClient
from app.urlinspector.google.gsc import GscUrlClient
from app.urlinspector.google.index import UrlInspectionClient
from app.urlinspector.google.window import (
    WINDOW_KEYS,
    GoogleWindow,
    Window,
    resolve_window,
)

__all__ = [
    "WINDOW_KEYS",
    "Ga4UrlClient",
    "GoogleWindow",
    "GscUrlClient",
    "UrlInspectionClient",
    "Window",
    "resolve_window",
]
