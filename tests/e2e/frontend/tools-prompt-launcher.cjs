/* Browser smoke for the Prompt Launcher tool (task-94) — real backend, real
 * client data (no fixture mode: the whole point is the real weekly
 * TechAudit client list, there is nothing meaningful to fake locally).
 *
 * Hub card -> template selector -> table of real clients -> Copy puts the
 * exact rendered launcher on the clipboard -> "copied" timestamp appears and
 * survives a reload -> Reset clears it -> a client with no domain shows a
 * disabled Copy button -> the paused/hidden group is collapsed by default ->
 * Prompt source panel lazy-loads the real file -> no horizontal scroll at
 * 390px.
 *
 * Invocation:
 *   NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 \
 *   node tests/e2e/frontend/tools-prompt-launcher.cjs
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
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });

  // 1. The hub lists the tool and finds it by keyword.
  await page.goto(`${baseUrl}/#/tools`, { waitUntil: "networkidle" });
  await page.locator(".tool-card").first().waitFor();
  await page.locator("#tools-search").fill("prompt");
  assert.equal(await page.locator(".tool-card").count(), 1, "the hub finds the tool by keyword");
  await page.locator(".tool-card").click();
  await page.locator("#pl-template-select").waitFor();

  // 2. The template is selected, the doc hint reports a mounted file, and
  // the summary is not blank.
  assert.equal(await page.locator("#pl-template-select").inputValue(), "techaudit");
  await page.locator("#pl-doc-hint code").waitFor();
  assert.match(await page.locator("#pl-doc-hint").innerText(), /TechAudit\.md/);
  assert.ok((await page.locator("#pl-summary").innerText()).length > 10, "summary is not blank");

  // 3. Real clients render with real CLIENT/URL columns.
  await page.locator(".pl-table tbody tr").first().waitFor({ timeout: 15_000 });
  const rowCount = await page.locator(".pl-table tbody tr").count();
  assert.ok(rowCount > 0, "at least one real client renders");
  await page.screenshot({ path: path.join(screenshots, "tools-prompt-launcher-1440.png"), fullPage: true });

  // 4. A blocked row (no domain, if one exists in the live data) shows a
  // disabled Copy button with a reason in its title — never a copyable
  // launcher with an empty URL. Not asserted as a hard requirement (the
  // live client list may have no such row today), only checked when present.
  const disabledCopy = page.locator(".pl-copy-btn[disabled]").first();
  if (await disabledCopy.count()) {
    const title = await disabledCopy.getAttribute("title");
    assert.match(title, /no domain/i, "a blocked row explains why via title");
  }

  // 5. Copy puts the EXACT rendered launcher on the clipboard, the button
  // flashes, and the row's "Copied" cell + the header counter update.
  //
  // The row is addressed by its stable `data-client-id`, never re-derived
  // as ".first() enabled button" after clicking: `flashCopied` disables the
  // clicked button for 1.6s, so ":not([disabled])" re-resolves to the NEXT
  // row the instant it is queried again — a stale-locator trap this test
  // hit live before this comment was added.
  const firstEnabledCopy = page.locator(".pl-copy-btn:not([disabled])").first();
  await firstEnabledCopy.waitFor();
  const clientId = await firstEnabledCopy.getAttribute("data-copy-client");
  const row = page.locator(`tr[data-client-id="${clientId}"]`);
  const beforeCounter = await page.locator("#pl-copy-count").innerText();
  await firstEnabledCopy.click();
  await row.locator(".pl-copy-btn:has-text('Copied')").waitFor();
  const clipboardText = await page.evaluate(() => navigator.clipboard.readText());
  assert.match(clipboardText, /^Read \/.*docs\/TechAudit\.md and run it end to end\.\n\nMODE:\s+tech\nCLIENT:\s+.+\nURL:\s+https?:\/\/\S+\n$/,
    "the clipboard holds the exact launcher shape, not a hand-assembled string");
  assert.notEqual(await row.locator(".pl-copied-cell").innerText(), "—", "the row's Copied cell leaves its dash placeholder");
  const afterCounter = await page.locator("#pl-copy-count").innerText();
  assert.notEqual(afterCounter, beforeCounter, "the header counter advances after a copy");
  assert.match(afterCounter, /^\d+ of \d+ copied this week$/);

  // 6. The mark survives a reload (localStorage), not just the in-memory row.
  await page.reload({ waitUntil: "networkidle" });
  await page.locator(".pl-table tbody tr").first().waitFor({ timeout: 15_000 });
  const reloadedRow = page.locator(`tr[data-client-id="${clientId}"]`);
  assert.notEqual(await reloadedRow.locator(".pl-copied-cell").innerText(), "—",
    "the copied mark survives a reload");
  assert.equal(await page.locator("#pl-copy-count").innerText(), afterCounter,
    "the counter matches what it was before reload");

  // 7. Reset clears the week's marks.
  await page.locator("#pl-reset").click();
  assert.equal(await page.locator("#pl-copy-count").innerText(), "0 of " + afterCounter.match(/of (\d+)/)[1] + " copied this week");
  assert.equal(await reloadedRow.locator(".pl-copied-cell").innerText(), "—", "Reset clears the Copied cell");

  // 8. The paused/hidden group (if the live data has one) is collapsed by
  // default and expands on click, never mixed into the primary table.
  const secondaryToggle = page.locator("#pl-secondary-toggle");
  if (await secondaryToggle.count()) {
    assert.equal(await page.locator("#pl-secondary-wrap").isHidden(), true, "secondary group starts collapsed");
    await secondaryToggle.click();
    assert.equal(await page.locator("#pl-secondary-wrap").isHidden(), false, "secondary group expands on click");
    assert.equal(await secondaryToggle.getAttribute("aria-expanded"), "true");
  }

  // 9. Prompt source lazy-loads the real file only once it is opened.
  assert.equal(await page.locator("#pl-source-body pre").count(), 0, "source is not fetched before opening");
  await page.locator("#pl-source summary").click();
  await page.locator("#pl-source-body pre").waitFor({ timeout: 10_000 });
  assert.match(await page.locator("#pl-source-body pre").innerText(), /TechAudit/);

  // 10. Mobile: the tool's own content does not force horizontal scroll.
  const mobile = await browser.newContext({ viewport: { width: 390, height: 900 }, reducedMotion: "reduce" });
  const mobilePage = await mobile.newPage();
  await mobilePage.goto(`${baseUrl}/#/tools/prompt-launcher`, { waitUntil: "networkidle" });
  await mobilePage.locator(".pl-table tbody tr").first().waitFor({ timeout: 15_000 });
  const { scrollWidth, clientWidth } = await mobilePage.evaluate(() => {
    const el = document.querySelector("#tools-page-root");
    return { scrollWidth: el.scrollWidth, clientWidth: el.clientWidth };
  });
  assert.ok(scrollWidth <= clientWidth, `no horizontal scroll inside the tool at 390px (${scrollWidth} > ${clientWidth})`);
  await mobilePage.screenshot({ path: path.join(screenshots, "tools-prompt-launcher-390.png"), fullPage: true });
  await mobile.close();

  assert.deepEqual(browserErrors, [], "no console or page errors");
  await context.close();
  await browser.close();
  console.log("tools-prompt-launcher.cjs: all assertions passed");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
