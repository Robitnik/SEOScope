/* Browser smoke for task-44 (Trends: conversion metrics + bucket-truth fixes).
 * Same invocation convention as the other suites in this folder:
 *   NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 \
 *   node tests/e2e/frontend/trends.cjs
 *
 * Fixture mode only — no provider unit is ever spent here. Covers: the three
 * new conversion metrics render and total correctly; the METRIC control is
 * split into labeled Search Console/GA4 groups without horizontal overflow;
 * a zero-baseline client is excluded from `Indexed` (named in a note, not
 * drawn as an empty line); a 0 -> N table transition shows `+N`, not `—`;
 * and the picker tells `no data yet` apart from `not connected`.
 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require("playwright-core");

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:6524";
const executablePath = process.env.CHROMIUM_PATH || "/snap/bin/chromium";
const screenshots = path.join(__dirname, "screenshots");
const VIEWPORTS = [[390, 844], [768, 900], [1440, 1000]];

async function openTrends(context, fixture, hashParams = "") {
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  await page.goto(`${baseUrl}/?fixture=${fixture}#/trends${hashParams}`, { waitUntil: "networkidle" });
  await page.locator('#tr-content[aria-busy="false"]').waitFor();
  return { page, errors };
}

async function assertNoOverflow(page, label) {
  for (const [width, height] of VIEWPORTS) {
    await page.setViewportSize({ width, height });
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth),
      true,
      `no page overflow at ${width}px on ${label}`,
    );
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
}

async function main() {
  const browser = await chromium.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: "reduce" });

  const { page, errors } = await openTrends(context, "trends-normal");

  // --- metric control: grouped, labeled, no horizontal overflow --------------
  // `.trends-metric-group__label` renders `text-transform: uppercase`;
  // `innerText()` reflects the rendered (CSS-transformed) text, not the DOM's.
  const groupLabels = await page.locator(".trends-metric-group__label").allInnerTexts();
  assert.deepEqual(groupLabels, ["SEARCH CONSOLE", "GA4"], "metric control is split into two labeled groups");
  assert.equal(await page.locator("#tr-metric button").count(), 7, "all 7 metrics are selectable");
  await assertNoOverflow(page, "default view, 7 metric buttons");

  // --- Total Conversions: no backfill required, plain read of ga4_daily ------
  await page.locator('#tr-metric button[data-metric="rn_total_conversions"]').click();
  await page.locator('#tr-content[aria-busy="false"]').waitFor();
  await page.locator(".trends-table tbody tr").first().waitFor();
  assert.match(
    await page.locator(".trends-chart-section").innerText(),
    /conversion/i,
    "Total Conversions renders a chart summary naming the metric",
  );

  // --- Organic Conversions: Indexed excludes the zero-baseline client --------
  await page.locator('#tr-metric button[data-metric="rn_organic_conversions"]').click();
  await page.locator('#tr-content[aria-busy="false"]').waitFor();
  await page.locator('#tr-view button[data-view="indexed"]').click();
  await page.locator('#tr-content[aria-busy="false"]').waitFor();
  const indexedNote = await page.locator(".trends-note", { hasText: "cannot be indexed" }).innerText();
  assert.match(indexedNote, /3XE/, "the zero-baseline client (3XE) is named in the note");
  const datasetLabels = await page.locator("#tr-canvas").evaluate(
    (canvas) => window.Chart.getChart(canvas).data.datasets.map((d) => d.label),
  );
  assert.ok(!datasetLabels.includes("3XE"), "3XE is not drawn as a (silently empty) line in Indexed view");

  // --- Users: non-additive note appears in week/month, not in day -----------
  await page.locator('#tr-view button[data-view="compare"]').click();
  await page.locator('#tr-metric button[data-metric="rn_users"]').click();
  await page.locator('#tr-content[aria-busy="false"]').waitFor();
  assert.match(
    await page.locator(".trends-note", { hasText: "unique" }).innerText(),
    /Weekly values sum daily unique users and overstate the true unique count\./,
    "Users at a week bucket shows the sum-of-uniques caveat verbatim",
  );
  await page.locator('#tr-bucket button[data-bucket="day"]').click();
  await page.locator('#tr-content[aria-busy="false"]').waitFor();
  assert.equal(await page.locator(".trends-note", { hasText: "unique" }).count(), 0, "no caveat needed for a day bucket (no summing happens)");

  // An additive metric never shows the caveat, even in week/month.
  await page.locator('#tr-bucket button[data-bucket="week"]').click();
  await page.locator('#tr-metric button[data-metric="gsc_clicks"]').click();
  await page.locator('#tr-content[aria-busy="false"]').waitFor();
  assert.equal(await page.locator(".trends-note", { hasText: "unique" }).count(), 0, "Clicks is additive — no caveat");

  // --- table: a real 0 -> N move shows +N, not a blank dash -------------------
  await page.locator('#tr-metric button[data-metric="rn_direct_conversions"]').click();
  await page.locator('#tr-content[aria-busy="false"]').waitFor();
  const goreportRow = page.locator(".trends-table tbody tr", { hasText: "GOREPORT" });
  await goreportRow.waitFor();
  const deltaCell = (await goreportRow.locator("td.delta").innerText()).trim();
  assert.equal(deltaCell, "+6", "a previous-period 0 shows the absolute delta, not a percentage or a dash");

  // --- picker: no_data vs not_configured are told apart ----------------------
  const picker = page.locator("#tr-picker");
  await picker.evaluate((node) => node.setAttribute("open", ""));
  const pickerRows = await page.locator(".trends-picker__row.is-disabled").allInnerTexts();
  assert.ok(pickerRows.some((t) => /NYCHEARING/.test(t) && /no data yet/i.test(t)), "connected-but-empty client reads 'no data yet'");
  assert.ok(pickerRows.some((t) => /PJC/.test(t) && /not connected/i.test(t)), "never-configured client reads 'not connected'");

  assert.deepEqual(errors, [], `no console/page errors: ${errors.join(" | ")}`);

  await page.screenshot({ path: path.join(screenshots, "trends-conversions-1440.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: path.join(screenshots, "trends-conversions-390.png") });

  await browser.close();
  console.log("trends.cjs: all checks passed");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
