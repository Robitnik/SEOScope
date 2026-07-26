#!/usr/bin/env bash
# Прогін СТАРОЇ test suite AISeoTools (PLAN §8.1, §15 «contract/regression»).
#
# Обов'язковий гейт перед злиттям будь-якої зміни в AISeoTools: наші
# dashboard-обгортки адитивні лише доти, доки вся стара suite лишається
# зеленою. Виконується всередині контейнера, як і решта Python-роботи
# (CLAUDE.md «Середовище виконання»):
#
#   docker compose exec api bash scripts/aiseotools_regression.sh
#   docker compose exec api bash scripts/aiseotools_regression.sh tests/test_ahrefs_parser.py
#
# Швидкий контракт сигнатур — окремо й дешево:
#   docker compose exec api poetry run pytest tests/contract -q
set -euo pipefail

AISEOTOOLS_ROOT="${AISEOTOOLS_ROOT:-/seo/AISeoTools}"

if [ ! -d "$AISEOTOOLS_ROOT/tests" ]; then
  echo "AISeoTools не змонтований у $AISEOTOOLS_ROOT — цей гейт запускається лише локально." >&2
  echo "(CI будує образ без sibling-checkout; там працює тільки tests/contract.)" >&2
  exit 2
fi

# Наш образ ставить AISeoTools лише з рантайм-залежностями (`--with local`),
# а її suite потребує власного dev-екстра (respx, pytest-asyncio, ...).
# Ставимо їх сюди ж, у контейнер: він одноразовий і перебудовується, а
# `pyproject.toml` цього репозиторію (власник — task-01) лишається недоторканим.
#
# НІКОЛИ не `pip install -e "$AISEOTOOLS_ROOT[dev]"`: editable-встановлення пише службові
# артефакти у ВИХІДНЕ дерево, а `$AISEOTOOLS_ROOT` — це bind-mount робочої копії юзера,
# у який ми заходимо від root. Саме так 2026-07-22 і 2026-07-24 у їхньому репозиторії
# з'явився root-овський `.venv` з `home = /usr/local/bin` (на хості не стартує взагалі),
# після чого агент вирішив, що Python-роботи на хості немає, і переніс браузерний прогін
# сюди — де Chrome недосяжний, тож усі експорти Ahrefs приїхали 0 байт.
# Маунт тепер `:ro` (compose.yaml), і цей рядок має лишатись сумісним з цим.
#
# Сам пакет уже імпортовний з образу; тут потрібні РІВНО відсутні dev-залежності.
if ! python -c "import respx" >/dev/null 2>&1; then
  if [ "${SKIP_DEP_INSTALL:-0}" = "1" ]; then
    echo "немає dev-залежностей AISeoTools (respx); зніми SKIP_DEP_INSTALL або постав їх вручну." >&2
    exit 2
  fi
  echo "Ставлю dev-екстра AISeoTools у контейнер (одноразово)..."
  pip install -q respx pytest-asyncio
fi

# `cd` у read-only маунт безпечний (це читання), але pytest не повинен там нічого створювати:
# `-p no:cacheprovider` прибирає `.pytest_cache`, `PYTHONDONTWRITEBYTECODE` — `__pycache__`.
echo "Прогін старої suite у $AISEOTOOLS_ROOT ..."
cd "$AISEOTOOLS_ROOT"
export PYTHONDONTWRITEBYTECODE=1
exec python -m pytest "${@:-tests}" -q -p no:cacheprovider
