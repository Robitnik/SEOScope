/* Browser smoke for the Framework Check tool — real backend, real .docx
 * upload: hub card -> upload -> live findings -> the `kind` selector
 * actually changes what the backend checks (FAQ band is blog-only) ->
 * no horizontal page scroll at 390px.
 *
 * Invocation:
 *   NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 \
 *   node tests/e2e/frontend/tools-framework-check.cjs
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:4173";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");
const sampleFile = path.join(__dirname, "..", "..", "..", ".verify_tmp", "sample-framework.docx");

async function main() {
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1200 }, reducedMotion: "reduce" });
  const page = await context.newPage();
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });

  // 1. The hub lists the tool and finds it by tag.
  await page.goto(`${baseUrl}/#/tools`, { waitUntil: "networkidle" });
  await page.locator(".tool-card").first().waitFor();
  await page.locator("#tools-search").fill("copywriter");
  assert.equal(await page.locator(".tool-card").count(), 1, "the hub finds the tool by keyword");
  await page.locator(".tool-card").click();
  await page.locator("#cq-file").waitFor();

  // 2. Default "Page framework" kind: the one FAQ question is not flagged
  // for count (framework-rules.md has no FAQ minimum for pages).
  await page.locator("#cq-file").setInputFiles(sampleFile);
  await page.locator("#cq-form button[type=submit]").click();
  await page.locator("[data-testid=cq-report]").waitFor({ timeout: 10_000 });
  let checks = await page.locator(".cq-finding__check").allInnerTexts();
  assert.ok(!checks.includes("faq-count"), "page kind: no FAQ count band (framework-rules.md)");
  assert.ok(checks.includes("meta-title-length"), "the real engine flagged the 43-char title (floor 50)");
  await page.screenshot({ path: path.join(screenshots, "tools-framework-check-1440.png"), fullPage: true });

  // 3. The em-dash findings show the actual offending heading text, not just
  // "em dash present".
  const emDashFinding = page.locator(".cq-finding", { hasText: "voice-em-dash" }).first();
  const emDashValue = await emDashFinding.locator(".cq-finding__value blockquote").innerText();
  assert.match(emDashValue, /—/, "the shown value contains the actual em dash from the heading");

  // 4. Persisted with a shareable link: URL carries a real id, reload keeps
  // the same findings, and it shows up in the tool's own "Reports" list.
  await page.waitForFunction(() => /\/tools\/framework-check\/[^/?#]+/.test(location.hash));
  assert.match(page.url(), /#\/tools\/framework-check\/[^/?#]+$/, "the URL carries a real report id");
  const findingsBeforeReload = checks;
  await page.reload({ waitUntil: "networkidle" });
  await page.locator("[data-testid=cq-report]").waitFor({ timeout: 10_000 });
  assert.deepEqual(
    await page.locator(".cq-finding__check").allInnerTexts(), findingsBeforeReload,
    "reloading the report URL shows the same findings",
  );

  // 5. Switching to "Blog framework" and re-running applies the blog FAQ band.
  await page.goto(`${baseUrl}/#/tools/framework-check`, { waitUntil: "networkidle" });
  await page.locator("#cq-reports .tool-table").waitFor({ timeout: 10_000 });
  assert.match(
    await page.locator("#cq-reports .tool-table tbody tr").first().innerText(),
    /sample-framework\.docx/,
    "the just-created report appears in the reports list",
  );
  await page.locator("#cq-file").setInputFiles(sampleFile);
  await page.selectOption("#cq-kind", "blog");
  await page.locator("#cq-form button[type=submit]").click();
  await page.locator("[data-testid=cq-report]").waitFor({ timeout: 10_000 });
  checks = await page.locator(".cq-finding__check").allInnerTexts();
  assert.ok(checks.includes("faq-count"), "blog kind: 1 FAQ question is below blog-rules.md's FAQ_MIN=4");

  // 6. Mobile: no horizontal page scroll.
  const mobile = await browser.newContext({ viewport: { width: 390, height: 900 }, reducedMotion: "reduce" });
  const mobilePage = await mobile.newPage();
  await mobilePage.goto(`${baseUrl}/#/tools/framework-check`, { waitUntil: "networkidle" });
  await mobilePage.locator("#cq-file").waitFor();
  await mobilePage.locator("#cq-file").setInputFiles(sampleFile);
  await mobilePage.locator("#cq-form button[type=submit]").click();
  await mobilePage.locator("[data-testid=cq-report]").waitFor({ timeout: 10_000 });
  const scrollWidth = await mobilePage.evaluate(() => document.documentElement.scrollWidth);
  const clientWidth = await mobilePage.evaluate(() => document.documentElement.clientWidth);
  assert.ok(scrollWidth <= clientWidth, `no horizontal page scroll at 390px (${scrollWidth} > ${clientWidth})`);
  await mobilePage.screenshot({ path: path.join(screenshots, "tools-framework-check-390.png"), fullPage: true });
  await mobile.close();

  assert.deepEqual(browserErrors, [], "no console or page errors");
  await context.close();
  await browser.close();
  console.log("tools-framework-check.cjs: all assertions passed");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
