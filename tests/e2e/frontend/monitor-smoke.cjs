#!/usr/bin/env node
/* Browser smoke for task-36 (Site Monitor UI). Sibling to frontend-smoke.cjs
 * (task-12/13, not edited here — different task's owned file) rather than an
 * addition to it, so this can be authored/rerun independently. Same harness
 * convention:
 *   NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:4173 \
 *   node tests/e2e/frontend/monitor-smoke.cjs
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:4173";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");
const VIEWPORTS = [[390, 844], [768, 900], [1024, 900], [1440, 1000], [1920, 1080]];

async function assertNoOverflow(page, label) {
  for (const [width, height] of VIEWPORTS) {
    await page.setViewportSize({ width, height });
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth),
      true,
      `no page overflow at ${width}px on ${label}`,
    );
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
}

async function main() {
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  const page = await context.newPage();
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") browserErrors.push(message.text()); });

  // --- Portfolio page, populated fixture --------------------------------
  // `all-states` — same "just render the populated default" flag frontend-smoke.cjs
  // already uses; no monitor function has a special case for it, so every
  // monitor fetch falls through to its own default bundle (dashboard-all-states
  // for the client roster, monitor-portfolio-normal for summaries/changes).
  await page.goto(`${baseUrl}/?fixture=all-states#/monitor`, { waitUntil: "networkidle" });
  await page.locator(".monitor-strip").waitFor();
  assert.equal(await page.locator(".monitor-strip__stat--critical .monitor-strip__value").innerText(), "1", "one client with open critical (client-cleveland)");
  assert.equal(await page.locator(".monitor-critical-list .monitor-change-row").count(), 1, "the open-critical section lists exactly the one open critical change");
  assert.match(await page.locator(".monitor-critical-list .monitor-change-row__type").first().innerText(), /Noindex appeared/, "change_type is humanized, not a raw identifier");
  assert.equal(await page.locator(".monitor-client-table tbody tr").count(), 4, "client table lists all 4 fixture clients");
  assert.equal(await page.locator(".monitor-client-state--paused").count(), 1, "the paused fixture client (client-nyc-hearing) shows the paused state, not a fabricated OK");
  await page.screenshot({ path: path.join(screenshots, "monitor-portfolio-1440.png"), fullPage: true });
  await assertNoOverflow(page, "monitor portfolio (populated)");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: path.join(screenshots, "monitor-portfolio-390.png"), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });

  // Acknowledge, keyboard-driven: tab to the first ack button and press it,
  // then confirm the row leaves the "Open critical" section (task-36's "already
  // triaged" reading — it stays visible with an Acknowledged chip in the
  // general feed below instead, per monitor-page.js's `applyAcknowledgement`).
  const ackButton = page.locator(".monitor-critical-list .monitor-change-row__ack").first();
  await ackButton.focus();
  assert.equal(await ackButton.evaluate((el) => document.activeElement === el), true, "Acknowledge is keyboard-focusable");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelectorAll(".monitor-critical-list .monitor-change-row").length === 0, { timeout: 5000 });

  // --- Portfolio page, empty fixture -------------------------------------
  await page.goto(`${baseUrl}/?fixture=monitor-empty#/monitor`, { waitUntil: "networkidle" });
  await page.locator(".monitor-strip").waitFor();
  assert.equal(await page.locator(".monitor-strip__stat--critical .monitor-strip__value").innerText(), "0", "empty fixture: zero clients with open critical");
  assert.equal(await page.locator(".monitor-client-state--never_checked").count(), 4, "every client reads Never checked, not a fabricated OK");

  // --- Client page: dense/populated fixture ------------------------------
  await page.goto(`${baseUrl}/?fixture=all-states#/client/client-cleveland/monitor`, { waitUntil: "networkidle" });
  await page.locator("#mc-blocks").waitFor();
  // monitor-client-dense.json's robots_status is 200 and carries no open
  // `robots_blocks_site` change, so the honest conclusion is "allowed" — the
  // dedicated "blocked" scenario below (site-scope change strip) covers the
  // opposite case.
  assert.match(await page.locator("#mc-robots .monitor-conclusion").innerText(), /Googlebot: allowed/, "robots conclusion reads allowed for the populated fixture");
  await page.screenshot({ path: path.join(screenshots, "monitor-client-1440.png"), fullPage: true });
  await assertNoOverflow(page, "monitor client (dense)");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: path.join(screenshots, "monitor-client-390.png"), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });

  // Diff viewer: open via keyboard, focus moves in, Escape closes and returns focus.
  const historyButton = page.locator('[data-action="robots-history"]');
  await historyButton.focus();
  await page.keyboard.press("Enter");
  await page.locator("#monitor-diff-dialog[open]").waitFor();
  await page.screenshot({ path: path.join(screenshots, "monitor-diff-1440.png") });
  await page.waitForFunction(() => document.activeElement && document.getElementById("monitor-diff-dialog").contains(document.activeElement));
  await page.keyboard.press("Escape");
  await page.locator("#monitor-diff-dialog[open]").waitFor({ state: "hidden" });
  assert.equal(await historyButton.evaluate((el) => document.activeElement === el), true, "focus returns to the History button that opened the diff viewer");

  // --- Client page: state fixtures (task-36 §3.5) ------------------------
  // Real client ids from dashboard-all-states.json — `loadClient()` looks the
  // id up in the dashboard fixture and renders "Client not found" for
  // anything else, regardless of the monitor-* scenario flag.
  for (const [fixture, expectText] of [
    ["monitor-never-checked", "Never checked."],
    ["monitor-running", "check running"],
    ["monitor-baseline", "Baseline recorded"],
    ["monitor-blocked", "Blocked by site protection"], // site-scope `monitor_blocked` change, humanized by changeTypeLabel
    ["monitor-truncated", "Truncated"],
    ["monitor-dns-unavailable", "DNS could not be checked"],
  ]) {
    await page.goto(`${baseUrl}/?fixture=${fixture}#/client/client-cleveland/monitor`, { waitUntil: "networkidle" });
    await page.locator("#mc-content").waitFor();
    await page.getByText(expectText, { exact: false }).first().waitFor({ timeout: 5000 });
  }

  // Paused-client state comes from the dashboard fixture, not a monitor-* flag
  // (client-nyc-hearing is already paused in dashboard-all-states.json).
  await page.goto(`${baseUrl}/?fixture=all-states#/client/client-nyc-hearing/monitor`, { waitUntil: "networkidle" });
  await page.locator("#mc-content").waitFor();
  await page.getByText("Monitoring off", { exact: false }).first().waitFor({ timeout: 5000 });

  // --- Network error --------------------------------------------------
  await page.goto(`${baseUrl}/?fixture=error#/monitor`, { waitUntil: "networkidle" });
  await page.locator(".state-panel.state-failed").waitFor();
  assert.equal(await page.getByRole("button", { name: /retry/i }).count() > 0, true, "portfolio error state offers Retry");

  assert.deepEqual(browserErrors, [], `no uncaught browser errors across the run: ${browserErrors.join(" | ")}`);
  await browser.close();
  console.log("monitor e2e: ok");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
