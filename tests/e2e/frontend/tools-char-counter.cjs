/* Browser smoke for the Character Counter tool.
 * Same invocation convention as the other suites in this folder:
 *   NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 \
 *   node tests/e2e/frontend/tools-char-counter.cjs
 *
 * Drives the fixture-mode tool: hub card → typing updates the live count →
 * Paste inserts clipboard text at the cursor → the SEO length block reacts
 * to the same text with distinct too-short/good/too-long states → no
 * horizontal page scroll at 390px.
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
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const page = await context.newPage();
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });

  // 1. The hub lists the tool and finds it by tag.
  await page.goto(`${baseUrl}/?fixture=normal#/tools`, { waitUntil: "networkidle" });
  await page.locator(".tool-card").first().waitFor();
  await page.locator("#tools-search").fill("counter");
  assert.equal(await page.locator(".tool-card").count(), 1, "the hub finds the tool by keyword");
  await page.locator(".tool-card").click();
  await page.locator("#tc-input").waitFor();

  // 2. The count starts at zero and updates live as text is typed.
  assert.match(await page.locator("#tc-count").innerText(), /^0 characters$/, "starts at zero, not blank");
  await page.locator("#tc-input").fill("Hello");
  assert.match(await page.locator("#tc-count").innerText(), /^5 characters$/, "the count tracks typed text");
  await page.locator("#tc-input").fill("A");
  assert.match(await page.locator("#tc-count").innerText(), /^1 character$/, "singular wording for a single character");
  await page.screenshot({ path: path.join(screenshots, "tools-char-counter-1440.png"), fullPage: true });

  // 3. Paste inserts the system clipboard content at the cursor.
  await page.locator("#tc-input").fill("Before After");
  await page.evaluate(() => navigator.clipboard.writeText("MIDDLE-"));
  await page.locator("#tc-input").click();
  await page.keyboard.press("Home");
  for (let i = 0; i < "Before ".length; i += 1) await page.keyboard.press("ArrowRight");
  await page.locator("#tc-paste").click();
  // The paste handler awaits navigator.clipboard.readText() after the click
  // event resolves, so give that microtask a turn before reading the value.
  await page.waitForTimeout(200);
  assert.equal(await page.locator("#tc-input").inputValue(), "Before MIDDLE-After",
    "paste inserts the clipboard text at the caret position");
  assert.match(await page.locator("#tc-count").innerText(), /^19 characters$/, "the count reflects the pasted text");

  // 4. The SEO length block reads the same text against three guidelines,
  // each with its own state — not a single pass/fail.
  await page.locator("#tc-input").fill("");
  assert.deepEqual(await page.locator(".tc-seo-card__status").allInnerTexts(), ["Empty", "Empty", "Empty"],
    "an empty field is reported as empty, not silently zero");
  await page.locator("#tc-input").fill("This Is A Perfectly Sized Title Tag");
  assert.deepEqual(await page.locator(".tc-seo-card__status").allInnerTexts(), ["Good", "Too short", "Good"],
    "the same 36-character text is judged separately per field");
  assert.equal(await page.locator(".tc-seo-card").nth(0).getAttribute("class"), "tc-seo-card state-ok",
    "a good length carries the shared state-ok tone class");
  assert.equal(await page.locator(".tc-seo-card").nth(1).getAttribute("class"), "tc-seo-card state-attention",
    "a too-short length carries the shared state-attention tone class");
  const overLong = "x".repeat(200);
  await page.locator("#tc-input").fill(overLong);
  assert.deepEqual(await page.locator(".tc-seo-card__status").allInnerTexts(), ["Too long", "Too long", "Too long"],
    "200 characters is too long for all three fields");
  assert.equal(await page.locator(".tc-seo-card").nth(0).getAttribute("class"), "tc-seo-card state-failed",
    "a too-long length carries the shared state-failed tone class");

  // 5. Mobile: no horizontal page scroll.
  const mobile = await browser.newContext({ viewport: { width: 390, height: 900 }, reducedMotion: "reduce" });
  const mobilePage = await mobile.newPage();
  await mobilePage.goto(`${baseUrl}/?fixture=normal#/tools/char-counter`, { waitUntil: "networkidle" });
  await mobilePage.locator("#tc-input").waitFor();
  await mobilePage.locator("#tc-input").fill("This Is A Perfectly Sized Title Tag");
  const scrollWidth = await mobilePage.evaluate(() => document.documentElement.scrollWidth);
  const clientWidth = await mobilePage.evaluate(() => document.documentElement.clientWidth);
  assert.ok(scrollWidth <= clientWidth, `no horizontal page scroll at 390px (${scrollWidth} > ${clientWidth})`);
  await mobilePage.screenshot({ path: path.join(screenshots, "tools-char-counter-390.png"), fullPage: true });
  await mobile.close();

  assert.deepEqual(browserErrors, [], "no console or page errors");
  await context.close();
  await browser.close();
  console.log("tools-char-counter.cjs: all assertions passed");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
