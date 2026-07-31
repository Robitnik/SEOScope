/* Browser smoke for task-30 (freshness chip + rolling-window refresh on #/trends).
 * Same invocation convention as the other suites in this folder:
 *   NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 \
 *   node tests/e2e/frontend/trends-refresh.cjs
 *
 * Fixture mode only — no provider unit is ever spent here. Covers: the chip
 * reads the stored `last_day` against the source's own `lag_days`; the refresh
 * preflight names the exact window before anything starts; the progress dialog
 * opens from a page that is NOT the dashboard (the whole point of the shared
 * `job-progress.js` singleton); the run survives closing the dialog; and the
 * reduced-motion pass renders every one of those states.
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:6524";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");

async function openTrends(context, fixture) {
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  await page.goto(`${baseUrl}/?fixture=${fixture}#/trends`, { waitUntil: "networkidle" });
  return { page, errors };
}

async function main() {
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });

  const { page, errors } = await openTrends(context, "trends-normal");

  // --- freshness chip --------------------------------------------------------
  await page.locator("#tr-freshness .freshness-chip").waitFor();
  const chip = (await page.locator("#tr-freshness .freshness-chip").textContent()).trim();
  assert.match(chip, /^Data through \w+ \d+/, `chip names the last collected day: ${chip}`);
  assert.match(chip, /up to date|days? behind/, `chip states the gap: ${chip}`);
  // The fixture's newest day is far in the past, so this must never read "up to date".
  assert.ok(chip.includes("behind"), `stale fixture must read as behind: ${chip}`);
  assert.equal(
    await page.locator("#tr-freshness .freshness-chip").evaluate((node) => node.className.includes("freshness-chip--stale")),
    true,
    "a long gap is styled as stale, not as a neutral note",
  );

  // --- preflight names the exact scope before spending anything --------------
  await page.locator("#tr-window").selectOption("14");
  await page.locator("#tr-refresh").click();
  await page.locator(".refresh-preflight").waitFor();
  const range = (await page.locator(".refresh-preflight h3").textContent()).trim();
  assert.match(range, /^\d{4}-\d{2}-\d{2} → \d{4}-\d{2}-\d{2}$/, `preflight names the window: ${range}`);
  const [start, end] = range.split(" → ").map((value) => Date.parse(`${value}T00:00:00Z`));
  assert.equal(Math.round((end - start) / 86_400_000), 13, "a 14-day window spans 14 inclusive days");
  assert.equal(
    Math.round((Date.parse(`${new Date().toISOString().slice(0, 10)}T00:00:00Z`) - end) / 86_400_000),
    1,
    "the window ends yesterday — today is still being written by both sources",
  );
  assert.match(await page.locator(".refresh-facts").textContent(), /Re-collect/, "preflight states the re-collect mode");
  await page.screenshot({ path: path.join(screenshots, "trends-refresh-preflight-1440.png") });

  // --- progress runs from a page that is not the dashboard -------------------
  await page.locator("#sync-footer [data-start-refresh]").click();
  await page.locator(".progress-matrix").waitFor();
  const providerHeaders = await page.locator(".progress-matrix thead th").allTextContents();
  assert.deepEqual(providerHeaders.slice(1), ["Search Console", "Reporting Ninja / GA4"], "only the two daily-history providers are collected");
  assert.match(await page.locator("[data-progress-eta]").textContent(), /cells? left/, "progress states what is left, never a fake percentage");
  await page.screenshot({ path: path.join(screenshots, "trends-refresh-progress-1440.png") });

  // Closing the dialog must not cancel the run (AGENTS §9).
  await page.getByLabel("Close dialog").first().click();
  assert.equal(await page.locator("#job-dock").isVisible(), true, "the run stays visible in the dock after closing");
  await page.locator("#job-dock-button").click();
  assert.equal(await page.locator("#sync-dialog").evaluate((dialog) => dialog.open), true, "progress reopens from the dock");
  await page.keyboard.press("Escape");

  // --- 390px: the freshness row must not push the page sideways --------------
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator("#tr-freshness").waitFor();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(overflow <= 0, `no horizontal page scroll at 390px (overflow: ${overflow}px)`);
  await page.screenshot({ path: path.join(screenshots, "trends-refresh-390.png"), fullPage: false });

  // --- empty history still offers the way out --------------------------------
  const empty = await openTrends(context, "trends-empty");
  await empty.page.locator("#tr-freshness").waitFor();
  assert.match(
    await empty.page.locator("#tr-freshness").textContent(),
    /No daily history collected/,
    "an empty portfolio says so instead of claiming a date",
  );
  errors.push(...empty.errors);

  assert.deepEqual(errors, [], `browser console errors: ${errors.join("; ")}`);
  await browser.close();
  console.log("trends refresh e2e: ok");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
