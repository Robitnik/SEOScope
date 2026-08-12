// task-38. Чисті функції переглядача збереженого вмісту. Той самий shim, що в
// `monitor-diff.test.mjs`: модуль тягне `api.js`, який на рівні модуля читає
// `location.search`, тож глобал має існувати ДО динамічного імпорту.
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.location = { search: "" };

const { extractSitemapLocs, filterLines, unescapeXml } = await import("../../../frontend/js/monitor-content.js");

test("unescapeXml decodes the five predefined entities", () => {
  assert.equal(unescapeXml("?a=1&amp;b=2"), "?a=1&b=2");
  assert.equal(unescapeXml("&lt;tag&gt;"), "<tag>");
  assert.equal(unescapeXml("&quot;q&quot; &apos;a&apos;"), '"q" \'a\'');
});

test("unescapeXml resolves &amp; last so a double-escaped entity survives", () => {
  // `&amp;lt;` means the literal text "&lt;", not "<". Decoding `&amp;` first
  // would turn it into `&lt;` and then into `<` — losing what the file said.
  assert.equal(unescapeXml("&amp;lt;"), "&lt;");
});

test("extractSitemapLocs pulls every loc and unescapes it", () => {
  const xml = `<?xml version="1.0"?><urlset>
    <url><loc>https://x.com/a/</loc><lastmod>2026-01-01</lastmod></url>
    <url><loc>https://x.com/?a=1&amp;b=2</loc></url>
  </urlset>`;
  assert.deepEqual(extractSitemapLocs(xml), ["https://x.com/a/", "https://x.com/?a=1&b=2"]);
});

test("extractSitemapLocs survives a malformed document instead of returning nothing", () => {
  // Живий кейс: обірваний файл. Строгий XML-парсер віддав би нуль записів —
  // тобто збрехав би, що sitemap порожній.
  const broken = "<urlset><url><loc>https://x.com/a/</loc></url><url><loc>https://x.com/b/";
  assert.deepEqual(extractSitemapLocs(broken), ["https://x.com/a/"]);
});

test("extractSitemapLocs returns [] for a non-sitemap document", () => {
  assert.deepEqual(extractSitemapLocs("User-agent: *\nDisallow: /"), []);
});

test("filterLines is case-insensitive substring matching and keeps order", () => {
  const lines = ["https://x.com/Product/a", "https://x.com/blog/b", "https://x.com/product/c"];
  assert.deepEqual(filterLines(lines, "PRODUCT"), ["https://x.com/Product/a", "https://x.com/product/c"]);
});

test("filterLines with an empty or whitespace query returns everything", () => {
  const lines = ["a", "b"];
  assert.deepEqual(filterLines(lines, ""), lines);
  assert.deepEqual(filterLines(lines, "   "), lines);
  assert.deepEqual(filterLines(lines, undefined), lines);
});
