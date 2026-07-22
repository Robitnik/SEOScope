#!/usr/bin/env bash
# Запуск браузерного smoke фронту (task-13 harness для task-12).
#
# Чому це єдиний крок, який виконується НЕ в Docker-образі застосунку:
# у ньому немає Node — образ пінить Python-стек, а тягнути туди npm заради
# одного прогону означало б роздути прод-образ. Правило CLAUDE.md стосується
# Python; сам застосунок тут піднятий у контейнері, а хост лише керує
# браузером (`node` + `chromium`).
#
#   docker compose up -d              # спершу підняти api (він і віддає фронт)
#   bash tests/e2e/run_frontend_smoke.sh
#
# Змінні: BASE_URL, CHROMIUM_PATH, E2E_HOME (кеш playwright-core).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BASE_URL="${BASE_URL:-http://127.0.0.1:${API_PORT:-6524}}"
E2E_HOME="${E2E_HOME:-/tmp/yovth-e2e}"
SHOTS="$REPO_ROOT/tests/e2e/frontend/screenshots"

need() { command -v "$1" >/dev/null 2>&1 || { echo "потрібен $1 у PATH" >&2; exit 2; }; }
need node
need npm

if [ -z "${CHROMIUM_PATH:-}" ]; then
  for candidate in /snap/bin/chromium /usr/bin/chromium /usr/bin/chromium-browser /usr/bin/google-chrome; do
    [ -x "$candidate" ] && CHROMIUM_PATH="$candidate" && break
  done
fi
[ -n "${CHROMIUM_PATH:-}" ] || { echo "не знайшов chromium; задай CHROMIUM_PATH" >&2; exit 2; }

if ! curl -fsS --max-time 5 "$BASE_URL/healthz" >/dev/null 2>&1; then
  echo "застосунок не відповідає на $BASE_URL — підніми стек: docker compose up -d" >&2
  exit 3
fi

if [ ! -d "$E2E_HOME/node_modules/playwright-core" ]; then
  echo "Ставлю playwright-core у $E2E_HOME (в застосунок JS-залежність не додається)..."
  mkdir -p "$E2E_HOME"
  (cd "$E2E_HOME" && npm install --silent --no-save --no-audit --no-fund playwright-core)
fi

# Базові знімки комітяться. Байтове порівняння НЕ є гейтом: растеризація
# шрифтів різниться між машинами, тож зелений/червоний тут дав би шум.
# Гейт — очима в рев'ю по `git diff`, і скрипт нижче сам його показує.
mkdir -p "$SHOTS"
before="$(cd "$REPO_ROOT" && git status --porcelain -- tests/e2e/frontend/screenshots | wc -l)"

echo "smoke -> $BASE_URL (chromium: $CHROMIUM_PATH)"
BASE_URL="$BASE_URL" \
CHROMIUM_PATH="$CHROMIUM_PATH" \
NODE_PATH="$E2E_HOME/node_modules" \
  node "$REPO_ROOT/tests/e2e/frontend/frontend-smoke.cjs"

for shot in dashboard-1440 dashboard-390 history-drawer-1440 sync-progress-1440; do
  file="$SHOTS/$shot.png"
  [ -s "$file" ] || { echo "знімок не створено або порожній: $file" >&2; exit 4; }
done

after="$(cd "$REPO_ROOT" && git status --porcelain -- tests/e2e/frontend/screenshots | wc -l)"
if [ "$after" -gt 0 ]; then
  echo
  echo "Змінені знімки (перевір діффи очима перед комітом):"
  (cd "$REPO_ROOT" && git status --short -- tests/e2e/frontend/screenshots)
  [ "$before" -gt 0 ] && echo "(частина з них була змінена ще до прогону)"
fi
echo "frontend e2e: ok"
