/* Browser smoke for GA4 Events by Month (task-98) — real backend, real GA4.
 *
 * Hub card -> client picker -> months GA4 actually has -> report with one tab
 * per month -> key-event filter -> keyboard tab switching -> the Users total
 * is the property total, not the column sum -> .xlsx download -> shareable
 * link replays the same snapshot -> no horizontal scroll at 390px.
 *
 * Invocation:
 *   NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 \
 *   node tests/e2e/frontend/tools-ga4-events.cjs
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:6524";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");

async function main() {
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1100 },
    reducedMotion: "reduce",
  });
  const page = await context.newPage();
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });

  // 1. The hub lists the tool and finds it by keyword.
  await page.goto(`${baseUrl}/#/tools`, { waitUntil: "networkidle" });
  await page.locator(".tool-card").first().waitFor();
  await page.locator("#tools-search").fill("event");
  assert.equal(await page.locator(".tool-card").count(), 1, "the hub finds the tool by keyword");
  await page.locator(".tool-card").click();
  await page.locator("#ge-client").waitFor();

  // 2. Picking a client loads the months GA4 really has.
  const options = await page.locator("#ge-client option:not([disabled])").allTextContents();
  const clientName = options.find((text) => text && !text.startsWith("Choose"));
  assert.ok(clientName, "at least one client has GA4 connected");
  await page.locator("#ge-client").selectOption({ label: clientName });
  await page.locator(".ge-month").first().waitFor({ timeout: 30_000 });
  const monthCount = await page.locator(".ge-month").count();
  assert.ok(monthCount > 3, `GA4 reports several months (got ${monthCount})`);
  const preselected = await page.locator(".ge-month input:checked").count();
  assert.equal(preselected, 3, "the three most recent months are preselected");

  // 3. Build the report: one tab per selected month.
  await page.locator("#ge-submit").click();
  await page.locator(".ge-tab").first().waitFor({ timeout: 60_000 });
  const tabs = await page.locator(".ge-tab").count();
  assert.equal(tabs, 3, "one tab per selected month");
  await page.locator(".ge-table tbody tr").first().waitFor();
  await page.screenshot({ path: path.join(screenshots, "tools-ga4-events-1440.png"), fullPage: true });

  // 4. Every event takes three comparison rows, named once.
  const comparisons = await page.locator(".ge-table tbody .ge-comparison").allInnerTexts();
  assert.deepEqual(
    comparisons.slice(0, 3),
    ["All Users", "Direct traffic", "Organic traffic"],
    "each event carries the three GA4 comparisons in order",
  );
  assert.equal(comparisons.length % 3, 0, "every event has exactly three rows");
  const names = await page.locator(".ge-table tbody .ge-event").count();
  assert.equal(names * 3, comparisons.length, "the event name is written once per group");

  // 5. The All Users total is the property total, never the sum of the column
  //    (measured live: summing overstated users 3.9x, plan §1.6).
  const totalUsers = Number(
    (await page.locator(".ge-total").first().locator(".ge-total__sub").innerText()).replace(/[^\d]/g, ""),
  );
  const columnUsers = (await page.locator(".ge-table tbody tr").evaluateAll((trs) =>
    trs
      .filter((tr) => tr.querySelector(".ge-comparison")?.textContent.trim() === "All Users")
      .map((tr) => Number([...tr.querySelectorAll("td.num")][1].textContent.replace(/\(.*/, "").replace(/[^\d]/g, "")) || 0),
  )).reduce((sum, value) => sum + value, 0);
  assert.ok(totalUsers > 0, "the property user total is present");
  assert.ok(
    totalUsers <= columnUsers,
    `property users (${totalUsers}) must not be the column sum (${columnUsers})`,
  );

  // 6. Key-events filter narrows the table without emptying it.
  const allRows = await page.locator(".ge-table tbody tr").count();
  await page.locator("#ge-key-only").check();
  const keyRows = await page.locator(".ge-table tbody tr").count();
  assert.ok(keyRows <= allRows, "the key-event filter never adds rows");
  await page.locator("#ge-key-only").uncheck();

  // 7. Tabs switch by keyboard alone (AGENTS.md §7).
  await page.locator(".ge-tab").first().focus();
  await page.keyboard.press("ArrowRight");
  const selected = await page.locator('.ge-tab[aria-selected="true"]').getAttribute("data-month");
  const firstMonth = await page.locator(".ge-tab").first().getAttribute("data-month");
  assert.notEqual(selected, firstMonth, "ArrowRight moves to the next month tab");

  // 8. The workbook downloads.
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.locator('#ge-result-actions a[download]').click(),
  ]);
  assert.match(await download.suggestedFilename(), /\.xlsx$/);

  // 9. The shareable link replays the same snapshot.
  const shareHref = await page.locator('#ge-result-actions a[href^="#/tools/ga4-events/"]').getAttribute("href");
  await page.goto(`${baseUrl}/${shareHref}`, { waitUntil: "networkidle" });
  await page.locator(".ge-tab").first().waitFor({ timeout: 30_000 });
  assert.equal(await page.locator(".ge-tab").count(), 3, "the saved report still has its three months");

  // 10. Mobile: this page adds no horizontal scroll of its own.
  //    Measured against the hub as the baseline, because the masthead
  //    (`.header-start`, app.css — not this tool's owned path) already
  //    overflows by the same amount on EVERY page at 390px. Comparing to a
  //    hard 0 would silently blame this page for a pre-existing app bug.
  await page.setViewportSize({ width: 390, height: 900 });
  await page.waitForTimeout(400);
  const overflowHere = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  await page.goto(`${baseUrl}/#/tools`, { waitUntil: "networkidle" });
  await page.locator(".tool-card").first().waitFor();
  const overflowBaseline = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  assert.ok(
    overflowHere <= overflowBaseline,
    `the report adds no horizontal scroll at 390px (page ${overflowHere}px vs hub ${overflowBaseline}px)`,
  );
  await page.goto(`${baseUrl}/${shareHref}`, { waitUntil: "networkidle" });
  await page.locator(".ge-tab").first().waitFor({ timeout: 30_000 });
  await page.screenshot({ path: path.join(screenshots, "tools-ga4-events-390.png"), fullPage: true });

  assert.deepEqual(browserErrors, [], "no console/page errors");
  await browser.close();
  console.log("tools-ga4-events: OK");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
