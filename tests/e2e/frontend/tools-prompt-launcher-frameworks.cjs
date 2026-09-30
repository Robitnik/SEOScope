/* Browser smoke for Prompt Launcher, strategy-driven templates (task-102) —
 * real backend, real Google Sheet and real xlsx files from AISeoTools/out.
 *
 * Template selector -> paste a Google Sheets link -> page rows with the client
 * matched by domain -> Copy puts the exact rendered launcher on the clipboard
 * -> the "copied" mark survives a reload (no weekly reset for these templates)
 * -> Reset clears it -> Blog Framework groups the multi-row articles -> a host
 * path to an .xlsx works too -> junk input shows an error, never a blank table
 * -> no horizontal scroll at 390px.
 *
 * Needs the agency Google account to see the sheet below.
 *
 * Invocation:
 *   NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 \
 *   node tests/e2e/frontend/tools-prompt-launcher-frameworks.cjs
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:6524";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");
const SHEET =
  "https://docs.google.com/spreadsheets/d/1UKjvBsyiNxgPDpseDJxPdNjwZ78HYOYtcaeIuT8PmdQ/edit?usp=sharing";
const BLOG_XLSX =
  "/home/elon/dev/seo/AISeoTools/out/GRP_grillpointnyc.com_2026-08-26_blog_strategy/GRP_BLOG_STRATEGY_Q32026.xlsx";

async function loadSource(page, value) {
  await page.locator("#pls-input").fill(value);
  await page.locator("#pls-load").click();
}

async function main() {
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, reducedMotion: "reduce" });
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const page = await context.newPage();
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => {
    // The two deliberate junk-input requests (step 9) answer 400 — Chromium logs those.
    if (message.type() === "error" && !/status of 400/.test(message.text())) browserErrors.push(message.text());
  });

  await page.goto(`${baseUrl}/#/tools/prompt-launcher`, { waitUntil: "networkidle" });
  await page.locator("#pl-template-select").waitFor();
  // Start from a clean slate: earlier runs leave the remembered source behind.
  await page.evaluate(() => {
    for (const key of Object.keys(localStorage)) {
      if (key.startsWith("yovth.prompt-launcher.")) localStorage.removeItem(key);
    }
  });

  // 1. The two new templates are in the selector, the client-kind ones keep their table.
  const options = await page.locator("#pl-template-select option").allInnerTexts();
  assert.deepEqual(options.slice(-2), ["Page Framework", "Blog Framework"]);

  // 2. Page Framework: paste the Google Sheets link.
  await page.locator("#pl-template-select").selectOption("page_framework");
  await page.locator("#pls-input").waitFor();
  await loadSource(page, SHEET);
  await page.locator(".pl-table tbody tr").first().waitFor({ timeout: 30_000 });
  assert.equal(await page.locator(".pl-table tbody tr").count(), 11, "11 page rows on the Q4 sheet");
  assert.match(await page.locator(".pls-summary").innerText(), /GRP/, "client matched");
  assert.match(await page.locator(".pls-summary").innerText(), /matched by domain/);
  assert.equal(await page.locator(".pl-badge--done").count(), 11, "every page already has a framework");
  assert.equal(await page.locator("#pls-sheet option[disabled]").count(), 2, "Structure and Blogs are not page sources");
  await page.screenshot({ path: path.join(screenshots, "tools-prompt-launcher-frameworks-1440.png"), fullPage: true });

  // 3. Copy puts the exact rendered launcher on the clipboard.
  const row = page.locator('.pl-row[data-row-key="https://grillpointnyc.com/menu/queens/"]');
  await row.locator(".pl-copy-btn").click();
  const clip = await page.evaluate(() => navigator.clipboard.readText());
  assert.match(clip, /^Read \/home\/elon\/dev\/seo\/AISeoTools\/docs\/PageFramework\.md and run it end to end\./);
  assert.match(clip, /MODE: {4}existing\nINPUT: {3}https:\/\/grillpointnyc\.com\/menu\/queens\/\nCLIENT: {2}GRP\n/);
  assert.match(clip, /Primary Key: grill point queens menu/);
  assert.match(clip, /Comments:\n {2}WHY:/, "multi-line comment is indented under its label");
  assert.match(await row.locator(".pl-copied-cell").innerText(), /\d/);
  assert.match(await page.locator("#pls-count").innerText(), /^1 of 11 copied/);

  // 4. The mark and the source survive a reload — and are NOT wiped by a week change.
  await page.reload({ waitUntil: "networkidle" });
  await page.locator("#pl-template-select").selectOption("page_framework");
  await page.locator(".pl-table tbody tr").first().waitFor({ timeout: 30_000 });
  assert.equal(await page.locator(".pl-row.is-copied").count(), 1, "copied mark survives reload");

  // 5. Hiding rows that already have a framework empties this fully-done sheet, visibly.
  await page.locator("#pls-hide-done").check();
  assert.match(await page.locator(".pl-empty-row").innerText(), /already has a framework/);
  await page.locator("#pls-hide-done").uncheck();

  // 6. Reset clears the marks.
  await page.locator("#pls-reset").click();
  assert.equal(await page.locator(".pl-row.is-copied").count(), 0, "Reset clears the marks");

  // 7. Blog Framework: articles are grouped from multi-row blocks.
  await page.locator("#pl-template-select").selectOption("blog_framework");
  await loadSource(page, SHEET);
  await page.locator(".pl-table tbody tr").first().waitFor({ timeout: 30_000 });
  assert.equal(await page.locator(".pl-table tbody tr").count(), 12, "12 articles from 95 sheet rows");
  const article = page.locator(".pl-row", { hasText: "Best Breakfast in NYC" }).first();
  await article.locator(".pl-copy-btn").click();
  const blog = await page.evaluate(() => navigator.clipboard.readText());
  assert.match(blog, /MODE: {4}blog\nINPUT: {3}Best Breakfast in NYC\n/);
  assert.match(blog, /Keywords: best breakfast nyc, best breakfast in nyc,/);
  assert.match(blog, /Anchors:\n {2}Grill Point's breakfast and full menu\n/);
  assert.match(blog, /Internal links:\n {2}https:\/\/grillpointnyc\.com\/menu\/\n/);
  assert.match(blog, /Commercial destination:\n/, "no column -> empty label, nothing invented");

  // 8. A host path to an .xlsx works as well.
  await loadSource(page, BLOG_XLSX);
  await page.locator(".pls-summary").filter({ hasText: "GRP_BLOG_STRATEGY_Q32026" }).waitFor({ timeout: 15_000 });
  assert.ok((await page.locator(".pl-table tbody tr").count()) > 0);

  // 9. Junk input is an error under the field, never a blank table.
  await loadSource(page, "hello world");
  await page.locator("#pls-error").waitFor({ state: "visible" });
  assert.match(await page.locator("#pls-error").innerText(), /Not a Google Sheets link/);
  await loadSource(page, "/etc/passwd.xlsx");
  await page.locator("#pls-error").filter({ hasText: /inside/ }).waitFor({ state: "visible" });

  // 10. The client-kind templates are untouched.
  await page.locator("#pl-template-select").selectOption("techaudit");
  await page.locator("#pl-clients-body .pl-table tbody tr").first().waitFor({ timeout: 15_000 });
  assert.equal(await page.locator("#pls-input").count(), 0, "no strategy input on client templates");

  // 11. Nothing inside the tool pokes out of the viewport at phone width. Measured on the
  // tool's own subtree: the app-shell `.header-start` is 39px wider than a 390px viewport on
  // EVERY route today (verified on the untouched TechAudit view), which is not this task's.
  await page.setViewportSize({ width: 390, height: 900 });
  await page.locator("#pl-template-select").selectOption("page_framework");
  await page.locator(".pl-table tbody tr").first().waitFor({ timeout: 30_000 });
  const pokingOut = await page.evaluate(() =>
    [...document.querySelectorAll("#tools-page-root *")]
      .filter((el) => !el.closest(".pl-table-wrap") && el.getBoundingClientRect().right > window.innerWidth + 1)
      .map((el) => `${el.tagName}.${el.className}`),
  );
  assert.deepEqual(pokingOut, [], `tool elements wider than the viewport: ${pokingOut.join(", ")}`);
  await page.screenshot({ path: path.join(screenshots, "tools-prompt-launcher-frameworks-390.png"), fullPage: true });

  assert.deepEqual(browserErrors, [], `browser errors: ${browserErrors.join(" | ")}`);
  await browser.close();
  console.log("tools-prompt-launcher-frameworks: OK");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
