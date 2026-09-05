"""Schema Markup Checker (task-56): перевірка й генерація schema.org
структурованих даних для сторінок клієнтів.

Підмодулі:

- `vocab.py` — завантажує вендорений дистилят вокабуляру schema.org
  (`data/vocab.json.gz`, зроблений `scripts/build_schema_vocab.py`) і
  рукописний Google rich-results профіль (`data/google_profiles.json`).
- `extract.py` — читає JSON-LD і Microdata з HTML (stdlib `html.parser`/
  `json`, без `extruct`/`lxml` — та сама причина, що в `alt_text/verify.py`
  й `redirects/trace.py`: `lxml` у образі лише транзитивний).
- `classify.py` — визначає «тип сторінки» (home/article/product/…) із
  сигналів URL і HTML для вибору релевантних Google-фіч.
- `validate.py` — три рівні перевірки: L0 синтаксис, L1 вокабуляр (тип і
  властивість існують, з обходом subclass-ланцюга), L2 Google-профіль
  (required/recommended, попередження про retired/no-rich-result типи).
- `generate.py` — будує JSON-LD-сніпет із доведених фактів сторінки; кожне
  поле має `source`, недоведене йде в `needs_input` (PLAN §12: здогад ≠ факт).
- `service.py` — оркеструє fetch → extract → classify → validate → generate.
"""

from __future__ import annotations
