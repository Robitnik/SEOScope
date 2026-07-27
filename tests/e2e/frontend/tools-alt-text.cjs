/* Browser smoke for task-27 (Tools hub + Image Alt Text tool).
 * Same invocation convention as the other suites in this folder:
 *   NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:4173 \
 *   node tests/e2e/frontend/tools-alt-text.cjs
 *
 * Drives the fixture-mode tool end to end: hub search → report table →
 * copy-alt marks progress → verify writes a verdict → filters persist in the URL.
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:4173";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");
const REPORT = "#/tools/alt-text/fixture-report-march";

async function main() {
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  // Clipboard writes need permission in headless Chromium; without it the copy
  // button reports failure and the status must NOT change (which we also assert).
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const page = await context.newPage();
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => {
    const text = message.text();
    // Fixture image hosts are deliberately unreachable — that IS the broken
    // preview state under test, not an application error.
    const isFixtureImage = /example|i0\.wp\.com|ERR_NAME_NOT_RESOLVED|status of 400/.test(text);
    if (message.type() === "error" && !isFixtureImage) browserErrors.push(text);
  });

  // 1. Hub: catalog renders, search filters, empty search state offers a way out.
  await page.goto(`${baseUrl}/?fixture=normal#/tools`, { waitUntil: "networkidle" });
  await page.locator(".tool-card").first().waitFor();
  // Task-29 added a second tool to the catalog; the hub is a growing shelf.
  assert.equal(await page.locator(".tool-card").count(), 2, "the catalog renders every registered tool");
  assert.equal(await page.locator("[data-nav='tools']").getAttribute("aria-current"), "page",
    "global nav marks the active section");
  assert.equal(await page.locator("#dashboard-command-bar").isHidden(), true,
    "dashboard-only masthead chrome stays hidden on the tools route");

  await page.locator("#tools-search").fill("wordpress");
  assert.equal(await page.locator(".tool-card").count(), 1, "search matches on tags, not just the name");
  await page.locator("#tools-search").fill("nothing-matches-this");
  assert.equal(await page.locator(".tool-card").count(), 0, "a non-matching search empties the grid");
  await page.locator("#tools-clear").click();
  assert.equal(await page.locator(".tool-card").count(), 2, "the empty state clears the search");
  await page.screenshot({ path: path.join(screenshots, "tools-hub-1440.png"), fullPage: true });

  // 1b. Audit files are named client-first by agency convention, so the client
  // must be picked automatically from the file name (regression: this silently
  // stopped working behind a stale cached module).
  await page.goto(`${baseUrl}/?fixture=normal#/tools/alt-text`, { waitUntil: "networkidle" });
  await page.locator("#at-file").waitFor();
  await page.locator("#at-file").setInputFiles({
    name: "19Cleveland_Ahrefs-Audit_21072026.xlsx",
    mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    buffer: Buffer.from("PKfixture"),
  });
  await page.locator("#at-map-form").waitFor();
  await page.waitForTimeout(600);
  assert.equal(await page.locator("#at-client option:checked").innerText(), "19 Cleveland",
    "the client is picked from the client-first file name");
  assert.match(await page.locator("#at-client-hint").innerText(), /file name/,
    "the auto-pick is disclosed, not silent");
  // A file with no client prefix must not guess one.
  await page.locator("#at-map-cancel").click();
  await page.locator("#at-file").setInputFiles({
    name: "generic-image-audit.xlsx",
    mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    buffer: Buffer.from("PKfixture"),
  });
  await page.locator("#at-map-form").waitFor();
  await page.waitForTimeout(600);
  assert.equal(await page.locator("#at-client").inputValue(), "",
    "no client is invented when the file name does not name one");

  // 2. Deep link straight into a report — state comes from the URL alone.
  await page.goto(`${baseUrl}/?fixture=normal${REPORT}`, { waitUntil: "networkidle" });
  await page.locator(".at-table tbody tr").first().waitFor();
  assert.equal(await page.locator(".at-table tbody tr").count(), 6, "every row of the report renders");

  // 3. The derived search key is what the operator copies — not the raw URL.
  const firstRow = page.locator(".at-table tbody tr").first();
  assert.equal(await firstRow.locator(".at-key code").first().innerText(), "rooftop-terrace-sunset",
    "the resize suffix is stripped from the media-library search key");
  assert.match(await firstRow.locator(".at-stripped").innerText(), /-1024x683/,
    "what was stripped stays visible to the operator");

  // A theme file is not searchable in the media library — a distinct, honest state.
  const themeRow = page.locator("tr", { hasText: "/assets/brand/logo.svg" });
  assert.match(await themeRow.innerText(), /THEME FILE/i, "a theme asset is labelled, not guessed at");

  // A row with no page has no verify affordance, and says why.
  const noPageRow = page.locator("tr", { hasText: "press-award" });
  assert.match(await noPageRow.innerText(), /No page in the sheet/, "a row with no page URL says so");
  assert.equal(await noPageRow.locator("button[data-action='verify']").count(), 0,
    "no verify button is offered where there is nothing to check");

  // 4. Copying the alt marks progress and the mark survives a reload.
  const pendingRow = page.locator("tr", { hasText: "bar-counter-2" });
  assert.equal(await pendingRow.locator("select[data-action='status']").inputValue(), "pending");
  await pendingRow.locator("button[data-action='copy-alt']").click();
  await page.waitForTimeout(400);
  assert.equal(await pendingRow.locator("select[data-action='status']").inputValue(), "copied",
    "copying the new alt marks the row as handled");
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()),
    "Marble bar counter with brass stools", "the clipboard holds the new alt text");

  await page.reload({ waitUntil: "networkidle" });
  await page.locator(".at-table tbody tr").first().waitFor();
  assert.equal(await page.locator("tr", { hasText: "bar-counter-2" }).locator("select[data-action='status']").inputValue(),
    "copied", "the progress mark is stored, not just painted");

  // Copying the search key is an intermediate step and must NOT move progress.
  const keyRow = page.locator("tr", { hasText: "kitchen-team" });
  await keyRow.locator("button[data-action='copy']").first().click();
  await page.waitForTimeout(300);
  assert.equal(await keyRow.locator("select[data-action='status']").inputValue(), "pending",
    "copying the search key does not mark the image as handled");

  // 4b. One click must produce exactly one reaction. Regression: the delegated
  // handlers were re-attached on every table re-render, so after N filter
  // changes a single copy fired N toasts and N requests.
  await page.locator("#at-filter-status").selectOption("copied");
  await page.waitForTimeout(400);
  await page.locator("#at-filter-status").selectOption("");
  await page.waitForTimeout(400);
  await page.locator("#at-search").fill("kitchen");
  await page.waitForTimeout(500);
  await page.locator("#toast-region").evaluate((region) => { region.innerHTML = ""; });
  await page.locator("tr", { hasText: "kitchen-team" }).locator("button[data-action='copy']").first().click();
  await page.waitForTimeout(500);
  assert.equal(await page.locator("#toast-region .toast-message").count(), 1,
    "one click produces exactly one toast, however many times the table was re-rendered");
  await page.locator("#at-search").fill("");
  await page.waitForTimeout(500);

  // 4c. Preview opens in a modal, closes on Escape and returns focus.
  const previewRow = page.locator("tr", { hasText: "rooftop-terrace-sunset" });
  await previewRow.locator("button[data-action='preview']").click();
  await page.waitForTimeout(400);
  assert.equal(await page.locator("#at-preview-dialog").evaluate((d) => d.open), true,
    "clicking the thumbnail opens the preview dialog");
  assert.match(await page.locator("#at-preview-title").innerText(), /rooftop-terrace-sunset/,
    "the dialog names the image file");
  assert.match(await page.locator(".at-preview-dialog__alt").innerText(), /Rooftop terrace/,
    "the dialog shows the new alt next to the image");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  assert.equal(await page.locator("#at-preview-dialog").evaluate((d) => d.open), false,
    "Escape closes the preview");
  assert.equal(await page.evaluate(() => document.activeElement?.dataset?.action), "preview",
    "focus returns to the thumbnail that opened the dialog");

  // 5. Verify writes a verdict; a confirmed match promotes the row.
  await pendingRow.locator("button[data-action='verify']").click();
  await page.waitForTimeout(900);
  assert.match(await pendingRow.innerText(), /Alt is live/, "a confirmed match is reported as such");
  assert.equal(await pendingRow.locator("select[data-action='status']").inputValue(), "applied",
    "only a confirmed match promotes the row to applied");

  // A mismatch shows what is actually on the page instead of a bare failure.
  assert.match(await page.locator("tr", { hasText: "private-dining-room" }).innerText(), /On page: “IMG_0042”/,
    "a mismatch names the alt that is really on the page");
  assert.equal(await page.locator(".at-cache-note").count(), 1,
    "the cache caveat is stated, not implied");

  // 6. Filters narrow the table and persist in the URL.
  await page.locator("#at-filter-status").selectOption("applied");
  await page.waitForTimeout(400);
  assert.equal(await page.locator(".at-table tbody tr").count(), 2, "the status filter narrows the table");
  assert.match(page.url(), /st=applied/, "the active filter persists in the URL");
  await page.locator("#at-search").fill("rooftop");
  await page.waitForTimeout(500);
  assert.match(page.url(), /q=rooftop/, "the search text persists in the URL");
  assert.equal(await page.locator(".at-table tbody tr").count(), 1, "search and status filters combine");

  await page.goto(`${baseUrl}/?fixture=normal${REPORT}`, { waitUntil: "networkidle" });
  await page.locator(".at-table tbody tr").first().waitFor();
  await page.screenshot({ path: path.join(screenshots, "tools-alt-text-1440.png"), fullPage: true });

  // 7. Mobile: no accidental horizontal page scroll (the table scrolls itself).
  const mobile = await browser.newContext({ viewport: { width: 390, height: 900 }, reducedMotion: "reduce" });
  const mobilePage = await mobile.newPage();
  await mobilePage.goto(`${baseUrl}/?fixture=normal${REPORT}`, { waitUntil: "networkidle" });
  await mobilePage.locator(".at-table tbody tr").first().waitFor();
  const scrollWidth = await mobilePage.evaluate(() => document.documentElement.scrollWidth);
  const clientWidth = await mobilePage.evaluate(() => document.documentElement.clientWidth);
  assert.ok(scrollWidth <= clientWidth, `no horizontal page scroll at 390px (${scrollWidth} > ${clientWidth})`);
  await mobilePage.screenshot({ path: path.join(screenshots, "tools-alt-text-390.png"), fullPage: true });
  await mobile.close();

  assert.deepEqual(browserErrors, [], "no console or page errors");
  await context.close();
  await browser.close();
  console.log("tools-alt-text.cjs: all assertions passed");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
