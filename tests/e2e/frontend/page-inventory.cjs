/* Browser e2e for task-57 (Page Inventory). Runs against the REAL backend
 * (no fixture) and the real ALPHA client (`alphastyle.store`), which
 * already has a built snapshot from live verification during development.
 * Invocation:
 *   NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://localhost:6524 \
 *   CHROMIUM_PATH=/snap/bin/chromium node tests/e2e/frontend/page-inventory.cjs
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "http://localhost:6524";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const clientId = process.env.CLIENT_ID || "d182b771-91cf-4efd-b0dc-b4e1f7fff0d3";
const screenshots = path.join(__dirname, "screenshots");

async function main() {
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1200 }, reducedMotion: "reduce" });
  const page = await context.newPage();
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") browserErrors.push(message.text()); });

  // --- Client page: compact summary card -------------------------------------
  await page.goto(`${baseUrl}/#/client/${clientId}`, { waitUntil: "load" });
  const summary = page.locator("#cp-pages");
  await summary.locator(".ahrefs-kpis, .state-panel").first().waitFor();
  await summary.locator("a", { hasText: "Open Page Inventory" }).waitFor();
  await page.screenshot({ path: path.join(screenshots, "page-inventory-summary-1440.png"), clip: { x: 0, y: 0, width: 1440, height: 900 } });

  // --- Sub-page: Inventory tab -------------------------------------------------
  await summary.locator("a", { hasText: "Open Page Inventory" }).click();
  await page.waitForURL(new RegExp(`#/client/${clientId}/pages`));
  await page.locator(".pi-table").first().waitFor({ timeout: 15000 });

  const headers = await page.locator(".pi-table thead th").allTextContents();
  assert.deepEqual(headers, ["Pages", "URL", "Breadcrumbs", "Title", "Description", "Keywords"],
    "Inventory tab columns match the operator's screenshot order");

  const rowCount = await page.locator(".pi-table tbody tr").count();
  assert.ok(rowCount > 0, "at least one row rendered");

  await page.screenshot({ path: path.join(screenshots, "page-inventory-table-1440.png"), clip: { x: 0, y: 0, width: 1440, height: 900 } });

  // No accidental page-level horizontal scroll from the wide table
  // (task-54's own regression, task-57 reuses the `contain: paint` fix).
  const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  assert.ok(scrollWidth <= 1450, `document does not scroll horizontally past the viewport (scrollWidth=${scrollWidth})`);

  // --- SEO QA tab ---------------------------------------------------------------
  await page.locator('[data-action="tab-qa"]').click();
  await page.locator(".pi-table--qa").waitFor();
  const qaHeaders = await page.locator(".pi-table--qa thead th").allTextContents();
  assert.deepEqual(qaHeaders, ["Pages", "URL", "Breadcrumbs", "Title", "Description", "Keywords"],
    "SEO QA tab has the same 6 columns");

  // Calibrated/Strict toggle is present and switching it re-fetches.
  await page.locator('[data-action="profile-strict"]').click();
  await page.waitForTimeout(400);
  await page.locator('[data-action="profile-strict"][aria-pressed="true"]').waitFor();

  // Expand a row's finding detail.
  const firstToggle = page.locator('[data-action="toggle-row"]').first();
  await firstToggle.click();
  await page.locator(".pi-qa-detail-row").first().waitFor();

  await page.screenshot({ path: path.join(screenshots, "page-inventory-qa-1440.png"), clip: { x: 0, y: 0, width: 1440, height: 900 } });

  // --- Re-crawl / Rebuild buttons present and correctly wired -------------------
  await page.locator('[data-action="tab-inventory"]').click();
  await page.locator('[data-action="rebuild"]').waitFor();
  await page.locator('[data-action="recrawl"]').waitFor();
  const recrawlText = await page.locator('[data-action="recrawl"]').textContent();
  assert.match(recrawlText, /Re-crawl site/, "Re-crawl button reads 'Re-crawl site'");

  // Export link resolves to the xlsx endpoint.
  const exportHref = await page.locator('a[href*="/pages/xlsx"]').getAttribute("href");
  assert.match(exportHref, /\/pages\/xlsx$/, "Export to Excel links to the xlsx endpoint");

  assert.deepEqual(browserErrors, [], `no console/page errors: ${browserErrors.join("; ")}`);

  await browser.close();
  console.log("page-inventory.cjs: all assertions passed");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
