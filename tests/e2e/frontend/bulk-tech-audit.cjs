#!/usr/bin/env node
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:6524";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");

async function main() {
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    reducedMotion: "reduce",
  });
  await context.addInitScript(() => localStorage.clear());
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });

  await page.goto(`${baseUrl}/?fixture=all-states`, { waitUntil: "networkidle" });
  await page.locator(".client-card").first().waitFor();
  assert.equal(await page.locator(".tech-audit-strip").count(), 4);
  assert.equal(await page.getByText("7 open errors", { exact: true }).count(), 1);
  assert.equal(await page.getByText("12+ errors found", { exact: true }).count(), 1);
  assert.equal(await page.getByText("Not audited", { exact: true }).count(), 1);
  await page.screenshot({ path: path.join(screenshots, "bulk-audit-cards-1440.png"), fullPage: true });

  await page.locator("#audit-all-button").click();
  const dialog = page.locator("#sync-dialog");
  assert.equal(await dialog.evaluate((node) => node.open), true);
  assert.equal(await page.locator('input[name="audit-client"]:checked').count(), 4);
  assert.equal(await page.locator(".audit-client-row__current.has-errors").count(), 2);
  await page.getByRole("button", { name: "Not audited", exact: true }).click();
  assert.equal(await page.locator('input[name="audit-client"]:checked').count(), 1);
  await page.getByRole("button", { name: "Needs refresh", exact: true }).click();
  assert.equal(await page.locator('input[name="audit-client"]:checked').count(), 2);
  await page.getByRole("button", { name: "All active", exact: true }).click();
  await page.screenshot({ path: path.join(screenshots, "bulk-audit-selection-1440.png"), fullPage: false });

  await dialog.getByRole("button", { name: "Run audits", exact: true }).click();
  await page.locator(".audit-run-row").first().waitFor();
  await page.locator('.audit-run-row[data-status="running"]').first().waitFor({ timeout: 5000 });
  assert.equal(await page.locator(".audit-run-row").count(), 4);
  assert.equal(await page.locator('.audit-run-row[data-status="running"]').count(), 1);
  assert.equal(await page.getByRole("button", { name: "Stop", exact: true }).count(), 1);
  await page.screenshot({ path: path.join(screenshots, "bulk-audit-progress-1440.png"), fullPage: false });

  await page.getByRole("button", { name: "Hide", exact: true }).click();
  assert.equal(await page.locator("#job-dock").isVisible(), true);
  await page.locator("#job-dock-button").click();
  assert.equal(await dialog.evaluate((node) => node.open), true);
  await page.keyboard.press("Escape");
  assert.equal(await page.locator("#job-dock-button").evaluate((node) => document.activeElement === node), true);

  await page.locator("#job-dock-button").click();
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(
    await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth),
    true,
  );
  await page.screenshot({ path: path.join(screenshots, "bulk-audit-progress-390.png"), fullPage: false });

  assert.deepEqual(errors, [], `browser errors: ${errors.join("; ")}`);
  await browser.close();
  console.log("bulk technical audit e2e: ok");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
