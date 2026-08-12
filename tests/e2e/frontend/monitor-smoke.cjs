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
  // task-37 §4 (the process fix): every other check in this file passes
  // `?fixture=...`, which short-circuits `api.js` before it ever makes a
  // real network call — that is exactly why F1's 422 and F3's
  // `[object Object]` shipped green. Fixture-mode requests only ever hit
  // `/fixtures/*.json`, so scoping this to `/api/v1/` is enough to isolate
  // failures the live pass below introduces from noise elsewhere in the file.
  const badResponses = [];
  page.on("response", (response) => {
    if (response.status() >= 400 && response.url().includes("/api/v1/")) {
      badResponses.push(`${response.status()} ${response.url()}`);
    }
  });

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
  assert.doesNotMatch(await page.locator("#mc-dns").innerText(), /has no dedicated check yet/, "F7: no dev-facing TODO copy left in the DNS card");
  // F5: DNS is 10 records — collapsed to 8 with a "Show all 10" chip, not
  // silently truncated. Clicking it reveals the rest.
  await page.locator("#mc-dns .monitor-dns-table").waitFor();
  assert.equal(await page.locator("#mc-dns tbody tr").count(), 8, "DNS table starts collapsed to 8 rows");
  const dnsShowAll = page.locator('[data-action="dns-show-all"]');
  await dnsShowAll.waitFor();
  assert.match(await dnsShowAll.innerText(), /Show all 10/);
  await dnsShowAll.click();
  assert.equal(await page.locator("#mc-dns tbody tr").count(), 10, "Show all reveals every DNS record");
  assert.equal(await page.locator('[data-action="dns-show-all"]').count(), 0, "Show all chip is gone once expanded");
  // F6: 26 open changes — capped at 25 with a "Load more" control, not one
  // 11,000px list.
  await page.locator("#mc-changes .monitor-changes").waitFor();
  assert.equal(await page.locator("#mc-changes .monitor-change").count(), 25, "change feed starts capped at 25 rows");
  const loadMore = page.locator('[data-action="changes-load-more"]');
  await loadMore.waitFor();
  assert.match(await page.locator(".monitor-changes-load-more p").innerText(), /Showing 25 of 26/);
  await loadMore.click();
  assert.equal(await page.locator("#mc-changes .monitor-change").count(), 26, "Load more reveals the remaining open change");
  assert.equal(await page.locator('[data-action="changes-load-more"]').count(), 0, "Load more control is gone once every change is shown");
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
    if (fixture === "monitor-running") {
      // task-37 F2: a run still `status: "running"` must show real
      // "Checked N of M" progress, and the "complete" toast must never fire
      // while it is still active — the exact bug (poller reading a stale
      // FINISHED run from `getMonitorSummary` and declaring victory in ~2s).
      await page.getByText(/Checked \d+ of \d+/, { exact: false }).first().waitFor({ timeout: 5000 });
      await page.waitForTimeout(2500); // longer than the 2s poll interval
      assert.equal(await page.locator(".toast-message", { hasText: /complete/i }).count(), 0, "no premature 'complete' toast while the run is still running");
    }
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

  // --- Live pass, no `?fixture=` (task-37 §4) -----------------------------
  // Hits whatever stack `BASE_URL` points at for real: real `/api/v1/monitor`
  // fetch, real `/api/v1/clients/{id}/monitor/pages` with the frontend's
  // actual default `sort=-last_checked_at` — the exact request F1 404'd on
  // and no fixture-driven check above can ever exercise. Idempotent and
  // read-only: it never calls `POST .../monitor/runs`, so rerunning against
  // the same client changes nothing server-side.
  await page.goto(`${baseUrl}/#/monitor`, { waitUntil: "networkidle" });
  await page.locator("#monitor-page-root").waitFor();

  const firstClientLink = page.locator(".monitor-client-table tbody tr a[href*='#/client/']").first();
  if (await firstClientLink.count()) {
    await firstClientLink.click();
    await page.locator("#monitor-client-page-root #mc-content").waitFor();
    await page.waitForLoadState("networkidle");
    // task-38 §1: this is the ONLY pass in this file hitting real concurrent
    // network timing — fixture mode short-circuits before `request()` even
    // runs, so it can never reproduce the abort race that left "Check
    // history" stuck in its skeleton forever (loadRuns' own request aborted
    // by resumePollingIfActive's two calls, all three sharing one key). One
    // assertion catches the whole class of bug regardless of cause: no
    // section may still claim `aria-busy="true"` a few seconds after the
    // page settled.
    await page.waitForTimeout(10_000);
    const stillBusy = await page.evaluate(() =>
      Array.from(document.querySelectorAll("#mc-content [aria-busy='true']")).map((el) => el.id || el.className)
    );
    assert.deepEqual(stillBusy, [], `no section stuck aria-busy after 10s: ${stillBusy.join(", ")}`);

    // F5 mid-page-life: `resumePollingIfActive` fires its two calls again on
    // reload, over the SAME "Check history" load this file just verified
    // settles — re-asserting after a reload is a safe, read-only way to
    // exercise that path a second time without starting a real check
    // against the client's live site (this file's read-only contract,
    // documented above).
    await page.reload({ waitUntil: "networkidle" });
    await page.locator("#mc-content").waitFor();
    await page.waitForTimeout(10_000);
    const stillBusyAfterReload = await page.evaluate(() =>
      Array.from(document.querySelectorAll("#mc-content [aria-busy='true']")).map((el) => el.id || el.className)
    );
    assert.deepEqual(stillBusyAfterReload, [], `no section stuck aria-busy after F5: ${stillBusyAfterReload.join(", ")}`);
  } else {
    console.log("monitor e2e: live pass skipped client-detail navigation — no clients in this stack's portfolio");
  }

  assert.deepEqual(badResponses, [], `no >=400 response from the live API: ${badResponses.join(" | ")}`);
  assert.deepEqual(browserErrors, [], `no uncaught browser errors across the run: ${browserErrors.join(" | ")}`);
  await browser.close();
  console.log("monitor e2e: ok");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
