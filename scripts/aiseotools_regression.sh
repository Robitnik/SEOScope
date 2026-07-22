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
if ! python -c "import respx" >/dev/null 2>&1; then
  if [ "${SKIP_DEP_INSTALL:-0}" = "1" ]; then
    echo "немає dev-залежностей AISeoTools (respx); зніми SKIP_DEP_INSTALL або постав їх вручну." >&2
    exit 2
  fi
  echo "Ставлю dev-екстра AISeoTools у контейнер (одноразово)..."
  pip install -q -e "${AISEOTOOLS_ROOT}[dev]"
fi

echo "Прогін старої suite у $AISEOTOOLS_ROOT ..."
cd "$AISEOTOOLS_ROOT"
exec python -m pytest "${@:-tests}" -q -p no:cacheprovider
