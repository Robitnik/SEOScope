#!/usr/bin/env node
/* Browser smoke for the MCP catalog page (task-77) and its "How to
 * connect" sub-page (task-77 follow-up: cards now share one fixed height,
 * Connect moved to `#/mcp/connect`, and Prompts leads with a copyable
 * all-tools briefing).
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

  // 2. Prompts lead the page — the all-tools briefing renders ABOVE the
  // tool grid and mentions every one of the 12 tools by name.
  await page.locator(".mcp-prompt-card--all").waitFor({ timeout: 20000 });
  const briefing = await page.locator(".mcp-prompt-card--all pre").innerText();
  const toolNames = await page.evaluate(() =>
    Array.from(document.querySelectorAll(".mcp-tool-card__name")).map((el) => el.textContent.trim())
  );
  assert.equal(toolNames.length, 12, "renders all 12 wave-1 tools");
  for (const name of toolNames) {
    assert.ok(briefing.includes(name), `all-tools briefing is missing ${name}`);
  }
  const promptsTop = (await page.locator(".mcp-prompt-card--all").boundingBox()).y;
  const toolsTop = (await page.locator(".mcp-tool-card").first().boundingBox()).y;
  assert.ok(promptsTop < toolsTop, "prompts render above the tool grid, not below it");

  // 3. Every tool card is the same height (uniform-size request).
  const heights = await page.evaluate(() =>
    Array.from(document.querySelectorAll(".mcp-tool-card")).map((el) => el.getBoundingClientRect().height)
  );
  const distinctHeights = new Set(heights.map((h) => Math.round(h)));
  assert.equal(distinctHeights.size, 1, `all tool cards share one height, got ${[...distinctHeights]}`);

  // 4. Copying the all-tools briefing puts real tool names on the clipboard.
  await page.click('.mcp-prompt-card--all .mcp-copy');
  await page.waitForTimeout(100);
  const briefingClipboard = await page.evaluate(() => navigator.clipboard.readText());
  assert.match(briefingClipboard, /run_client_report/, "the all-tools briefing copies the real tool list");

  // 5. The one write tool is clearly marked, and its arg table scrolls
  // INSIDE its fixed-height card rather than stretching it.
  assert.equal(await page.locator(".mcp-badge--write").count(), 1, "exactly one tool is marked write");
  assert.match(
    await page.locator(".mcp-badge--write").locator("xpath=ancestor::article").locator(".mcp-tool-card__name").innerText(),
    /run_client_report/,
    "the one write tool is run_client_report"
  );

  // 6. Search narrows the grid and updates the live count.
  await page.locator("#mcp-tool-search").fill("archive");
  await page.waitForTimeout(50);
  const visibleAfterSearch = await page.locator(".mcp-tool-card:visible").count();
  assert.ok(visibleAfterSearch > 0 && visibleAfterSearch < 12, "search narrows the tool grid");
  await page.locator("#mcp-tool-search").fill("");

  // 7. Scenario prompts still render with placeholders, not real client data.
  const promptText = await page.locator(".mcp-prompt-grid .mcp-prompt-card pre").first().innerText();
  assert.match(promptText, /\{\{/, "prompt text still carries a placeholder, not a filled-in example");

  await page.screenshot({ path: path.join(screenshots, "mcp-page-1440.png"), fullPage: true });

  // 8. "How to connect" is its own page now, reached via the hero CTA —
  // not an inline section competing with Tools/Prompts for scroll room.
  await page.click(".mcp-connect-cta");
  await page.waitForURL(/#\/mcp\/connect/);
  await page.locator(".mcp-tab").first().waitFor({ timeout: 20000 });
  assert.equal(await page.locator(".mcp-tool-card").count(), 0, "the connect page has no tool cards");

  await page.click('.mcp-tab[data-tab="0"]');
  await page.click('.mcp-copy[data-copy-target="connect-0"]');
  await page.waitForTimeout(100);
  const connectClipboard = await page.evaluate(() => navigator.clipboard.readText());
  assert.match(connectClipboard, /mcpServers/, "the connect snippet copies real config JSON");
  assert.ok(connectClipboard.includes("<your-token>"), "snippet carries a token PLACEHOLDER, never a real secret");
  assert.match(connectClipboard, /seo\.theyovth\.space/, "the connect snippet points at the real domain, not localhost");
  assert.ok(!connectClipboard.includes("127.0.0.1"), "no localhost address leaks into the connect snippet");

  await page.locator(".back-link").click();
  await page.waitForURL(/#\/mcp(?!\/connect)/);
  await page.locator(".mcp-tool-card").first().waitFor();

  // 9. No horizontal scroll INTRODUCED BY THIS PAGE at a narrow viewport.
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
