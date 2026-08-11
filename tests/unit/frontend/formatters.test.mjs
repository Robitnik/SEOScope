// task-36: перший модульний JS-тест у репо. `frontend/js/*.js` завантажується
// нативним ESM-детектором Node (v20.19+, без `package.json`/"type":"module" —
// перевірено вручну проти цієї версії Node перед тим, як писати цей файл).
// Запуск: `node --test tests/unit/frontend/` (з кореня репо, на хості — це не
// Python, правило CLAUDE.md «лише Docker» на нього не поширюється; той самий
// виняток, яким уже користується `tests/e2e/frontend/run_frontend_smoke.sh`).
//
// Файли з побічними ефектами модульного рівня (`router.js`, `dashboard.js`,
// будь-який `*-page.js`) звертаються до `document`/`location`/`window` одразу
// при імпорті й НЕ підлягають цьому підходу без DOM-шима (нової залежності) —
// перевірено: `import("./router.js")` у голому Node падає з
// `ReferenceError: window is not defined`. Тому тут — лише чисті функції
// (`formatters.js`, `monitor-diff.js`), без DOM. Маршрути/URL-фільтри
// покриває браузерний e2e-смоук (`tests/e2e/frontend/frontend-smoke.cjs`),
// як і для кожної попередньої таски з подібним URL-станом (trends/blogs/redirects).

import { test } from "node:test";
import assert from "node:assert/strict";
import { changeTypeLabel, MONITOR_SEVERITY_META, escapeHtml } from "../../../frontend/js/formatters.js";

test("MONITOR_SEVERITY_META has all three severities, each with label+icon+tone", () => {
  for (const key of ["critical", "warning", "info"]) {
    const entry = MONITOR_SEVERITY_META[key];
    assert.ok(entry, `missing entry for ${key}`);
    assert.equal(typeof entry.label, "string");
    assert.equal(typeof entry.icon, "string");
    assert.equal(typeof entry.tone, "string");
  }
  // Never color-only (AGENTS §7): a caller pairing tone with icon+label must
  // find real, distinct icons per severity, not the same icon three times.
  const icons = new Set(Object.values(MONITOR_SEVERITY_META).map((entry) => entry.icon));
  assert.equal(icons.size, 3);
});

test("changeTypeLabel humanizes known change types from the severity catalog", () => {
  assert.equal(changeTypeLabel("page_noindex_appeared"), "Noindex appeared");
  assert.equal(changeTypeLabel("sitemap_broken_urls"), "Sitemap has broken URLs");
  assert.equal(changeTypeLabel("dns_ns_changed"), "DNS nameservers changed");
});

test("changeTypeLabel labels self-healing `_resolved` events relative to the original", () => {
  assert.equal(changeTypeLabel("page_gone_resolved"), "Resolved: Page is gone");
  assert.equal(changeTypeLabel("site_down_resolved"), "Resolved: Site down");
});

test("changeTypeLabel falls back to sentence-cased snake_case for unknown types", () => {
  // The catalog is open by contract (app/monitor/severity.py can add new types
  // without a frontend release) — an unrecognized type must never render as a
  // raw identifier.
  assert.equal(changeTypeLabel("some_future_change_type"), "Some future change type");
  assert.equal(changeTypeLabel("some_future_change_type_resolved"), "Resolved: Some future change type");
});

test("changeTypeLabel returns empty string for falsy input", () => {
  assert.equal(changeTypeLabel(""), "");
  assert.equal(changeTypeLabel(null), "");
  assert.equal(changeTypeLabel(undefined), "");
});

test("escapeHtml still escapes the five reserved characters (sanity check on a shared dependency)", () => {
  assert.equal(escapeHtml(`<a href="x">it's & "quoted"</a>`), "&lt;a href=&quot;x&quot;&gt;it&#39;s &amp; &quot;quoted&quot;&lt;/a&gt;");
});
