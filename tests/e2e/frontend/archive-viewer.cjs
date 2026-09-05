/* Browser e2e for task-63 (Web Archive replay-viewer fidelity/urls). Runs
 * against the REAL backend and real captured portfolio data.
 * Invocation:
 *   NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://localhost:6524 \
 *   CHROMIUM_PATH=/snap/bin/chromium node tests/e2e/frontend/archive-viewer.cjs
 *
 * Snapshot ids below are the real chain the plan's own investigation found
 * live on 2026-09-05 (`https://exxxoticaexpo.com/`, client 3XE — 4 versions)
 * and the HWELL page whose script-fidelity bug motivated §3. If the local
 * archive gets swept/re-captured and these ids age out, re-resolve them via
 * `GET /api/v1/archive/urls/{url_id}/snapshots` and update the constants.
 *
 * Assertions match the address by ID/TS PREFIX, never the full trailing URL:
 * once the replayed iframe's own shim posts its `postMessage` (real host,
 * possibly a `www.` variant the tracked `ArchiveUrl.url` doesn't carry),
 * the chrome re-points the address bar's URL SEGMENT to that live value —
 * intentional (Wayback's own convention: show what's actually displayed),
 * not a version drift. `waitUntil: "domcontentloaded"`, not `"load"`: the
 * REAL captured page's own iframe subresources (a live analytics beacon
 * under `frame-src https:`) can keep the top-level `load` event from ever
 * firing at all.
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "http://localhost:6524";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");

const EXXXOTICA_URL = "https://exxxoticaexpo.com/";
const OLDEST = { id: "a5a7b47d-8188-4e6e-90d1-9ed18d3d7f41", ts: "20260904103104" };
const MIDDLE = { id: "b1bc3eb6-5987-4c10-bc09-93ef95a1c657", ts: "20260904134046" };
const NEWER = { id: "7f434b6f-056b-451b-930f-cde69ea06901", ts: "20260904163030" };

const HWELL_URL = "https://hearwellservices.com/about-us/jessica-m-frankel/";
const HWELL_SNAPSHOT = { id: "d2704a11-d5cd-4ef9-8298-48cab43c03dd", ts: "20260904134046" };

function viewPath({ id, ts }, url) {
  return `/archive/view/snapshot/${id}/${ts}/${url}`;
}

function versionPrefix({ id, ts }) {
  return `/archive/view/snapshot/${id}/${ts}/`;
}

async function main() {
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: "reduce" });
  const page = await context.newPage();
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));

  // --- §1: the server-backed path loads directly (no hash, no JS needed --
  // to resolve WHICH version — the address alone identifies it) -------------
  await page.goto(`${baseUrl}${viewPath(OLDEST, EXXXOTICA_URL)}`, { waitUntil: "domcontentloaded" });
  await page.locator(".archive-banner__url").waitFor({ timeout: 15000 });
  assert.ok(
    new URL(page.url()).pathname.startsWith(versionPrefix(OLDEST)),
    "opens at the exact version requested",
  );

  // --- §2: Previous/Next resolve to THEMSELVES, and the banner never ------
  // drifts backward after landing (the `resolve_snapshot` off-by-one this
  // task fixes, live-confirmed pre-fix: every Next silently re-showed the
  // PREVIOUS version instead) --------------------------------------------
  await page.locator('[data-action="next"]').click();
  await page.waitForFunction(
    (prefix) => location.pathname.startsWith(prefix),
    versionPrefix(MIDDLE),
    { timeout: 15000 },
  );
  // Give any stray postMessage/adopt-snapshot round trip a moment, then
  // confirm the address STAYED put instead of bouncing back.
  await page.waitForTimeout(800);
  assert.ok(
    new URL(page.url()).pathname.startsWith(versionPrefix(MIDDLE)),
    "address did not drift backward after Next resolved",
  );

  await page.locator('[data-action="next"]').click();
  await page.waitForFunction(
    (prefix) => location.pathname.startsWith(prefix),
    versionPrefix(NEWER),
    { timeout: 15000 },
  );
  await page.waitForTimeout(800);
  assert.ok(new URL(page.url()).pathname.startsWith(versionPrefix(NEWER)));

  // --- §2/§1.3: browser Back walks versions, not routes -------------------
  await page.goBack();
  await page.waitForFunction(
    (prefix) => location.pathname.startsWith(prefix),
    versionPrefix(MIDDLE),
    { timeout: 15000 },
  );
  await page.locator(".archive-banner__url").waitFor({ timeout: 15000 });

  // --- §4: version-picker calendar in the banner --------------------------
  await page.locator('[data-action="toggle-calendar"]').click();
  await page.locator(".archive-calendar-popover .archive-calendar").waitFor({ timeout: 10000 });
  const hasCaptureDays = await page.locator(".archive-calendar__day--has-capture").count();
  assert.ok(hasCaptureDays > 0, "calendar highlights at least one day with a capture");
  await page.screenshot({ path: path.join(screenshots, "archive-viewer-calendar-1440.png") });
  await page.keyboard.press("Escape");
  await page.locator(".archive-calendar-popover").waitFor({ state: "hidden", timeout: 5000 });

  // --- §3: replay fidelity with scripts ENABLED (default) — no request ---
  // for this app's OWN origin should ever leave the replay's rewrite path
  // (the LAB.js/location.href bug this task fixes) -------------------------
  // Every legitimate replay request also goes THROUGH this app's own host
  // (`/archive/r/<ts>/<real url>`) — that alone proves nothing. What §3.1
  // actually fixes is the ARCHIVED URL EMBEDDED after that prefix pointing
  // at our own host instead of the real site's (a page's own JS resolving
  // its asset root off `location.href`, which inside the replay IS our
  // origin) — so only the tail after the `/archive/r/<ts>/` marker counts.
  const wrongOriginRequests = [];
  const replayMarker = `/archive/r/${HWELL_SNAPSHOT.ts}/`;
  const appHost = new URL(baseUrl).host;
  page.on("request", (request) => {
    const url = request.url();
    const markerIndex = url.indexOf(replayMarker);
    if (markerIndex === -1) return;
    const archivedTarget = url.slice(markerIndex + replayMarker.length);
    if (archivedTarget.includes(appHost)) wrongOriginRequests.push(url);
  });
  await page.goto(`${baseUrl}${viewPath(HWELL_SNAPSHOT, HWELL_URL)}`, { waitUntil: "domcontentloaded" });
  await page.locator(".archive-banner__url").waitFor({ timeout: 15000 });
  const frame = page.frameLocator(".archive-iframe");
  await frame.locator("body").waitFor({ timeout: 15000 });
  // Let the page's own deferred-load bootstrap fire (`setTimeout(..., 0)`)
  // and LazyLoad promote its images.
  await page.waitForTimeout(3000);
  const logoNaturalWidth = await frame.locator('img.wp-image-13937').evaluate((img) => img.naturalWidth).catch(() => 0);
  assert.ok(logoNaturalWidth > 0, `header logo actually loaded with scripts on (naturalWidth=${logoNaturalWidth})`);
  assert.deepEqual(
    wrongOriginRequests, [],
    `no request should ever target this app's own origin for a /wp-content/ path: ${wrongOriginRequests.join(", ")}`,
  );
  await page.screenshot({ path: path.join(screenshots, "archive-viewer-scripts-on-1440.png") });

  // `page.on("pageerror")` also surfaces exceptions thrown INSIDE the
  // sandboxed replay iframe — some are the ARCHIVED third-party page's own
  // script tripping over the sandbox's deliberate `allow-same-origin`
  // omission (a real site's own code calling `localStorage`, or load-order
  // quirks in a third-party bundle expecting `jQuery` globally). That is
  // the security boundary working as designed (task-59 §1.6), not a defect
  // in this app's own chrome — only an error OUTSIDE that known pattern
  // would indicate something this task broke.
  const unexpectedErrors = browserErrors.filter(
    (message) => !/allow-same-origin|jQuery is not defined/.test(message),
  );
  assert.deepEqual(unexpectedErrors, [], `no unexpected page errors: ${unexpectedErrors.join("; ")}`);

  await browser.close();
  console.log("archive-viewer.cjs: all assertions passed");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
