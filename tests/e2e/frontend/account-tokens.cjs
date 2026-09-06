#!/usr/bin/env node
/* Browser smoke for the personal-tokens cabinet (task-79) — `#/account`.
 *
 * Two things make this file different from every other e2e script here.
 *
 * 1. It CANNOT run against `AUTH_LOCALHOST_BYPASS`: that identity is
 *    `user_id="local"`, a pseudo-user with no row in `users`, and personal
 *    tokens are by definition owned by a real account. Any request from
 *    `127.0.0.1` resolves to that bypass FIRST, before any cookie session
 *    or bearer token is even checked (`app/core/auth/middleware.py`'s
 *    order), so a real login on localhost is invisible to this feature.
 *    This script creates its own disposable, clearly-labeled account via
 *    the app's own CLI, logs in as it over the PUBLIC domain (where the
 *    bypass structurally does not apply), and deactivates the account
 *    again in `finally` — a normal deactivated row, never a deleted one
 *    (task-71's own invariant).
 *
 * 2. The token's actual authenticate/de-authenticate CONTRACT checks go
 *    straight to the ORIGIN over plain HTTP, not through the browser.
 *    Found live: the public domain sits behind Cloudflare, and a GET to
 *    this exact URL kept coming back 200 for a bearer token that had
 *    ALREADY been revoked seconds earlier (confirmed by reading
 *    `revoked_at` in Postgres directly) — a unique cache-busting query
 *    string and `cache: "no-store"` on the fetch did not help, meaning
 *    the edge is caching by a key that ignores both (same class of issue
 *    this project already hit for static assets — the CDN layer can
 *    outrank origin headers entirely). That is an infrastructure fix
 *    (a Cache Rule for `/api/*`), not an application bug: the instant-
 *    revocation guarantee is already proven with zero flakiness at the
 *    unit and API level (tests/unit/auth/test_personal_tokens.py,
 *    tests/unit/api/test_me_tokens_api.py — no CDN in that path at all).
 *    So here, the UI steps (login, create, revoke) still go through the
 *    real public domain like a real user, but the PROOF that the token
 *    works/stops-working reads the origin directly — with the same
 *    `X-Forwarded-For` spoof used for manual verification, to defeat the
 *    localhost bypass so the check exercises real bearer resolution.
 *
 *   BASE_URL=https://seo.theyovth.space ORIGIN_URL=http://127.0.0.1:6524 \
 *   node tests/e2e/frontend/account-tokens.cjs
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { execSync } = require("node:child_process");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "https://seo.theyovth.space";
const originUrl = process.env.ORIGIN_URL || "http://127.0.0.1:6524";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");
const email = `qa-e2e-account-tokens-${Date.now()}@theyovth.space`;

function cli(command) {
  return execSync(`docker compose exec -T api poetry run yovth-report ${command}`, {
    cwd: path.join(__dirname, "..", "..", ".."),
    encoding: "utf8",
  });
}

// Straight to the origin, bypassing the CDN, with a proxy-marker header so
// `is_trusted_loopback()` (task-73) does NOT treat this as the trusted
// localhost operator — exercising the real bearer-token code path.
async function checkTokenStatus(token) {
  const response = await fetch(`${originUrl}/api/v1/me/tokens`, {
    headers: { Authorization: `Bearer ${token}`, "X-Forwarded-For": "203.0.113.1" },
  });
  return response.status;
}

async function main() {
  const created = cli(
    `user-create --email "${email}" --name "QA E2E account-tokens" --role admin`
  );
  const password = created.match(/password \(shown once, store it now\): (\S+)/)?.[1];
  assert.ok(password, `couldn't parse a password out of user-create's output:\n${created}`);

  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    // Cloudflare's own analytics beacon trips this app's strict CSP —
    // pre-existing, unrelated to this page, not worth failing the run over.
    if (message.type() === "error" && !message.text().includes("cloudflareinsights")) {
      errors.push(message.text());
    }
  });

  try {
    // 1. Real login (not the bypass) — the only way to reach a genuine
    // per-user identity from a headless browser against this stack.
    await page.goto(`${baseUrl}/login.html`, { waitUntil: "domcontentloaded" });
    await page.fill("#email, input[type=email]", email);
    await page.fill("#password, input[type=password]", password);
    await page.click("button[type=submit]");
    await page.locator("#user-menu").waitFor({ state: "visible", timeout: 20000 });

    // 2. Reached from the user menu — "Your account" sits above the
    // admin-only links, visible to every role.
    await page.click("#user-menu-toggle");
    await page.locator("#user-menu-account").waitFor({ state: "visible" });
    await page.click("#user-menu-account");
    await page.locator("#account-token-form").waitFor({ timeout: 20000 });

    // 3. The hero shows the REAL signed-in identity, not a stale/empty one
    // (found live: a session.js import under the wrong `?v=` tag gave a
    // second, never-populated module instance — hero showed "Email —").
    const heroText = await page.locator(".account-intro").innerText();
    assert.match(heroText, new RegExp(email), "hero shows the real signed-in email");
    assert.match(heroText, /Administrator/, "hero shows the real role");

    // 4. Create a token — full value shown exactly once.
    await page.fill("#account-token-label", "e2e-claude-code");
    await page.click("#account-token-form button[type=submit]");
    await page.locator("#account-secret-value").waitFor({ timeout: 10000 });
    const secret = await page.locator("#account-secret-value").innerText();
    assert.match(secret, /^yvth_/, "the raw value carries the yvth_ prefix");

    // 5. The token actually authenticates against the real API (checked
    // at the origin — see the file header for why not through the CDN).
    assert.equal(await checkTokenStatus(secret), 200, "the freshly created token authenticates");

    // 6. Copy button puts the same value on the clipboard.
    await page.click("#account-secret-copy");
    await page.waitForTimeout(150);
    const clipboard = await page.evaluate(() => navigator.clipboard.readText());
    assert.equal(clipboard, secret, "Copy puts the exact same value on the clipboard");
    await page.click("#account-secret-dismiss");

    await page.screenshot({ path: path.join(screenshots, "account-page-1440.png"), fullPage: true });

    // 7. The list shows only the prefix — never the full value again.
    const listingText = await page.locator("#account-tokens-table").innerText();
    assert.ok(!listingText.includes(secret), "the listing never repeats the full value");
    assert.equal(await page.locator("#account-tokens-table tbody tr").count(), 1);

    // 8. Revoke — confirmation dialog, then a visible "Revoked" badge.
    page.once("dialog", (dialog) => dialog.accept());
    await page.click(".account-revoke");
    await page.locator(".account-badge--revoked").waitFor({ timeout: 10000 });

    // 9. Revoked immediately — same direct-to-origin check as step 5.
    assert.equal(
      await checkTokenStatus(secret),
      401,
      "a revoked token is rejected immediately, not just hidden in the UI"
    );

    // 10. No horizontal scroll INTRODUCED BY THIS PAGE at a narrow viewport
    // (the app shell itself already has a fixed ~39px baseline overflow —
    // see mcp-page.cjs for the same measured baseline on another page).
    await page.setViewportSize({ width: 390, height: 900 });
    await page.waitForTimeout(100);
    const overflowPx = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    assert.ok(overflowPx <= 40, `page doesn't widen the shell's existing overflow further (got ${overflowPx}px)`);
    await page.screenshot({ path: path.join(screenshots, "account-page-390.png"), fullPage: true });

    assert.deepEqual(errors, [], `no browser errors: ${errors.join(" | ")}`);
    console.log("account-tokens smoke OK");
  } finally {
    await browser.close();
    // Deactivate, never delete (task-71 invariant) — leaves a normal,
    // clearly-labeled inactive row behind, same as any offboarded person.
    cli(`user-deactivate --email "${email}"`);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
