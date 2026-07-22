#!/usr/bin/env node
/* Browser e2e for task-22 (sync modal auth UX). Same harness convention as
 * frontend-smoke.cjs — see tests/e2e/frontend/README.md for how to run it.
 *
 *   BASE_URL=http://127.0.0.1:4173 \
 *   CHROMIUM_PATH=/snap/bin/chromium \
 *   NODE_PATH=/tmp/yovth-e2e/node_modules \
 *   node tests/e2e/frontend/sync-auth-flow.cjs
 *
 * Exercises the fixture-mode auth demo built into `frontend/js/api.js`
 * (Semrush parks at `awaiting_auth` for the first client of a run, then
 * resumes once `startProviderLogin` has been called for that provider) —
 * no Docker/backend/real Chrome needed.
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:4173";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");

async function main() {
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  try {
    await run(browser);
  } finally {
    await browser.close();
  }
}

async function run(browser) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  await context.addInitScript(() => localStorage.clear());
  const page = await context.newPage();
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") browserErrors.push(message.text()); });

  const semrushRow = '.session-row[data-session-provider="semrush"]';
  const semrushButton = `${semrushRow} [data-session-action="semrush"]`;
  const rnRow = '.session-row[data-session-provider="reporting_ninja"]';

  // Scope the run to one client (Cleveland: ahrefs/semrush/gsc configured,
  // reporting_ninja not_configured) so the not_configured/never-a-button
  // assertion below is meaningful — the "all clients" run has reporting_ninja
  // configured for one of the four and would never show a pure not_configured row.
  await page.goto(`${baseUrl}/?fixture=all-states`, { waitUntil: "networkidle" });
  await page.locator('.client-card[data-client-id="client-cleveland"] [data-action="sync"]').click();
  await page.locator("[data-start]").click();
  await page.locator(".session-stage").waitFor();

  // 1. Dead session -> "Login required" row + an available login button.
  await page.locator(semrushRow).getByText("Login required", { exact: true }).waitFor({ timeout: 10000 });
  assert.equal(await page.locator(semrushButton).getAttribute("aria-disabled"), "false", "the login button is enabled once the session is actually dead");
  assert.equal(await page.locator(semrushButton).isVisible(), true, "the login button is keyboard/pointer reachable");

  // A not_configured provider (reporting_ninja for this client) never gets a login button.
  await page.locator(rnRow).getByText("Not configured", { exact: true }).waitFor();
  assert.equal(await page.locator(`${rnRow} [data-session-action]`).count(), 0, "not_configured never shows a login action");

  // Other services are visibly still collecting while Semrush waits — the matrix isn't frozen.
  const ahrefsCellBefore = await page.locator(".progress-matrix tbody td").nth(1).innerText();

  // 2. Keyboard-only activation, and focus must not be lost across the state change.
  await page.locator(semrushButton).focus();
  await page.keyboard.press("Enter");
  await page.waitForTimeout(50);
  assert.equal(await page.evaluate(() => document.activeElement?.dataset?.sessionAction), "semrush", "focus stays on the same control right after activation (starting phase)");

  // 3. Waiting state: busy button (aria-disabled, still focusable), countdown copy, other work continues.
  await page.locator(semrushRow).getByText("Waiting for login…", { exact: true }).waitFor({ timeout: 5000 });
  assert.equal(await page.locator(semrushButton).getAttribute("aria-disabled"), "true", "the button reports busy while waiting");
  assert.equal(await page.evaluate(() => document.activeElement?.dataset?.sessionAction), "semrush", "focus still hasn't moved once in the waiting phase");
  await page.locator(semrushRow).getByText(/\d{2}:\d{2} remaining\./).waitFor();
  const ahrefsCellDuringWait = await page.locator(".progress-matrix tbody td").nth(1).innerText();
  assert.notEqual(ahrefsCellBefore, ahrefsCellDuringWait, "other providers keep visibly progressing while Semrush is parked, not frozen");

  // 6 (checked here, while the row is guaranteed still present): aria-live
  // carries the current per-provider summary for screen readers.
  const liveText = await page.locator(".session-stage [aria-live]").innerText();
  assert.match(liveText, /Semrush/, "aria-live summary names the provider");

  await page.screenshot({ path: path.join(screenshots, "sync-auth-waiting-1440.png"), fullPage: false });

  // 4. Close and reopen the dialog mid-wait: job is not cancelled, and the wait state restores.
  await page.getByLabel("Close dialog").first().click();
  assert.equal(await page.locator("#job-dock").isVisible(), true, "closing the dialog mid-wait keeps the background job alive");
  await page.locator("#job-dock-button").click();
  await page.locator(semrushRow).getByText(/Waiting for login…|Login succeeded|Session active/).waitFor();

  // 5. Auto-continue: no manual restart. Ahrefs/GSC for this single client
  // finish long before the parked Semrush cell resumes, so the whole job
  // typically goes terminal in the very same tick Semrush finally succeeds —
  // the session-stage panel is then correctly hidden (job done, matrix covers
  // it) rather than lingering on "active". Assert the terminal outcome
  // itself, not a transient row that may legitimately never render.
  await page.locator("#sync-title").getByText(/Collection finished/).waitFor({ timeout: 15000 });
  assert.equal(await page.locator('[data-start]').count(), 0, "collection never needed a second manual start");
  await page.locator(".progress-matrix").getByText("Completed").first().waitFor();

  await page.screenshot({ path: path.join(screenshots, "sync-auth-resumed-1440.png"), fullPage: false });

  // 7. Timeout copy — verified against the frozen fixture rather than waiting out the real 15-minute deadline.
  await page.goto(`${baseUrl}/?fixture=sessions-timeout`, { waitUntil: "networkidle" });
  await page.locator("#sync-all-button").click();
  await page.locator("[data-start]").click();
  await page.locator(semrushRow).getByText("Wait timed out", { exact: true }).waitFor();
  assert.equal(await page.locator(`${semrushRow} [data-session-action]`).count(), 0, "a timed-out row is not actionable again from here — Retry failed covers it");
  await page.locator(semrushRow).getByText(/Other services and clients were not affected/).waitFor();

  assert.deepEqual(browserErrors, [], `browser console errors: ${browserErrors.join("; ")}`);
  console.log("sync-auth-flow: ok");
}

main()
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  })
  .then(() => process.exit(process.exitCode || 0));
