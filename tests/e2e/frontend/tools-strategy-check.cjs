/* Browser smoke for the Strategy Check tool — real backend, no fixture mode
 * (the engine is deterministic and fast enough that a live run is a better
 * check than canned data): hub card -> upload a real .xlsx -> live findings
 * render, grouped by severity -> severity filter hides a group -> copy
 * button copies a ref -> the run gets a shareable `#/tools/strategy-check/<id>`
 * URL that survives a reload -> the report shows up in the "Reports" list ->
 * no horizontal page scroll at 390px.
 *
 * Invocation:
 *   NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 \
 *   node tests/e2e/frontend/tools-strategy-check.cjs
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:4173";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");
const sampleFile = path.join(__dirname, "..", "..", "..", ".verify_tmp", "sample-strategy.xlsx");

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
  await page.locator("#tools-search").fill("cannibalization");
  assert.equal(await page.locator(".tool-card").count(), 1, "the hub finds the tool by keyword");
  await page.locator(".tool-card").click();
  await page.locator("#cq-file").waitFor();

  // 2. Submitting without a file shows an inline error, not a network call.
  await page.locator("#cq-form button[type=submit]").click();
  assert.match(await page.locator("#cq-form-error").innerText(), /choose a strategy workbook/i,
    "empty submit is caught client-side");

  // 3. Upload a real workbook, set a brand name, run the check against the live backend.
  await page.locator("#cq-file").setInputFiles(sampleFile);
  assert.equal(await page.locator("#cq-file-name").innerText(), "sample-strategy.xlsx");
  await page.locator("#cq-brand").fill("Acme");
  await page.locator("#cq-form button[type=submit]").click();
  await page.locator("[data-testid=cq-report]").waitFor({ timeout: 10_000 });

  // 4. Real findings from the real engine: the brand-padded overlong title.
  const checks = await page.locator(".cq-finding__check").allInnerTexts();
  assert.ok(checks.includes("meta-title-length"), "real backend returned a real meta-title-length finding");
  assert.ok(checks.includes("meta-title-brand-padding"), "brand query param reached the engine");
  const errorCount = await page.locator(".cq-sev--error strong").first().innerText();
  assert.ok(Number(errorCount) > 0, "at least one error-severity finding is summarized");
  await page.screenshot({ path: path.join(screenshots, "tools-strategy-check-1440.png"), fullPage: true });

  // 5. Severity filter actually hides a group, not just visually dims it.
  await page.locator('[data-cq-filter="warn"]').uncheck();
  await page.locator('[data-cq-filter="info"]').uncheck();
  await page.waitForTimeout(50);
  assert.equal(await page.locator('[data-cq-group="error"]').isVisible(), true, "error group stays visible");
  const warnGroup = page.locator('[data-cq-group="warn"]');
  if (await warnGroup.count()) assert.equal(await warnGroup.isHidden(), true, "warn group is hidden by the filter");

  // 6. Copy-to-clipboard on a finding's ref, and on a finding's value (the
  // actual over-length title text, not just its character count).
  await page.locator('[data-cq-filter="warn"]').check();
  await page.locator('[data-cq-filter="info"]').check();
  const firstRef = await page.locator(".cq-finding__ref").first().innerText();
  await page.locator("[data-cq-copy]").first().click();
  const clipboard = await page.evaluate(() => navigator.clipboard.readText());
  assert.equal(clipboard, firstRef, "the copy button copies the exact ref text");

  const valueBlock = page.locator(".cq-finding__value").first();
  await valueBlock.waitFor();
  const shownValue = await valueBlock.locator("blockquote").innerText();
  assert.ok(shownValue.length > 0, "the finding shows the actual column value, not just a length");
  await valueBlock.locator("[data-cq-copy]").click();
  const valueClipboard = await page.evaluate(() => navigator.clipboard.readText());
  assert.equal(valueClipboard, shownValue, "the value copy button copies the exact field text");

  // 7. The run is persisted: the URL updates to a real report id, and
  // reloading that URL shows the exact same report — this is the "shareable
  // link" contract.
  await page.waitForFunction(() => /\/tools\/strategy-check\/[^/?#]+/.test(location.hash));
  const reportUrl = page.url();
  assert.match(reportUrl, /#\/tools\/strategy-check\/[^/?#]+$/, "the URL carries a real report id");
  const findingsBeforeReload = await page.locator(".cq-finding__check").allInnerTexts();

  await page.reload({ waitUntil: "networkidle" });
  await page.locator("[data-testid=cq-report]").waitFor({ timeout: 10_000 });
  const findingsAfterReload = await page.locator(".cq-finding__check").allInnerTexts();
  assert.deepEqual(findingsAfterReload, findingsBeforeReload, "reloading the report URL shows the same findings");

  // "Copy link" produces the exact full, absolute URL of this report — the
  // thing the operator actually pastes into Slack/an email.
  await page.locator(".cq-report__link").click();
  const copiedLink = await page.evaluate(() => navigator.clipboard.readText());
  assert.equal(copiedLink, reportUrl, "Copy link copies the exact current report URL");

  // 8. The report shows up in the tool's own "Reports" list.
  await page.goto(`${baseUrl}/#/tools/strategy-check`, { waitUntil: "networkidle" });
  await page.locator("#cq-reports .tool-table").waitFor({ timeout: 10_000 });
  assert.ok(
    (await page.locator("#cq-reports .tool-table tbody tr").count()) >= 1,
    "the just-created report appears in the reports list",
  );
  assert.match(
    await page.locator("#cq-reports .tool-table tbody tr").first().innerText(),
    /sample-strategy\.xlsx/,
    "the reports list shows the source filename",
  );

  // 9. A clean file shows the explicit "no issues" state, not an empty table.
  const cleanXlsxPath = path.join(__dirname, "..", "..", "..", ".verify_tmp", "sample-strategy-clean.xlsx");
  await page.locator("#cq-file").setInputFiles(cleanXlsxPath);
  await page.locator("#cq-form button[type=submit]").click();
  await page.locator(".cq-clean").waitFor({ timeout: 10_000 });
  assert.match(await page.locator(".cq-clean").innerText(), /no issues found/i,
    "a clean file shows the explicit no-issues state, not an empty table");

  // 10. Mobile: no horizontal page scroll.
  const mobile = await browser.newContext({ viewport: { width: 390, height: 900 }, reducedMotion: "reduce" });
  const mobilePage = await mobile.newPage();
  await mobilePage.goto(`${baseUrl}/#/tools/strategy-check`, { waitUntil: "networkidle" });
  await mobilePage.locator("#cq-file").waitFor();
  await mobilePage.locator("#cq-file").setInputFiles(sampleFile);
  await mobilePage.locator("#cq-form button[type=submit]").click();
  await mobilePage.locator("[data-testid=cq-report]").waitFor({ timeout: 10_000 });
  const scrollWidth = await mobilePage.evaluate(() => document.documentElement.scrollWidth);
  const clientWidth = await mobilePage.evaluate(() => document.documentElement.clientWidth);
  assert.ok(scrollWidth <= clientWidth, `no horizontal page scroll at 390px (${scrollWidth} > ${clientWidth})`);
  await mobilePage.screenshot({ path: path.join(screenshots, "tools-strategy-check-390.png"), fullPage: true });
  await mobile.close();

  assert.deepEqual(browserErrors, [], "no console or page errors");
  await context.close();
  await browser.close();
  console.log("tools-strategy-check.cjs: all assertions passed");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
