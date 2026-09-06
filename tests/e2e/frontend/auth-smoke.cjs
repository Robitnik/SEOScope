#!/usr/bin/env node
/* Browser smoke for task-71 (auth, roles, admin page).
 *
 * Requires a RUNNING api with `auth_enabled=true` and two accounts, whose
 * credentials come from the environment — this file must never carry them.
 *
 *   BASE_URL=http://127.0.0.1:6524 \
 *   ADMIN_EMAIL=… ADMIN_PASSWORD=… VIEWER_EMAIL=… VIEWER_PASSWORD=… \
 *   NODE_PATH=/tmp/yovth-e2e/node_modules node tests/e2e/frontend/auth-smoke.cjs
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:6524";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");

async function signIn(page, email, password) {
  await page.goto(`${baseUrl}/login.html`, { waitUntil: "networkidle" });
  await page.fill("#login-email", email);
  await page.fill("#login-password", password);
  await Promise.all([
    page.waitForURL((url) => !url.pathname.endsWith("login.html"), { timeout: 20000 }),
    page.click("#login-submit"),
  ]);
}

async function main() {
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  // 401 тут — очікувана частина сценарію (навмисно невірний пароль, вихід із
  // сесії): браузер логує кожну таку відповідь як console error. Ловимо все
  // інше — саме воно означає справжню поламку.
  page.on("console", (message) => {
    const text = message.text();
    if (message.type() === "error" && !/401 \(Unauthorized\)/.test(text)) errors.push(text);
  });

  // 1. Неавторизований відвідувач ніколи не бачить додатка.
  await page.goto(`${baseUrl}/`, { waitUntil: "networkidle" });
  assert.match(page.url(), /\/login\.html\?next=/, "an anonymous visit lands on the sign-in page");
  await page.screenshot({ path: path.join(screenshots, "auth-login-1440.png"), fullPage: true });

  // 2. Невірний пароль пояснює, що сталося, і не пускає далі.
  await page.fill("#login-email", "nobody@theyovth.com");
  await page.fill("#login-password", "definitely-wrong");
  await page.click("#login-submit");
  await page.locator("#login-error").waitFor({ state: "visible" });
  assert.match(await page.locator("#login-error").innerText(), /Invalid email or password/i);

  // 3. Адмін заходить, бачить своє меню й пункт People.
  await signIn(page, process.env.ADMIN_EMAIL, process.env.ADMIN_PASSWORD);
  await page.locator("#user-menu").waitFor({ state: "visible" });
  assert.equal(await page.locator('[data-nav="admin"]').isVisible(), true, "admin sees the People tab");
  await page.click("#user-menu-toggle");
  assert.match(await page.locator("#user-menu-role").innerText(), /Administrator/i);
  await page.keyboard.press("Escape");

  // 4. Сторінка People малює таблицю акаунтів.
  await page.goto(`${baseUrl}/#/admin/users`, { waitUntil: "networkidle" });
  await page.locator(".admin-table").waitFor();
  assert.ok((await page.locator(".admin-table tbody tr").count()) >= 1, "the account table has rows");
  await page.screenshot({ path: path.join(screenshots, "auth-admin-users-1440.png"), fullPage: true });

  // 5. Вихід повертає на форму входу, а захищена сторінка більше не відкривається.
  await page.click("#user-menu-toggle");
  await page.click("#user-menu-signout");
  await page.waitForURL(/login\.html/, { timeout: 20000 });
  await page.goto(`${baseUrl}/#/admin/users`, { waitUntil: "networkidle" });
  assert.match(page.url(), /login\.html/, "a signed-out session cannot reopen the app");

  // 6. Read-only роль: додаток працює, адмінки немає, глобальні дії вимкнені.
  if (process.env.VIEWER_EMAIL) {
    await signIn(page, process.env.VIEWER_EMAIL, process.env.VIEWER_PASSWORD);
    await page.locator("#user-menu").waitFor({ state: "visible" });
    assert.equal(await page.locator('[data-nav="admin"]').isVisible(), false, "viewer never sees the People tab");
    await page.click("#user-menu-toggle");
    assert.match(await page.locator("#user-menu-role").innerText(), /Read-only/i);
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#sync-all-button").isDisabled(), true, "viewer cannot start collection");
    // Даємо дашборду домалюватись, щоб знімок був сторінкою з даними, а не
    // скелетоном.
    await page.locator(".client-card").first().waitFor({ timeout: 30000 });
    await page.screenshot({ path: path.join(screenshots, "auth-viewer-dashboard-1440.png"), fullPage: true });
  }

  assert.deepEqual(errors, [], `no browser errors: ${errors.join(" | ")}`);
  await browser.close();
  console.log("auth smoke OK");
}

main().catch((error) => { console.error(error); process.exit(1); });
