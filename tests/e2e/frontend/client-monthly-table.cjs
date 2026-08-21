/* Browser e2e for task-54 (Monthly Performance table). Companion to
 * client-page-smoke.cjs, covering both halves of the split: the compact
 * summary card at the TOP of the client page, and the full sheet-fidelity
 * table + Export to Excel on its own sub-page (`#/client/<id>/monthly`).
 * Same invocation convention:
 *   NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 \
 *   CHROMIUM_PATH=/snap/bin/chromium node tests/e2e/frontend/client-monthly-table.cjs
 *
 * `?fixture=normal` activates `frontend/fixtures/client-monthly-table.json`
 * for every `getClientMonthlyTable`/`refreshClientMonthlyTable`/
 * `getClientMonthlyKeyEvents`/`putClientMonthlyTableSettings` call regardless
 * of which client route is open (api.js's fixture branch ignores clientId) —
 * live-verified separately against the real backend/SDS client (task-54 §6
 * "Живий гейт").
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:4173";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");

async function main() {
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1200 }, reducedMotion: "reduce" });
  await context.addInitScript(() => localStorage.clear());
  const page = await context.newPage();
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") browserErrors.push(message.text()); });

  // --- Client page: compact summary card, right after the header ------------
  await page.goto(`${baseUrl}/?fixture=normal#/client/client-cleveland`, { waitUntil: "networkidle" });
  const summary = page.locator("#cp-monthly");
  await summary.locator(".ahrefs-kpis").waitFor();

  const order = await page.evaluate(() => {
    const header = document.querySelector("#cp-header");
    const monthly = document.querySelector("#cp-monthly");
    const periodPanel = document.querySelector(".period-panel");
    return {
      afterHeader: Boolean(header.compareDocumentPosition(monthly) & Node.DOCUMENT_POSITION_FOLLOWING),
      beforePeriodPanel: Boolean(monthly.compareDocumentPosition(periodPanel) & Node.DOCUMENT_POSITION_FOLLOWING),
    };
  });
  assert.equal(order.afterHeader, true, "Monthly Performance card sits right after the client header");
  assert.equal(order.beforePeriodPanel, true, "Monthly Performance card sits above the period panel");

  // At most 3-4 KPIs (AGENTS §4), the full table is NOT inlined on this page.
  assert.equal(await summary.locator(".ahrefs-kpi").count(), 4, "the summary card shows exactly 4 KPI tiles");
  assert.equal(await summary.locator(".mt-table").count(), 0, "the full sheet-fidelity table does not render on the client page");
  const openLink = summary.locator("a", { hasText: "Open Monthly Performance" });
  await openLink.waitFor();

  await page.screenshot({ path: path.join(screenshots, "client-monthly-summary-1440.png"), clip: { x: 0, y: 0, width: 1440, height: 700 } });

  // --- Sub-page: the full table + its own controls ---------------------------
  await openLink.click();
  await page.waitForURL(/#\/client\/client-cleveland\/monthly/);
  const section = page.locator("#mp-monthly");
  await section.locator(".mt-table").waitFor();

  const headerText = await section.locator("thead tr").innerText();
  const columnOrder = ["Total organic Users", "Impressions", "Clicks", "Organic Conversions", "Conversion rate", "Organic \"Book Now\"", "CTR"];
  let lastIndex = -1;
  for (const label of columnOrder) {
    const index = headerText.indexOf(label);
    assert.ok(index > lastIndex, `column "${label}" appears in order`);
    lastIndex = index;
  }

  const firstRow = section.locator("tbody tr").first();
  assert.match(await firstRow.innerText(), /—/, "a month before the GSC window shows em dash, not 0");
  assert.equal(await section.locator(".mt-badge--partial").count() > 0, true, "a partial month (edge of the GSC window) is flagged");
  assert.equal(await section.locator(".mt-badge--warn").count() > 0, true, "a possibly-truncated month is flagged");
  const failedRow = page.locator("tbody tr", { hasText: "May 2026" });
  assert.match(await failedRow.innerText(), /—/, "a failed collection attempt shows em dash for every affected cell");

  assert.equal(await section.locator(".mt-direction").count() > 0, true, "direction markers render by default");
  await section.locator("#mt-plain-colors").check();
  assert.equal(await section.locator(".mt-table").evaluate((el) => getComputedStyle(el.querySelector(".mt-direction")).display), "none", "Sheet colors only hides direction markers");
  await section.locator("#mt-plain-colors").uncheck();

  // Export to Excel: the link exists and points at the right endpoint. The
  // xlsx bytes/headers themselves are verified live against the real
  // backend + a real client in task-54 §6 ("Живий гейт") — `client-cleveland`
  // here is a FIXTURE-mode identity with no row in the real DB, so a request
  // against it 404s regardless of the frontend being correct.
  const exportLink = section.locator('a:has-text("Export to Excel")');
  await exportLink.waitFor();
  const exportHref = await exportLink.getAttribute("href");
  assert.match(exportHref, /\/api\/v1\/clients\/client-cleveland\/monthly-table\/xlsx$/, "Export to Excel points at the xlsx endpoint for this client");
  assert.equal(await exportLink.getAttribute("download"), "", "Export to Excel is a real download, not a SPA navigation");

  // Configure: attribution selects, event name input, live "Suggest events".
  await section.locator(".mt-settings summary").click();
  assert.equal(await section.locator(".mt-settings").getAttribute("open") !== null, true, "Configure opens on click");
  await section.locator("#mt-discover-events").click();
  await page.waitForSelector("#mt-event-candidates option", { state: "attached", timeout: 5000 });
  assert.equal(await section.locator("#mt-event-candidates option").count() > 0, true, "Suggest events populates candidates from the fixture");

  await section.locator('input[name="key_event_label"]').fill("Book Now");
  await section.locator("#mt-settings-save").click();
  await page.getByText("Settings saved.").waitFor({ timeout: 5000 });
  assert.equal(await section.locator(".mt-settings").getAttribute("open") !== null, true, "Configure stays open after Save");

  await section.locator("#mt-refresh").click();
  assert.equal(await section.locator("#mt-refresh").isDisabled(), true, "Refresh disables itself while busy");
  assert.match(await section.locator("#mt-refresh").innerText(), /Refreshing/, "Refresh shows a busy label");
  await page.waitForSelector("#mt-refresh:not([disabled])", { timeout: 10000 });
  assert.match(await section.locator("#mt-refresh").innerText(), /^Refresh$/, "Refresh returns to its resting label");

  // Back link returns to the client page.
  await page.locator(".back-link").click();
  await page.waitForURL(/#\/client\/client-cleveland$/);
  await page.locator("#cp-monthly .ahrefs-kpis").waitFor();

  // No accidental horizontal page scroll at any breakpoint (AGENTS §11) — a
  // live-measured real bug (task-54 §8): a wide <table> in an overflow:auto
  // wrapper can still scroll the whole <html> in Chromium without this guard.
  await page.goto(`${baseUrl}/?fixture=normal#/client/client-cleveland/monthly`, { waitUntil: "networkidle" });
  await page.locator("#mp-monthly .mt-table").waitFor();
  for (const [width, height] of [[390, 900], [768, 900], [1024, 900], [1440, 1200]]) {
    await page.setViewportSize({ width, height });
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth),
      true,
      `no page overflow at ${width}px`,
    );
    await page.locator("#mp-monthly").scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(screenshots, `client-monthly-table-${width}.png`), fullPage: false });
  }
  // A true 200% browser-zoom check needs Chromium's own zoom (CSS `zoom`/
  // `element.style.*` mutation is blocked by this app's CSP, same as any
  // other inline style) — verified manually via CDP `Emulation.setDeviceMetricsOverride`
  // per AGENTS §15 (task-54 §8: 0px overflow confirmed at scale=2).
  await page.setViewportSize({ width: 1440, height: 1200 });

  console.log("browser errors:", browserErrors.length ? browserErrors.join("\n") : "(none)");
  assert.equal(browserErrors.length, 0, "no uncaught errors or console.error calls");

  await browser.close();
}

main().then(() => console.log("client monthly table e2e: ok")).catch((error) => { console.error(error); process.exit(1); });
