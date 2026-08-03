#!/usr/bin/env node
/* Browser smoke for task-12. The task-13 harness may provide playwright-core.
 * Local example:
 *   NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:4173 \
 *   node tests/e2e/frontend/frontend-smoke.cjs
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
  await context.addInitScript(() => localStorage.clear());
  const page = await context.newPage();
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") browserErrors.push(message.text()); });

  await page.goto(`${baseUrl}/?fixture=all-states`, { waitUntil: "networkidle" });
  await page.locator(".client-card").first().waitFor();
  assert.equal(await page.locator(".client-card").count(), 4, "fixture renders four client cards");
  assert.equal(await page.getByText("No data", { exact: true }).count() > 0, true, "null is shown as missing, never as a number");
  assert.equal(await page.getByText("0", { exact: true }).count() > 0, true, "numeric zero remains visible");
  assert.match(await page.locator("#period-copy").innerText(), /Jul 13, 2026.*Jul 19, 2026.*Jul 06, 2026.*Jul 12, 2026/su, "exact current and previous ranges are visible");

  // Inline dates (frontend/AGENTS.md §3/§5): visible as page text, not only in a title attribute.
  const dateLines = await page.locator(".provider-dates").allInnerTexts();
  assert.equal(dateLines.length > 0, true, "provider blocks render an inline date line");
  assert.equal(dateLines.some((text) => text.includes("Snapshot as of")), true, "snapshot metrics show an as-of date, not a range");
  assert.equal(dateLines.some((text) => /\w{3} \d{2}, 2026.*vs.*\w{3} \d{2}, 2026/su.test(text)), true, "range metrics show exact current vs previous dates");
  assert.equal(await page.locator(".metric-table th[title]").count(), 0, "date info is not hover-only");

  // Health % + coverage (PLAN §21.2): the corner gauge always carries the value,
  // and coverage rides along in its accessible label. `null` never renders as `0`.
  assert.equal(await page.locator(".health-gauge__number", { hasText: "—" }).count() > 0, true, "a client with no complete service shows — for health, never 0");
  assert.equal(await page.locator(".health-gauge", { hasText: "78%" }).count(), 1, "computed health % renders on the gauge");
  assert.equal(await page.locator('.health-gauge[title*="2/3 services"]').count(), 1, "coverage travels with the health value");

  // Card -> client page (router.js `#/client/<id>`): logo, name, and the open icon are all entry points.
  const firstCard = page.locator(".client-card").first();
  const cardId = await firstCard.getAttribute("data-client-id");
  for (const selector of [".client-logo-link", ".client-title h2 a", ".client-open"]) {
    assert.equal(await firstCard.locator(selector).getAttribute("href"), `#/client/${cardId}`, `${selector} links to the client page`);
  }
  assert.match(await firstCard.locator(".client-domain > a").first().getAttribute("href"), /^https:\/\//, "the domain itself still links to the client's site");

  // Copy-URL button: a real, keyboard-reachable control (not the decorative
  // tabindex=-1 link icons) that copies the site URL and confirms.
  // `.client-copy` now matches TWO controls (copy client NAME and copy website URL). This block
  // asserts the URL one (see the aria-label/data-copy assertions below), so exclude the name
  // variant rather than tripping Playwright strict mode.
  const copyButton = firstCard.locator(".client-copy:not(.client-copy--name)");
  assert.equal(await copyButton.evaluate((el) => el.tagName), "BUTTON", "copy affordance is a real button");
  assert.match(await copyButton.getAttribute("aria-label"), /^Copy website URL/, "copy button has an accessible label");
  assert.match(await copyButton.getAttribute("data-copy"), /^https:\/\//, "copy button carries the site URL to copy");
  assert.equal(await copyButton.evaluate((el) => { el.focus(); return document.activeElement === el; }), true, "copy button is keyboard-focusable");

  for (const [width, height] of [[390, 844], [768, 900], [1024, 900], [1440, 1000], [1920, 1080]]) {
    await page.setViewportSize({ width, height });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), true, `no page overflow at ${width}px`);
  }
  await page.setViewportSize({ width: 1440, height: 1000 });

  // Sort control (PLAN §21.3): default order puts the fully unconfigured client last, and the mode persists in the URL.
  assert.equal(await page.locator(".client-card").last().getAttribute("data-client-id"), "client-onboarding", "client without connected services sorts last by default");
  await page.selectOption("#sort-select", "name");
  await page.waitForTimeout(50);
  assert.match(page.url(), /sort=name/, "sort mode persists in the URL");
  await page.reload({ waitUntil: "networkidle" });
  assert.equal(await page.locator("#sort-select").inputValue(), "name", "sort mode is restored from the URL on reload");
  await page.selectOption("#sort-select", "quality");
  await page.waitForTimeout(50);
  assert.doesNotMatch(page.url(), /sort=/, "default quality sort is not written to the URL");

  await page.locator('[data-group="vertex"]').click();
  await page.locator(".client-card").nth(1).waitFor();
  assert.equal(await page.locator(".client-card").count(), 2, "portfolio group filters the client roster");
  assert.match(page.url(), /group=vertex/, "portfolio group persists in the URL");
  await page.locator('[data-group="all"]').click();
  await page.locator(".client-card").nth(3).waitFor();

  await page.locator("#client-search").fill("Cleveland");
  await page.waitForTimeout(400);
  assert.equal(await page.locator(".client-card").count(), 1, "search filters cards");
  await page.locator("#clear-filters").click();
  await page.locator(".client-card").nth(3).waitFor();

  // task-33: Connect looks the account up at the provider first. Manual entry
  // is the fallback, revealed on demand, so no outcome is a dead end.
  await page.locator('.client-card[data-client-id="client-long-name"] [data-action="integrate"][data-provider="reporting_ninja"]').click();
  await page.locator("#integration-choices .choice").first().waitFor();
  assert.equal(await page.locator("[data-account-field]").first().isVisible(), false, "manual fields stay hidden while candidates are offered");
  assert.equal(await page.locator("#integration-connect").isVisible(), true, "picking a candidate is the primary action");
  await page.locator("#integration-manual-toggle").click();
  assert.equal(await page.locator('[name="domain"]').inputValue(), "advanced-restorative-implant-dentistry.example.com", "domain integration is prefilled from the client");
  assert.equal(await page.locator("[data-account-field]").first().isVisible(), true, "Reporting Ninja account field is shown");
  await page.locator("#integration-dialog [data-close]").first().click();

  await page.locator("#sync-all-button").click();
  assert.equal(await page.locator("#sync-dialog").evaluate((dialog) => dialog.open), true, "sync dialog opens");
  await page.locator("[data-start]").click();
  await page.locator(".progress-matrix").waitFor();
  assert.equal(await page.locator(".progress-matrix tbody tr").count(), 4, "progress is a client by provider matrix");
  await page.screenshot({ path: path.join(screenshots, "sync-progress-1440.png"), fullPage: false });
  await page.getByLabel("Close dialog").first().click();
  assert.equal(await page.locator("#job-dock").isVisible(), true, "closing dialog keeps the background job visible");
  await page.locator("#job-dock-button").click();
  assert.equal(await page.locator("#sync-dialog").evaluate((dialog) => dialog.open), true, "progress can be reopened");
  await page.keyboard.press("Escape");
  assert.equal(await page.locator("#job-dock-button").evaluate((button) => document.activeElement === button), true, "dialog returns focus to opener");

  await page.locator('.client-card[data-client-id="client-cleveland"] [data-action="detail"]').click();
  await page.locator("#history-chart").waitFor();
  assert.equal(await page.locator("#detail-dialog").evaluate((dialog) => dialog.open), true, "history drawer opens");
  assert.deepEqual(await page.locator("#history-chart").evaluate((canvas) => window.Chart.getChart(canvas).data.datasets[0].data), [82, 84, null, 87, 88, 91], "history preserves numeric values and missing gaps");
  await page.screenshot({ path: path.join(screenshots, "history-drawer-1440.png"), fullPage: false });
  await page.keyboard.press("Escape");

  await page.screenshot({ path: path.join(screenshots, "dashboard-1440.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: path.join(screenshots, "dashboard-390.png"), fullPage: true });

  await page.goto(`${baseUrl}/?fixture=empty`, { waitUntil: "networkidle" });
  await page.getByText("No clients found.").waitFor();
  await page.goto(`${baseUrl}/?fixture=error`, { waitUntil: "networkidle" });
  await page.getByText("Failed to load dashboard.").waitFor();

  assert.deepEqual(browserErrors, [], `browser console errors: ${browserErrors.join("; ")}`);
  await browser.close();
  console.log("frontend smoke: ok");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
