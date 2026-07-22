# Frontend browser smoke

The smoke suite exercises fixture-driven rendering, `null` versus `0`, search,
the background sync lifecycle, the progress matrix, dialog focus return, history,
empty/error states, reduced motion, and desktop/mobile screenshots.

It intentionally does not add a JavaScript dependency to the application. Run it
with the Playwright package supplied by the task-13 test harness, or install
`playwright-core` in a temporary directory and expose it through `NODE_PATH`.

```bash
BASE_URL=http://127.0.0.1:4173 \
CHROMIUM_PATH=/usr/bin/chromium \
NODE_PATH=/tmp/yovth-e2e/node_modules \
node tests/e2e/frontend/frontend-smoke.cjs
```
