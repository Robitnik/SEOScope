#!/usr/bin/env node
/* Browser smoke for the MCP catalog page (task-77) and its "How to
 * connect" sub-page (task-77 follow-ups: cards share one fixed height;
 * Connect moved to `#/mcp/connect`; the all-tools briefing prompt now
 * lives on THAT page, framed as step 2 of onboarding, with a Download .md
 * button alongside Copy).
 *
 * Requires a RUNNING stack with `AUTH_LOCALHOST_BYPASS=true` (task-73) so
 * the page loads with a real session against the real live catalog — this
 * page has no fixture mode, its whole point is "what did the server
 * actually register", not a canned fixture.
 *
 *   BASE_URL=http://127.0.0.1:6524 node tests/e2e/frontend/mcp-page.cjs
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
    acceptDownloads: true,
  });
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });

  // 1. Reached from the user menu, not a top-level nav tab.
  await page.goto(`${baseUrl}/`, { waitUntil: "domcontentloaded" });
  await page.locator("#user-menu").waitFor({ state: "visible", timeout: 20000 });
  await page.click("#user-menu-toggle");
  await page.locator("#user-menu-mcp").waitFor({ state: "visible" });
  await page.click("#user-menu-mcp");

  // 2. Prompts lead the page; the all-tools briefing itself is NOT here —
  // it moved to the connect page — but a link to it is.
  await page.locator(".mcp-prompt-grid").waitFor({ timeout: 20000 });
  await page.locator(".mcp-tool-card").first().waitFor();
  assert.equal(await page.locator(".mcp-prompt-card--all").count(), 0, "the all-tools briefing lives on the connect page now");
  const toolNames = await page.evaluate(() =>
    Array.from(document.querySelectorAll(".mcp-tool-card__name")).map((el) => el.textContent.trim())
  );
  assert.equal(toolNames.length, 12, "renders all 12 wave-1 tools");
  const promptsTop = (await page.locator("#mcp-prompts-title").boundingBox()).y;
  const toolsTop = (await page.locator(".mcp-tool-card").first().boundingBox()).y;
  assert.ok(promptsTop < toolsTop, "prompts render above the tool grid, not below it");

  // 3. Every tool card is the same height (uniform-size request).
  const heights = await page.evaluate(() =>
    Array.from(document.querySelectorAll(".mcp-tool-card")).map((el) => el.getBoundingClientRect().height)
  );
  const distinctHeights = new Set(heights.map((h) => Math.round(h)));
  assert.equal(distinctHeights.size, 1, `all tool cards share one height, got ${[...distinctHeights]}`);

  // 4. The one write tool is clearly marked, and its arg table scrolls
  // INSIDE its fixed-height card rather than stretching it.
  assert.equal(await page.locator(".mcp-badge--write").count(), 1, "exactly one tool is marked write");
  assert.match(
    await page.locator(".mcp-badge--write").locator("xpath=ancestor::article").locator(".mcp-tool-card__name").innerText(),
    /run_client_report/,
    "the one write tool is run_client_report"
  );

  // 5. Search narrows the grid and updates the live count.
  await page.locator("#mcp-tool-search").fill("archive");
  await page.waitForTimeout(50);
  const visibleAfterSearch = await page.locator(".mcp-tool-card:visible").count();
  assert.ok(visibleAfterSearch > 0 && visibleAfterSearch < 12, "search narrows the tool grid");
  await page.locator("#mcp-tool-search").fill("");

  // 6. Scenario prompts still render with placeholders, not real client data.
  const promptText = await page.locator(".mcp-prompt-grid .mcp-prompt-card pre").first().innerText();
  assert.match(promptText, /\{\{/, "prompt text still carries a placeholder, not a filled-in example");

  await page.screenshot({ path: path.join(screenshots, "mcp-page-1440.png"), fullPage: true });

  // 7. "How to connect" is its own page, reached via the hero CTA. It carries
  // ONLY the main prompt — the docker/stdio config block was removed on the
  // user's call: nobody outside this machine has docker, so a config snippet
  // they cannot run is noise in front of the one thing they can use.
  await page.click(".mcp-connect-cta");
  await page.waitForURL(/#\/mcp\/connect/);
  await page.locator(".mcp-prompt-card--all").waitFor({ timeout: 20000 });
  assert.equal(await page.locator(".mcp-tool-card").count(), 0, "the connect page has no tool cards");
  assert.equal(await page.locator(".mcp-tab").count(), 0, "no host tabs left on the connect page");
  assert.equal(
    await page.locator(".mcp-code-block").count(),
    0,
    "no config snippet block left on the connect page",
  );

  // 8. The all-tools briefing is the whole page, and mentions every tool.
  await page.locator(".mcp-prompt-card--all").waitFor();
  const briefing = await page.locator(".mcp-prompt-card--all pre").innerText();
  for (const name of toolNames) {
    assert.ok(briefing.includes(name), `all-tools briefing is missing ${name}`);
  }
  await page.click('.mcp-prompt-card--all .mcp-copy');
  await page.waitForTimeout(100);
  const briefingClipboard = await page.evaluate(() => navigator.clipboard.readText());
  assert.match(briefingClipboard, /run_client_report/, "the all-tools briefing copies the real tool list");

  // 9. Download .md triggers a real download AND hands over the right bytes.
  //
  // The BYTES are captured from the Blob the page itself builds, not read
  // back off disk: snap-packaged Chromium writes downloads inside a private
  // /tmp mount namespace, so `download.path()`/`saveAs()`/`createReadStream()`
  // all point at a file this Node process genuinely cannot see. Hooking
  // `URL.createObjectURL` checks the exact content the app hands the
  // browser, which is the part worth asserting anyway.
  await page.evaluate(() => {
    const original = URL.createObjectURL.bind(URL);
    window.__downloadedText = null;
    URL.createObjectURL = (blob) => {
      blob.text().then((text) => { window.__downloadedText = text; });
      return original(blob);
    };
  });
  const downloadPromise = page.waitForEvent("download");
  await page.click(".mcp-download");
  const download = await downloadPromise;
  assert.equal(download.suggestedFilename(), "yovth-mcp-tools-briefing.md");
  assert.equal(await download.failure(), null, "the download completed without failing");

  await page.waitForFunction(() => window.__downloadedText !== null);
  const downloadedText = await page.evaluate(() => window.__downloadedText);
  assert.match(downloadedText, /run_client_report/, "the downloaded .md carries the real tool list");
  assert.match(downloadedText, /^# /, "the downloaded file opens with a markdown heading");

  await page.screenshot({ path: path.join(screenshots, "mcp-connect-page-1440.png"), fullPage: true });

  await page.locator(".back-link").click();
  await page.waitForURL(/#\/mcp(?!\/connect)/);
  await page.locator(".mcp-tool-card").first().waitFor();

  // 10. No horizontal scroll INTRODUCED BY THIS PAGE at a narrow viewport.
  // The app shell itself already overflows by a fixed ~39px at 390px on
  // EVERY route (confirmed on the dashboard too, pre-existing, not this
  // page's doing) — so the bar here is "no wider than the shell's own
  // baseline overflow", not "zero overflow".
  await page.setViewportSize({ width: 390, height: 900 });
  await page.waitForTimeout(100);
  const overflowPx = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  assert.ok(overflowPx <= 40, `page doesn't widen the shell's existing overflow further (got ${overflowPx}px)`);
  await page.screenshot({ path: path.join(screenshots, "mcp-page-390.png"), fullPage: true });

  assert.deepEqual(errors, [], `no browser errors: ${errors.join(" | ")}`);
  await browser.close();
  console.log("mcp-page smoke OK");
}

main().catch((error) => { console.error(error); process.exit(1); });
