#!/usr/bin/env node
/* Browser smoke for the MCP catalog page (task-77).
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
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, reducedMotion: "reduce" });
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

  // 2. All 12 wave-1 tools render, grouped, from the LIVE catalog.
  await page.locator(".mcp-tool-card").first().waitFor({ timeout: 20000 });
  assert.equal(await page.locator(".mcp-tool-card").count(), 12, "renders all 12 wave-1 tools");
  assert.equal(await page.locator(".mcp-badge--write").count(), 1, "exactly one tool is marked write");
  assert.match(
    await page.locator(".mcp-badge--write").locator("xpath=ancestor::article").locator(".mcp-tool-card__name").innerText(),
    /run_client_report/,
    "the one write tool is run_client_report"
  );

  // 3. Search narrows the grid and updates the live count.
  await page.locator("#mcp-tool-search").fill("archive");
  await page.waitForTimeout(50);
  const visibleAfterSearch = await page.locator(".mcp-tool-card:visible").count();
  assert.ok(visibleAfterSearch > 0 && visibleAfterSearch < 12, "search narrows the tool grid");
  await page.locator("#mcp-tool-search").fill("");

  // 4. Connect snippet copies to the clipboard.
  await page.click('.mcp-tab[data-tab="0"]');
  await page.click('.mcp-copy[data-copy-target="connect-0"]');
  await page.waitForTimeout(100);
  const clipboard = await page.evaluate(() => navigator.clipboard.readText());
  assert.match(clipboard, /mcpServers/, "the connect snippet copies real config JSON");
  assert.ok(!clipboard.includes("<your-token>") || clipboard.includes("MCP_TOKEN"), "snippet mentions the token placeholder, not a real secret");

  // 5. Prompts render with placeholders, not real client data.
  const promptText = await page.locator(".mcp-prompt-card pre").first().innerText();
  assert.match(promptText, /\{\{/, "prompt text still carries a placeholder, not a filled-in example");

  await page.screenshot({ path: path.join(screenshots, "mcp-page-1440.png"), fullPage: true });

  // 6. No horizontal scroll INTRODUCED BY THIS PAGE at a narrow viewport.
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
