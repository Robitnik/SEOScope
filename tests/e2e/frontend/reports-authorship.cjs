#!/usr/bin/env node
/* Browser smoke for task-93 (authorship on #/reports and Strategy Package
 * Check) — admin-only "Started by"/"Checked by" columns, chips, and the
 * "Activity" deep link into the audit log.
 *
 * Same two-domain split as account-tokens.cjs, and for the identical
 * reason: `AUTH_LOCALHOST_BYPASS` resolves BEFORE any cookie session in
 * `app/core/auth/middleware.py`'s order, so a real "member" login on
 * `127.0.0.1` is invisible — this file's member pass runs through the
 * PUBLIC domain (Cloudflare tunnel headers defeat `is_trusted_loopback()`),
 * while the admin pass stays on the local bypass, matching the rest of this
 * app's e2e suite.
 *
 *   BASE_URL=https://seo.theyovth.space ORIGIN_URL=http://127.0.0.1:6524 \
 *   node tests/e2e/frontend/reports-authorship.cjs
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { execSync } = require("node:child_process");
const { chromium } = require("playwright-core");

const publicUrl = process.env.BASE_URL || "https://seo.theyovth.space";
const localUrl = process.env.ORIGIN_URL || "http://127.0.0.1:6524";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");
const memberEmail = `qa-e2e-authorship-${Date.now()}@theyovth.space`;

function cli(command) {
  return execSync(`docker compose exec -T api poetry run yovth-report ${command}`, {
    cwd: path.join(__dirname, "..", "..", ".."),
    encoding: "utf8",
  });
}

// A successful patch REPLACES the `[data-author-id]` placeholder with a
// plain `.author-chip` — "resolved" means the placeholder marker is gone,
// not present-and-not-loading (there is no loading CLASS to poll for).
async function waitForAuthorsResolved(page) {
  await page.waitForFunction(
    () => document.querySelectorAll("[data-author-id]").length === 0
      && document.querySelectorAll(".author-chip").length > 0,
    { timeout: 10000 },
  );
}

async function adminPass() {
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });

  await page.goto(`${localUrl}/#/reports`, { waitUntil: "networkidle" });
  await waitForAuthorsResolved(page);
  assert.match(await page.locator("thead").innerText(), /started by/i, "admin sees Started by on #/reports");
  const batchRow = page.locator("[data-batch-row]").first();
  if (await batchRow.count()) {
    assert.match(await batchRow.innerText(), /started by/i, "batch card shows Started by");
  }
  await page.screenshot({ path: path.join(screenshots, "reports-authorship-1440.png"), fullPage: true });

  // No overflow INTRODUCED by the new column beyond the app shell's own
  // pre-existing baseline (see account-tokens.cjs's identical note — this
  // table already overflowed ~39px before task-93 touched it).
  await page.setViewportSize({ width: 390, height: 900 });
  await page.waitForTimeout(150);
  const overflowPx = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  assert.ok(overflowPx <= 40, `Started by column doesn't widen the pre-existing overflow further (got ${overflowPx}px)`);
  await page.screenshot({ path: path.join(screenshots, "reports-authorship-390.png"), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });

  // Run detail: chip + Activity link, and the link actually filters the log.
  const firstRunId = await page.locator("[data-run-row]").first().getAttribute("data-run-row");
  await page.goto(`${localUrl}/#/reports/${firstRunId}`, { waitUntil: "networkidle" });
  await waitForAuthorsResolved(page);
  assert.match(await page.locator(".reports-run-header").innerText(), /started by/i, "run header shows Started by");
  await page.click(".reports-run-header__actions a:has-text('Activity')");
  await page.waitForTimeout(600);
  assert.match(page.url(), /#\/admin\/audit\?resource_type=report_run&resource_id=/, "Activity opens the log filtered to this run");
  await page.locator("#audit-filters").waitFor({ timeout: 10000 });

  // Strategy Package Check: same shape, "Checked by".
  await page.goto(`${localUrl}/#/tools/strategy-package`, { waitUntil: "networkidle" });
  await waitForAuthorsResolved(page);
  assert.match(await page.locator("thead").innerText(), /checked by/i, "admin sees Checked by on the package list");
  await page.screenshot({ path: path.join(screenshots, "strategy-package-authorship-1440.png"), fullPage: true });

  assert.deepEqual(errors, [], `no browser errors (admin pass): ${errors.join(" | ")}`);
  await browser.close();
  console.log("admin pass OK");
}

async function memberPass() {
  const created = cli(`user-create --email "${memberEmail}" --name "QA E2E authorship" --role member`);
  const password = created.match(/password \(shown once, store it now\): (\S+)/)?.[1];
  assert.ok(password, `couldn't parse a password out of user-create's output:\n${created}`);

  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => {
    if (m.type() === "error" && !m.text().includes("cloudflareinsights")) errors.push(m.text());
  });
  const authorshipRequests = [];
  page.on("request", (req) => { if (req.url().includes("/api/v1/authorship")) authorshipRequests.push(req.url()); });

  try {
    await page.goto(`${publicUrl}/login.html`, { waitUntil: "domcontentloaded" });
    await page.fill("#login-email, input[type=email]", memberEmail);
    await page.fill("#login-password, input[type=password]", password);
    await page.click("button[type=submit]");
    await page.locator("#user-menu").waitFor({ state: "visible", timeout: 20000 });

    await page.goto(`${publicUrl}/#/reports`, { waitUntil: "networkidle" });
    await page.waitForTimeout(1000);
    assert.doesNotMatch(await page.locator("thead").innerText(), /started by/i, "member does not see Started by");
    assert.equal(await page.locator("[data-author-id]").count(), 0, "member has no author placeholders at all");

    await page.goto(`${publicUrl}/#/tools/strategy-package`, { waitUntil: "networkidle" });
    await page.waitForTimeout(1000);
    assert.doesNotMatch(await page.locator("thead").innerText(), /checked by/i, "member does not see Checked by");

    assert.equal(authorshipRequests.length, 0, "member's browser never calls /api/v1/authorship");
    assert.deepEqual(errors, [], `no browser errors (member pass): ${errors.join(" | ")}`);
    console.log("member pass OK");
  } finally {
    await browser.close();
    cli(`user-deactivate --email "${memberEmail}"`);
  }
}

async function main() {
  await adminPass();
  await memberPass();
  console.log("reports-authorship e2e: ok");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
