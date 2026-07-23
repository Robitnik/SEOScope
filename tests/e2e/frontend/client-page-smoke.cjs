/* Browser smoke for task-18 (client analytics page). Companion to
 * frontend-smoke.cjs (task-12/19, dashboard). Same invocation convention:
 *   NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:4173 \
 *   node tests/e2e/frontend/client-page-smoke.cjs
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:4173";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");

async function main() {
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  await context.addInitScript(() => localStorage.clear());
  const page = await context.newPage();
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") browserErrors.push(message.text()); });

  // Deep link straight into the client route — the page must restore full
  // state from the URL alone (AGENTS.md §12), not from prior in-app navigation.
  await page.goto(`${baseUrl}/?fixture=normal#/client/client-cleveland`, { waitUntil: "networkidle" });
  await page.locator("#cp-header h1").waitFor();
  assert.equal(await page.locator("#cp-header h1").innerText(), "19 CLEVELAND", "client identity loads from the dashboard fixture");
  assert.equal(await page.locator("#dashboard-command-bar").isHidden(), true, "dashboard-only masthead chrome stays hidden on the client route");
  assert.match(await page.locator("#cp-period-dates").innerText(), /Current:.*Previous:/s, "exact current and previous dates are visible without hover");

  // null vs 0 (PLAN §12): a partial-quality row shows "—" for CTR/position,
  // a genuine zero-click row still shows "0".
  const roofInspectionRow = page.locator("tr", { hasText: "/services/roof-inspection" });
  assert.match(await roofInspectionRow.innerText(), /—/, "partial-quality CTR/position render as em dash, not 0 or blank");
  const gutterTipsRow = page.locator("tr", { hasText: "/blog/gutter-maintenance-tips" });
  assert.match(await gutterTipsRow.innerText(), /\b0\b/, "a real zero-click row still renders 0, not em dash");
  assert.equal(await page.locator(".tag-new").count() > 0, true, "a page with no previous-period row is flagged, not shown as a fabricated delta");
  assert.equal(await page.locator(".truncated-note").count(), 1, "truncated top-N is disclosed, not silently presented as the full list");

  // Tabs: keyboard-operable, switches dataset and persists to the URL.
  await page.locator("#cp-tab-page").focus();
  await page.keyboard.press("ArrowRight");
  assert.equal(await page.locator("#cp-tab-query").getAttribute("aria-selected"), "true", "arrow-key switches the active tab");
  assert.match(page.url(), /tab=query/, "active tab persists in the URL");
  await page.locator("#cp-search").fill("gutter");
  await page.waitForTimeout(400);
  assert.equal(await page.locator("#cp-tabpanel .analytics-table tbody tr").count(), 1, "search filters rows client-side");
  assert.match(page.url(), /q=gutter/, "search text persists in the URL");

  // Alerts block: absolute impact, exact dates, both periods always shown.
  assert.match(await page.locator(".alerts-section__dates").innerText(), /\d{4}/, "alerts show exact calendar dates for both periods");
  assert.equal(await page.locator(".alert-group", { hasText: "Declining" }).locator(".alert-row").count() > 0, true, "declining group lists entries");

  // Extended periods use exact live ranges. Stored exact-period history can
  // render a chart while an empty range starts an on-demand collection.
  await page.locator('[data-period-kind="quarter"]').click();
  await page.waitForTimeout(400);
  assert.equal(await page.locator("#cp-coverage-note").isVisible(), true, "coverage note explains exact live collection for extended periods");
  assert.match(await page.locator("#cp-coverage-note").innerText(), /exact live periods/i, "extended-period note describes the live behavior");
  assert.equal(await page.locator("#cp-canvas").count(), 1, "stored exact-quarter history renders in the chart");

  // Custom range + reload survives (AGENTS.md §12: URL state over reload/back/forward/share).
  await page.locator('[data-period-kind="custom"]').click();
  await page.locator("#cp-custom-start").fill("2026-06-01");
  await page.locator("#cp-custom-end").fill("2026-06-30");
  await page.locator("#cp-custom-apply").click();
  await page.waitForTimeout(400);
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForTimeout(400);
  assert.equal(await page.locator('[data-period-kind="custom"]').getAttribute("aria-pressed"), "true", "custom period survives reload");
  assert.equal(await page.locator("#cp-custom-start").inputValue(), "2026-06-01", "custom range dates survive reload");

  for (const [width, height] of [[390, 900], [768, 900], [1024, 900], [1440, 1000]]) {
    await page.setViewportSize({ width, height });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), true, `no page overflow at ${width}px`);
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: path.join(screenshots, "client-page-1440.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 900 });
  await page.screenshot({ path: path.join(screenshots, "client-page-390.png"), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });

  // Empty data automatically starts the scoped live job, streams progress,
  // then refreshes the page while preserving the honest empty result if the
  // fixture collector has no rows to return.
  await page.goto(`${baseUrl}/?fixture=empty-analytics#/client/client-cleveland`, { waitUntil: "networkidle" });
  await page.getByText("No data collected for this period yet.").waitFor();
  await page.getByText("Not enough data for alerts.").waitFor();
  await page.locator("#cp-live-status").waitFor({ state: "visible" });
  await page.getByText("Live data updated", { exact: true }).waitFor({ timeout: 10000 });
  assert.equal(await page.locator("#cp-live-refresh").isEnabled(), true, "manual live refresh is restored after the automatic job completes");

  // GSC not connected at all vs GSC needs reauth are distinct, explained states.
  await page.goto(`${baseUrl}/?fixture=normal#/client/client-nyc-hearing`, { waitUntil: "networkidle" });
  await page.getByText("GSC re-authorization required.").waitFor();

  // Back link returns to the dashboard route and it renders normally.
  await page.goto(`${baseUrl}/?fixture=all-states#/client/client-cleveland`, { waitUntil: "networkidle" });
  await page.locator(".back-link").click();
  await page.locator(".client-card").first().waitFor();
  assert.equal(await page.locator("#dashboard-view").isVisible(), true, "back link returns to a fully rendered dashboard");

  console.log("browser errors:", browserErrors.length ? browserErrors.join("\n") : "(none)");
  assert.equal(browserErrors.length, 0, "no uncaught errors or console.error calls");

  await browser.close();
}

main().then(() => console.log("client page e2e: ok")).catch((error) => { console.error(error); process.exit(1); });
