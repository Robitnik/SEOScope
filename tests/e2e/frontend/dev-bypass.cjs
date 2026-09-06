#!/usr/bin/env node
/* Browser smoke for task-73 (dev-only access without login).
 *
 * Requires a RUNNING api with `AUTH_LOCALHOST_BYPASS=true` set in its `.env`
 * — this file never carries the value itself.
 *
 *   BASE_URL=http://127.0.0.1:6524 node tests/e2e/frontend/dev-bypass.cjs
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:6524";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");

async function main() {
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 900, height: 700 }, reducedMotion: "reduce" });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });

  // 1. Немає cookie взагалі — і додаток одразу на екрані, не форма входу.
  await page.goto(`${baseUrl}/`, { waitUntil: "domcontentloaded" });
  await page.locator("#user-menu").waitFor({ state: "visible", timeout: 20000 });
  assert.ok(!page.url().includes("login.html"), "localhost bypass skips the sign-in form entirely");
  assert.match(await page.locator("#user-menu-name").innerText(), /Local operator/i);

  // 2. Меню користувача відкривається ПОВНІСТЮ — task-73 прибрав overflow:hidden
  //    на `.masthead`, який різав нижню частину панелі («Sign out» не було видно).
  await page.click("#user-menu-toggle");
  await page.locator("#user-menu-panel").waitFor({ state: "visible" });
  const signOut = page.locator("#user-menu-signout");
  await signOut.waitFor({ state: "visible" });
  const box = await signOut.boundingBox();
  const viewport = page.viewportSize();
  assert.ok(box.y + box.height <= viewport.height, "Sign out is fully inside the viewport, not clipped");
  await page.screenshot({ path: path.join(screenshots, "dev-bypass-dropdown-900.png"), fullPage: false });

  assert.deepEqual(errors, [], `no browser errors: ${errors.join(" | ")}`);
  await browser.close();
  console.log("dev-bypass smoke OK");
}

main().catch((error) => { console.error(error); process.exit(1); });
