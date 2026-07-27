/* Browser smoke for task-29 (Redirect Checker).
 * Same invocation convention as the other suites in this folder:
 *   NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 \
 *   node tests/e2e/frontend/tools-redirects.cjs
 *
 * Drives the fixture-mode tool: hub card → paste one link (immediate trace) →
 * saved check table → chain strip and hop-by-hop detail → run a queued row →
 * filters persist in the URL → no horizontal page scroll at 390px.
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:4173";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");
const CHECK = "#/tools/redirects/9c1f0a44-3e57-4c0a-9a1b-6f5f1d0f7a10";

async function main() {
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const page = await context.newPage();
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => {
    const text = message.text();
    // Fixture hosts (`*.example`) are deliberately unreachable.
    const isFixtureHost = /example|ERR_NAME_NOT_RESOLVED|ERR_CERT/.test(text);
    if (message.type() === "error" && !isFixtureHost) browserErrors.push(text);
  });

  // 1. The hub lists the tool and finds it by tag.
  await page.goto(`${baseUrl}/?fixture=normal#/tools`, { waitUntil: "networkidle" });
  await page.locator(".tool-card").first().waitFor();
  await page.locator("#tools-search").fill("redirect");
  assert.equal(await page.locator(".tool-card").count(), 1, "the hub finds the tool by keyword");
  await page.locator(".tool-card").click();
  await page.locator("#rd-form").waitFor();

  // 2. A single pasted link is traced immediately and nothing is saved.
  await page.locator("#rd-urls").fill("https://acme.example/blog/2019/office-lighting");
  assert.match(await page.locator("#rd-submit").innerText(), /Trace link/,
    "one link offers an immediate trace, not a saved check");
  await page.locator("#rd-submit").click();
  await page.locator(".rd-quick__card").waitFor();
  assert.match(await page.locator(".rd-quick__card").innerText(), /One redirect/,
    "the verdict is named in words, not only coloured");
  assert.equal(await page.locator(".rd-quick__card .rd-hop").count(), 2,
    "the chain is listed hop by hop, including the destination");
  assert.match(await page.locator(".rd-quick__card .rd-hop").first().innerText(), /301/,
    "the first hop shows the status the server actually returned");
  assert.match(await page.locator(".rd-quick__card").innerText(), /Canonical points elsewhere/,
    "signals are explained in operator language");
  await page.screenshot({ path: path.join(screenshots, "tools-redirects-form-1440.png"), fullPage: true });

  // 3. A pasted list becomes a saved check.
  await page.locator("#rd-urls").fill("https://acme.example/a\nhttps://acme.example/b");
  assert.match(await page.locator("#rd-submit").innerText(), /Check 2 links/,
    "the submit label says what will happen with a list");

  // 4. Deep link into the saved check — all state comes from the URL.
  await page.goto(`${baseUrl}/?fixture=normal${CHECK}`, { waitUntil: "networkidle" });
  await page.locator(".rd-table tbody tr.rd-row").first().waitFor();
  assert.equal(await page.locator(".rd-table tbody tr.rd-row").count(), 6, "every row of the check renders");

  // The chain strip reads as one glance: 301 → 302 → 200.
  const chainRow = page.locator("tr.rd-row", { hasText: "old-collection" });
  assert.deepEqual(
    await chainRow.locator(".rd-status").allInnerTexts(),
    ["301", "302", "200"],
    "the strip shows every hop status in order",
  );
  assert.match(await chainRow.innerText(), /Redirect chain/, "two hops are reported as a chain");
  assert.match(await chainRow.innerText(), /2 redirects/, "the hop count counts redirects, not responses");

  // Distinct states, not "ok / not ok".
  assert.match(await page.locator("tr.rd-row", { hasText: "/gone" }).innerText(), /Dead end/,
    "a chain ending in 404 is a dead end");
  assert.match(await page.locator("tr.rd-row", { hasText: "checkout" }).innerText(), /Blocked by the site/,
    "bot protection is never reported as a dead site");

  // An unchecked row shows "not checked", never a zero.
  const queuedRow = page.locator("tr.rd-row", { hasText: "office-lighting" });
  assert.match(await queuedRow.innerText(), /Not checked/, "a queued row says it was not checked");
  assert.doesNotMatch(await queuedRow.locator(".rd-chain").innerText(), /0 redirect/,
    "missing data is not rendered as zero");
  assert.equal(await queuedRow.locator("button[data-action='toggle']").isDisabled(), true,
    "there is nothing to expand before the row is checked");

  // 5. Hop-by-hop detail: URLs, what changed, and advice.
  await chainRow.locator("button[data-action='toggle']").click();
  await page.waitForTimeout(300);
  const detail = page.locator(".rd-detail").first();
  assert.equal(await detail.locator(".rd-hop").count(), 3, "the detail lists all three responses");
  assert.match(await detail.innerText(), /http → https/, "the detail names what changed on the hop");
  // "From → to" must be readable without inferring it from the next hop's URL.
  assert.match(await detail.locator(".rd-route").innerText(), /old-collection[\s\S]*collections\/summer/,
    "the detail states where the link starts and where it ends");
  assert.match(await detail.locator(".rd-route").innerText(), /via 2 redirects/,
    "the route summary counts the redirects");
  assert.equal(await detail.locator(".rd-hop__to").count(), 2,
    "every redirect hop says explicitly where it points");
  assert.match(await detail.locator(".rd-hop__to").first().innerText(), /redirects to/i,
    "the target of a hop is labelled, not implied");
  assert.match(await detail.innerText(), /Mixed 301 and 302/, "mixed permanence is explained");
  assert.match(await detail.locator(".rd-hop--final").innerText(), /destination/i,
    "the last hop is marked as the destination");
  assert.equal(await chainRow.locator("button[data-action='toggle']").getAttribute("aria-expanded"), "true",
    "the toggle exposes its state to assistive tech");
  await page.screenshot({ path: path.join(screenshots, "tools-redirects-chain-1440.png"), fullPage: true });

  // 6. Running a queued row stores a verdict that survives a reload.
  await queuedRow.locator("button[data-action='run']").click();
  await page.waitForTimeout(900);
  assert.match(await page.locator("tr.rd-row", { hasText: "office-lighting" }).innerText(), /One redirect/,
    "running a row records its verdict");
  await page.reload({ waitUntil: "networkidle" });
  await page.locator(".rd-table tbody tr.rd-row").first().waitFor();
  assert.match(await page.locator("tr.rd-row", { hasText: "office-lighting" }).innerText(), /One redirect/,
    "the result is stored, not just painted");

  // 6b. One click, one reaction (the task-27 delegation regression).
  await page.locator("#rd-filter-verdict").selectOption("chain");
  await page.waitForTimeout(400);
  await page.locator("#rd-filter-verdict").selectOption("");
  await page.waitForTimeout(400);
  await page.locator("#toast-region").evaluate((region) => { region.innerHTML = ""; });
  await page.locator("tr.rd-row", { hasText: "old-collection" }).locator("button[data-action='copy']").first().click();
  await page.waitForTimeout(500);
  assert.equal(await page.locator("#toast-region .toast-message").count(), 1,
    "one click produces exactly one toast, however many times the table was re-rendered");

  // 7. Filters narrow the table and persist in the URL.
  await page.locator("#rd-filter-verdict").selectOption("broken");
  await page.waitForTimeout(400);
  assert.equal(await page.locator(".rd-table tbody tr.rd-row").count(), 1, "the verdict filter narrows the table");
  assert.match(page.url(), /vd=broken/, "the active filter persists in the URL");
  await page.locator("#rd-filter-verdict").selectOption("");
  await page.waitForTimeout(400);
  await page.locator("#rd-search").fill("checkout");
  await page.waitForTimeout(600);
  assert.match(page.url(), /q=checkout/, "the search text persists in the URL");
  assert.equal(await page.locator(".rd-table tbody tr.rd-row").count(), 1, "search narrows the table");
  await page.locator("#rd-search").fill("nothing-matches");
  await page.waitForTimeout(600);
  assert.equal(await page.locator(".tool-empty").count(), 1, "an empty filter result offers a way out");
  await page.locator("button[data-action='clear-filters']").click();
  await page.waitForTimeout(500);
  assert.equal(await page.locator(".rd-table tbody tr.rd-row").count(), 6, "clearing the filters restores the rows");

  // 8. Mobile: the table scrolls itself, the page does not.
  const mobile = await browser.newContext({ viewport: { width: 390, height: 900 }, reducedMotion: "reduce" });
  const mobilePage = await mobile.newPage();
  await mobilePage.goto(`${baseUrl}/?fixture=normal${CHECK}`, { waitUntil: "networkidle" });
  await mobilePage.locator(".rd-table tbody tr.rd-row").first().waitFor();
  const scrollWidth = await mobilePage.evaluate(() => document.documentElement.scrollWidth);
  const clientWidth = await mobilePage.evaluate(() => document.documentElement.clientWidth);
  assert.ok(scrollWidth <= clientWidth, `no horizontal page scroll at 390px (${scrollWidth} > ${clientWidth})`);
  await mobilePage.screenshot({ path: path.join(screenshots, "tools-redirects-390.png"), fullPage: true });
  await mobile.close();

  assert.deepEqual(browserErrors, [], "no console or page errors");
  await context.close();
  await browser.close();
  console.log("tools-redirects.cjs: all assertions passed");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
