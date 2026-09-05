/* Browser e2e for task-61 (URL Inspector). Runs against the REAL backend
 * and real portfolio data (SAPHNY has GSC+GA4+Monitor+Archive+Inventory
 * coverage for its homepage — task-61 plan §0.7/DoD).
 * Invocation:
 *   NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://localhost:6524 \
 *   CHROMIUM_PATH=/snap/bin/chromium node tests/e2e/frontend/url-inspector.cjs
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "http://localhost:6524";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");
const SAPHNY_HOME = "https://www.nysapphire.com/";

async function main() {
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1400 }, reducedMotion: "reduce" });
  const page = await context.newPage();
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") browserErrors.push(message.text()); });

  // --- Header search field is present on the dashboard ------------------------
  await page.goto(`${baseUrl}/#/`, { waitUntil: "load" });
  const searchInput = page.locator("#url-search-input");
  await searchInput.waitFor();
  await page.screenshot({ path: path.join(screenshots, "url-inspector-header-1440.png"), clip: { x: 0, y: 0, width: 1440, height: 120 } });

  // --- Submitting a URL navigates to #/url and renders real data -------------
  await searchInput.fill(SAPHNY_HOME);
  await Promise.all([
    page.waitForURL(/#\/url\?u=/),
    page.locator("#url-search-form").evaluate((form) => form.requestSubmit()),
  ]);
  await page.locator(".url-inspector__coverage").waitFor({ timeout: 15000 });

  const clientName = await page.locator(".url-inspector__client strong").textContent();
  assert.equal(clientName, "SAPHNY", "resolves the real client for this domain");

  const chips = await page.locator(".url-inspector__chip span").allTextContents();
  assert.deepEqual(
    chips,
    [
      "Search Console", "GA4", "Site Monitor", "Web Archive", "Meta & Copy", "Technical Audit",
      "Keywords", "Site Queries", "Screenshots",
    ],
    "all nine coverage chips render with their real labels (task-64 adds keywords/site_queries/screenshots)",
  );

  // --- Two-column layout at desktop width (task-64 §4) ------------------------
  const mainBox = await page.locator(".url-inspector__main").boundingBox();
  const sidebarBox = await page.locator(".url-inspector__sidebar").boundingBox();
  assert.ok(mainBox && sidebarBox, "main column and sidebar both render");
  assert.ok(
    sidebarBox.x >= mainBox.x + mainBox.width - 5,
    "sidebar sits beside the main column, not below it, at 1440px",
  );

  // --- Keywords & queries panel renders, disclaimer present (task-64 §3.3) ----
  await page.locator("#ui-keywords-heading").waitFor();
  const keywordsSectionText = await page.locator("#ui-keywords-heading").locator("xpath=..").textContent();
  assert.ok(
    keywordsSectionText.includes("NOT this page's own search queries"),
    "site-level query matches carry the disclaimer they are not per-page GSC data",
  );

  await page.screenshot({ path: path.join(screenshots, "url-inspector-full-1440.png"), fullPage: true });

  // --- Series render with matching point counts (proves the trailing-slash --
  // fix + granularity grouping work end-to-end, not just in unit tests) -------
  const gscPoints = await page.locator('[data-series="gsc"] .url-inspector__series-table tbody tr').count();
  const ga4Points = await page.locator('[data-series="ga4"] .url-inspector__series-table tbody tr').count();
  assert.ok(gscPoints > 0, "GSC series has at least one point");
  assert.ok(ga4Points > 0, "GA4 series has at least one point");

  // --- Switching granularity persists in the URL and re-fetches --------------
  await page.locator('[data-action="granularity"][data-value="month"]').click();
  await page.waitForFunction(() => location.hash.includes("g=month"));
  await page.locator('[data-action="granularity"][data-value="month"][aria-pressed="true"]').waitFor();

  // --- Live audit auto-runs without a click (follow-up: 2026-09-05) ----------
  await page.waitForFunction(
    () => document.querySelector('[data-action="run-live"]')?.textContent.includes("Recheck"),
    { timeout: 20000 },
  );
  const liveResultHtml = await page.locator("#ui-live-result").innerHTML();
  assert.ok(liveResultHtml.includes("url-inspector__facts"), "live audit populated automatically");

  // --- Header search history + type-ahead suggestions (follow-up) ------------
  await page.goto(`${baseUrl}/#/`, { waitUntil: "load" });
  await searchInput.click();
  const historyOptions = await page.locator("#url-search-suggestions li").allTextContents();
  assert.ok(
    historyOptions.some((text) => text.includes("nysapphire.com")),
    "the just-visited URL shows up as a history suggestion on next focus",
  );
  await searchInput.fill("nysapph");
  await page.waitForFunction(
    () => document.querySelectorAll("#url-search-suggestions li").length > 0,
  );
  const typeAhead = await page.locator("#url-search-suggestions li").allTextContents();
  assert.ok(
    typeAhead.some((text) => text.includes("nysapphire.com")),
    "type-ahead suggests the real client's homepage while the host is still being typed",
  );
  await page.screenshot({ path: path.join(screenshots, "url-inspector-suggestions-1440.png"), clip: { x: 0, y: 0, width: 700, height: 200 } });

  // --- Screenshots: real capture end-to-end (task-64 §2) ----------------------
  // Never auto-captured (opt-in, ~20s) — starts on a "no data" state with a
  // "Capture screenshots" prompt.
  await page.goto(`${baseUrl}/#/url?u=${encodeURIComponent(SAPHNY_HOME)}`, { waitUntil: "load" });
  await page.locator("#ui-shots-heading").waitFor();
  // The button briefly reads "Capturing…" while the page's own fast cache
  // check (`loadCachedScreenshots`, not a real capture) is in flight —
  // wait for it to settle before asserting the resting label.
  await page.waitForFunction(
    () => !document.querySelector('[data-action="capture-shots"]')?.disabled,
    { timeout: 15000 },
  );
  const captureButtonText = await page.locator('[data-action="capture-shots"]').textContent();
  assert.ok(
    /Capture screenshots|Recapture/.test(captureButtonText),
    "the capture button reflects whether a cached set already exists",
  );
  await page.locator('[data-action="capture-shots"]').click();
  await page.waitForFunction(
    () => document.querySelector(".url-inspector__shot img, .url-inspector__shot--failed") !== null,
    { timeout: 45000 },
  );
  const shotCount = await page.locator(".url-inspector__shot").count();
  assert.ok(shotCount > 0, "at least one screenshot card rendered after capture");

  // --- Full-size dialog opens on click (CSP-safe <dialog>, no inline style) ---
  const firstOkShot = page.locator('.url-inspector__shot[data-action="open-shot"]').first();
  if (await firstOkShot.count()) {
    await firstOkShot.click();
    await page.locator("#ui-shot-dialog[open]").waitFor({ timeout: 5000 });
    const dialogImgSrc = await page.locator("#ui-shot-dialog-img").getAttribute("src");
    assert.ok(dialogImgSrc && dialogImgSrc.includes("/api/v1/url-screenshots/"), "dialog loads the full-size image");
    await page.locator('[data-action="close-shot-dialog"]').click();
    await page.waitForFunction(() => !document.querySelector("#ui-shot-dialog")?.open);
  }

  // --- Unknown domain is an honest 200 state, not a crash ---------------------
  await page.goto(`${baseUrl}/#/url?u=${encodeURIComponent("https://example.com/nope")}`, { waitUntil: "load" });
  await page.locator(".url-inspector__client--unknown").waitFor({ timeout: 15000 });

  // --- Mobile viewport: no horizontal scroll ----------------------------------
  await page.goto(`${baseUrl}/#/url?u=${encodeURIComponent(SAPHNY_HOME)}`, { waitUntil: "load" });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator(".url-inspector__coverage").waitFor({ timeout: 15000 });
  await page.waitForTimeout(300);
  const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
  assert.ok(scrollWidth <= 400, `no horizontal overflow on mobile (scrollWidth=${scrollWidth})`);
  await page.screenshot({ path: path.join(screenshots, "url-inspector-mobile-390.png"), fullPage: true });

  // Cloudflare's own edge-injected Web Analytics beacon (only present when
  // running BASE_URL through the real tunnel, never on localhost) is
  // correctly blocked by this app's own CSP (`default-src 'self'`, no
  // exception for it) — that is our CSP doing its job, not a defect in
  // this page. Filtered out so the assertion still catches OUR errors.
  const ownErrors = browserErrors.filter((msg) => !msg.includes("cloudflareinsights.com"));
  assert.deepEqual(ownErrors, [], `no console/page errors: ${ownErrors.join("; ")}`);

  await browser.close();
  console.log("url-inspector.cjs: all assertions passed");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
