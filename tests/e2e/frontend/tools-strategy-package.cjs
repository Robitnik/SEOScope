/* Browser smoke for the Strategy Package Check tool (task-90) — real
 * backend, real Google Drive/Docs/Sheets (no fixture mode: the source is
 * always a Drive file, there is nothing meaningful to fake locally). Hub
 * card -> paste the SHB strategy link -> resolve -> inspect (sheets +
 * framework candidates) -> run a check against a couple of real frameworks
 * -> report renders with tabs (Overview/Strategy/Frameworks/Cross-checks)
 * -> shareable URL survives a reload -> shows up in the Reports list -> no
 * horizontal scroll at 390px.
 *
 * Invocation:
 *   NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 \
 *   node tests/e2e/frontend/tools-strategy-package.cjs
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:6524";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");
const STRATEGY_URL = "https://docs.google.com/spreadsheets/d/140t5rT6kf2Du9Yu5nLmFFVebAvZ2byg60gbEQsudfkI/edit";

async function main() {
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1200 }, reducedMotion: "reduce" });
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const page = await context.newPage();
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });

  // 1. The hub lists the tool and finds it by tag.
  await page.goto(`${baseUrl}/#/tools`, { waitUntil: "networkidle" });
  await page.locator(".tool-card").first().waitFor();
  await page.locator("#tools-search").fill("cross-check");
  assert.equal(await page.locator(".tool-card").count(), 1, "the hub finds the tool by keyword");
  await page.locator(".tool-card").click();
  await page.locator("#sp-link-input").waitFor();

  // 2. Resolving without a value shows an inline error, not a network call.
  await page.locator("#sp-link-go").click();
  assert.match(await page.locator("#sp-source-error").innerText(), /paste a link/i,
    "empty resolve is caught client-side");

  // 3. Paste the real SHB strategy link -> resolve -> auto-inspect.
  await page.locator("#sp-link-input").fill(STRATEGY_URL);
  await page.locator("#sp-link-go").click();
  await page.locator(".sp-resolved").waitFor({ timeout: 15_000 });
  assert.match(await page.locator(".sp-resolved__body strong").innerText(), /SHB_SEO_STRATEGY/,
    "resolve found the real Drive file by name");
  await page.locator(".sp-framework-picker, .sp-framework-picker__empty").first().waitFor({ timeout: 20_000 });

  // 4. Real framework candidates came back from the real strategy sheet.
  const candidateCount = await page.locator("[data-sp-fw]").count();
  assert.ok(candidateCount > 0, "real framework candidates were found via chipRuns/hyperlinks");

  // 5. Keep the run small and deterministic: select only the first framework.
  await page.locator("[data-sp-fw-none]").click();
  await page.locator("[data-sp-fw]").first().check();
  assert.match(
    await page.locator(".sp-framework-picker__head .cq-sheet-picker__label").innerText(),
    /1 of \d+ selected/i, // CSS text-transform:uppercase changes rendered innerText casing
  );

  await page.locator("#sp-profile-form").waitFor();
  await page.screenshot({ path: path.join(screenshots, "tools-strategy-package-form-1440.png"), fullPage: true });
  await page.locator("#sp-submit").click();
  await page.locator("[data-testid=sp-report]").waitFor({ timeout: 60_000 });

  // 6. The report shell: header, severity summary, five tabs, Comparison
  // active by default (task-91 §2.5 — the literal Strategy/Framework/Site
  // table is the landing view). Grouped by PAGE (task-91 §3.6, corrected
  // live per direct operator feedback): one page = three mini-tables
  // (Strategy/Framework/Site) back to back, not one giant table per source.
  assert.match(await page.locator(".cq-report__file").innerText(), /SHB_SEO_STRATEGY/);
  assert.equal(await page.locator(".cq-summary__item").count(), 3, "error/warn/info severity summary renders");
  assert.equal(await page.locator("#sp-tab-comparison").getAttribute("aria-selected"), "true");
  await page.locator(".sp-matrix-page").first().waitFor({ timeout: 10_000 });
  await page.screenshot({ path: path.join(screenshots, "tools-strategy-package-comparison-1440.png"), fullPage: true });

  // 6a. Real data from the real SHB run: each page group has exactly three
  // mini-tables in this order. The strategy sheet's URL column is
  // literally named "New URL" (a live alias bug found and fixed via
  // direct operator feedback — task-91 tabular.py `_ALIASES`) and must
  // now resolve to a real, matching URL, not a false "no such column".
  const firstPage = page.locator(".sp-matrix-page").first();
  const miniTitles = await firstPage.locator(".sp-matrix-mini__title").allInnerTexts();
  assert.deepEqual(
    miniTitles.map((t) => t.toLowerCase()),
    ["strategy", "framework", "site"], // CSS text-transform:uppercase changes rendered case
  );
  assert.equal(
    await firstPage.locator(".sp-matrix-cell--absent").count(), 0,
    "SHB's 'New URL' column is now recognized — nothing on this page should read absent",
  );
  assert.match(
    await firstPage.locator(".sp-matrix-mini").first().locator("tbody td").first().innerText(),
    /^https:\/\/www\.smashhouseburgers\.com/,
    "Strategy's URL cell shows the real recognized value, not a fallback",
  );
  assert.ok(
    await firstPage.locator(".sp-matrix-verdict-badge.sp-matrix-verdict--match").count() > 0,
    "at least one field matches across sources",
  );

  // 6b. "Mismatches only" actually filters page groups, not just dims them.
  const totalPages = await page.locator(".sp-matrix-page").count();
  await page.locator("#sp-matrix-mismatches-only").check();
  const filteredPages = await page.locator(".sp-matrix-page").count();
  assert.ok(filteredPages < totalPages, "mismatches-only filter reduces the number of page groups");
  await page.locator("#sp-matrix-mismatches-only").uncheck();

  // 6c. Export to Excel points at the real download endpoint for this package.
  const exportHref = await page.locator('a:has-text("Export to Excel")').getAttribute("href");
  assert.match(exportHref, /\/matrix\.xlsx$/);

  // 6d. Overview still works — the matrix is an addition, not a replacement.
  await page.locator("#sp-tab-overview").click();
  await page.locator(".sp-fw-overview").waitFor({ timeout: 10_000 });
  assert.equal(await page.locator("#sp-tab-overview").getAttribute("aria-selected"), "true");
  await page.screenshot({ path: path.join(screenshots, "tools-strategy-package-overview-1440.png"), fullPage: true });

  // 7. Strategy tab reuses the shared findings panel (filter/sort/group).
  await page.locator("#sp-tab-strategy").click();
  await page.locator("#sp-tabpanel .cq-summary, #sp-tabpanel .cq-clean").first().waitFor();
  assert.equal(await page.locator("#sp-tab-strategy").getAttribute("aria-selected"), "true");

  // 8. Frameworks tab: a chip per checked framework, detail below with its
  // own cross-check badges (Frame<->Strategy / Frame<->Site / Copy<->Site).
  await page.locator("#sp-tab-frameworks").click();
  await page.locator(".sp-fw-chip").first().waitFor({ timeout: 10_000 });
  const crossBadgeCount = await page.locator(".sp-fw-detail__cross .sp-cross-badge, .state-panel").count();
  assert.ok(crossBadgeCount > 0, "framework detail shows either cross-check badges or a not-checked reason");
  await page.screenshot({ path: path.join(screenshots, "tools-strategy-package-frameworks-1440.png"), fullPage: true });

  // 9. Cross-checks tab: the per-framework coverage table + aggregate findings panel.
  await page.locator("#sp-tab-cross").click();
  await page.locator(".sp-cross-table, .cq-clean, #sp-tabpanel .cq-summary").first().waitFor({ timeout: 10_000 });

  // 10. The run is persisted: the URL carries a real report id, and reloading
  // that exact URL shows the same package (shareable-link contract).
  await page.waitForFunction(() => /\/tools\/strategy-package\/[^/?#]+/.test(location.hash));
  const reportUrl = page.url();
  assert.match(reportUrl, /#\/tools\/strategy-package\/[^/?#]+$/, "the URL carries a real package id");
  await page.reload({ waitUntil: "networkidle" });
  await page.locator("[data-testid=sp-report]").waitFor({ timeout: 15_000 });
  assert.match(await page.locator(".cq-report__file").innerText(), /SHB_SEO_STRATEGY/,
    "reloading the report URL shows the same package");

  await page.locator("#sp-copy-link").click();
  const copiedLink = await page.evaluate(() => navigator.clipboard.readText());
  assert.equal(copiedLink, reportUrl, "Copy link copies the exact current report URL");

  // 11. The report shows up in the tool's own "Reports" list.
  await page.goto(`${baseUrl}/#/tools/strategy-package`, { waitUntil: "networkidle" });
  await page.locator("#sp-reports-body .tool-table").waitFor({ timeout: 10_000 });
  assert.match(
    await page.locator("#sp-reports-body .tool-table tbody tr").first().innerText(),
    /SHB_SEO_STRATEGY/,
    "the reports list shows the source name",
  );

  // 12. Mobile: the tool's own content does not force horizontal scroll.
  // Scoped to #tools-page-root, not `document.documentElement` — the
  // masthead's `.header-start` already overflows 390px app-wide (measured
  // live on the bare dashboard root, zero tool interaction: task-90 did not
  // introduce this, it is out of this tool's owned paths to fix).
  const mobile = await browser.newContext({ viewport: { width: 390, height: 900 }, reducedMotion: "reduce" });
  const mobilePage = await mobile.newPage();
  await mobilePage.goto(`${baseUrl}/#/tools/strategy-package`, { waitUntil: "networkidle" });
  await mobilePage.locator("#sp-link-input").waitFor();
  const { scrollWidth, clientWidth } = await mobilePage.evaluate(() => {
    const el = document.querySelector("#tools-page-root");
    return { scrollWidth: el.scrollWidth, clientWidth: el.clientWidth };
  });
  assert.ok(scrollWidth <= clientWidth, `no horizontal scroll inside the tool at 390px (${scrollWidth} > ${clientWidth})`);
  await mobilePage.screenshot({ path: path.join(screenshots, "tools-strategy-package-390.png"), fullPage: true });
  await mobile.close();

  assert.deepEqual(browserErrors, [], "no console or page errors");
  await context.close();
  await browser.close();
  console.log("tools-strategy-package.cjs: all assertions passed");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
