// task-36. `monitor-diff.js` imports `api.js`, which reads
// `location.search` at module scope (fixture-mode detection) — a single
// browser global, not a DOM tree. One-line shim below, no new dependency,
// no jsdom: this is deliberately narrower than trying to make `router.js` or
// any `*-page.js` importable (those touch `document`/`history`/`window` in a
// cascade of side effects at import time and are left to the browser e2e
// smoke test instead — see the comment in formatters.test.mjs).
//
// Static `import` statements are hoisted and evaluate before any preceding
// plain statement in the same module, so the shim has to run before a
// *dynamic* `import()` — a static import of `monitor-diff.js` here would
// resolve (and crash inside `api.js`) before the assignment below ever runs.
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.location = { search: "" };

const {
  parseUnifiedDiff,
  robotsGroupKey,
  robotsStructureToFields,
  signalsToFields,
  diffDnsVersions,
} = await import("../../../frontend/js/monitor-diff.js");

test("parseUnifiedDiff drops the empty --- /+++ filename headers", () => {
  const lines = ["--- ", "+++ ", "@@ -1,3 +1,3 @@", " context", "-removed", "+added", " context2"];
  const entries = parseUnifiedDiff(lines);
  assert.equal(entries.some((e) => e.text === undefined), false);
  assert.deepEqual(entries.map((e) => e.type), ["hunk", "context", "del", "add", "context"]);
  assert.equal(entries[0].hunkLabel, "Line 1");
  assert.equal(entries[2].text, "removed");
  assert.equal(entries[3].text, "added");
});

test("parseUnifiedDiff returns [] for an empty text_diff (no visible change)", () => {
  assert.deepEqual(parseUnifiedDiff([]), []);
});

test("parseUnifiedDiff handles a diff with no header lines (defensive)", () => {
  const entries = parseUnifiedDiff(["@@ -5,1 +5,1 @@", "-old", "+new"]);
  assert.deepEqual(entries.map((e) => e.type), ["hunk", "del", "add"]);
});

test("robotsGroupKey matches the backend's group:<sorted-lowercased-uas> label exactly", () => {
  // app/monitor/robots.py: sorted, lowercased, joined by "+"; empty group -> "(empty)".
  assert.equal(robotsGroupKey(["*"]), "*");
  assert.equal(robotsGroupKey([]), "(empty)");
  assert.equal(robotsGroupKey(["Googlebot-Image", "*"]), "*+googlebot-image");
  // Order in the input must not matter — the backend sorts before joining.
  assert.equal(robotsGroupKey(["*", "Googlebot-Image"]), robotsGroupKey(["Googlebot-Image", "*"]));
});

test("robotsStructureToFields marks only the changed group, keeps unaffected groups unmarked", () => {
  const from = {
    groups: [
      { user_agents: ["*"], rules: [{ directive: "Disallow", path: "/wp-admin/" }] },
      { user_agents: ["googlebot-image"], rules: [{ directive: "Allow", path: "/" }] },
    ],
    sitemaps: ["https://example.com/sitemap.xml"],
  };
  const to = {
    groups: [
      { user_agents: ["*"], rules: [{ directive: "Disallow", path: "/wp-admin/" }, { directive: "Disallow", path: "/staging/" }] },
      { user_agents: ["googlebot-image"], rules: [{ directive: "Allow", path: "/" }] },
    ],
    sitemaps: ["https://example.com/sitemap.xml", "https://example.com/sitemap-images.xml"],
  };
  const rows = robotsStructureToFields(from, to, ["group:*", "sitemaps"]);
  const starRow = rows.find((r) => r.key === "group:*");
  const imageRow = rows.find((r) => r.key === "group:googlebot-image");
  const sitemapsRow = rows.find((r) => r.key === "sitemaps");
  assert.equal(starRow.changed, true);
  assert.equal(imageRow.changed, false);
  assert.equal(sitemapsRow.changed, true);
  assert.equal(starRow.to.length, 2);
});

test("robotsStructureToFields handles a removed group (present only on the from side)", () => {
  const from = { groups: [{ user_agents: ["gptbot"], rules: [{ directive: "Disallow", path: "/" }] }], sitemaps: [] };
  const to = { groups: [], sitemaps: [] };
  const rows = robotsStructureToFields(from, to, ["group:gptbot"]);
  const row = rows.find((r) => r.key === "group:gptbot");
  assert.equal(row.changed, true);
  assert.equal(row.to, null);
  assert.ok(Array.isArray(row.from));
});

test("signalsToFields only flags fields present in changed_fields, others stay changed:false", () => {
  const from = { title: "Old", meta_robots: "index, follow", canonical: "https://x.com/" };
  const to = { title: "Old", meta_robots: "noindex, follow", canonical: "https://x.com/" };
  const rows = signalsToFields(from, to, ["meta_robots"]);
  const robotsRow = rows.find((r) => r.key === "meta_robots");
  const titleRow = rows.find((r) => r.key === "title");
  assert.equal(robotsRow.changed, true);
  assert.equal(titleRow.changed, false);
});

test("signalsToFields does not drop unknown/future signal keys", () => {
  const rows = signalsToFields({ some_new_signal: "a" }, { some_new_signal: "b" }, ["some_new_signal"]);
  const row = rows.find((r) => r.key === "some_new_signal");
  assert.ok(row, "unknown key must still produce a row");
  assert.equal(row.changed, true);
});

test("diffDnsVersions treats reordered-but-identical values as unchanged", () => {
  const from = { values: ["1.1.1.1", "2.2.2.2"], ttl: 300, resolver: "dns.google" };
  const to = { values: ["2.2.2.2", "1.1.1.1"], ttl: 300, resolver: "dns.google" };
  const rows = diffDnsVersions(from, to);
  assert.equal(rows.find((r) => r.key === "values").changed, false);
});

test("diffDnsVersions flags a genuine value-set change, and independently flags ttl/resolver", () => {
  const from = { values: ["1.1.1.1"], ttl: 300, resolver: "dns.google" };
  const to = { values: ["9.9.9.9"], ttl: 60, resolver: "cloudflare-dns.com" };
  const rows = diffDnsVersions(from, to);
  assert.equal(rows.find((r) => r.key === "values").changed, true);
  assert.equal(rows.find((r) => r.key === "ttl").changed, true);
  assert.equal(rows.find((r) => r.key === "resolver").changed, true);
});
