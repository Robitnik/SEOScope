// task-37 F3: `normalizeErrorDetail` is the one place FastAPI's three
// `detail` shapes (string / `{message, error_code}` / 422 validation array)
// funnel through before reaching `new Error(...)` — without it, a 422 array
// stringifies to the literal text "[object Object]" in every error toast in
// the app, not just Site Monitor.
//
// `api.js` reads `location.search` at module load (`FIXTURE_MODE`), so a
// bare Node import needs a `location` shim first — same DOM-boundary reason
// `formatters.test.mjs` sticks to `formatters.js`/`monitor-diff.js`, except
// here the one module-scope statement is trivial to stub instead of avoided.
// Static `import` is hoisted above any other top-level code, so the shim has
// to go through a dynamic `import()` (awaited at module top level) instead —
// a plain `import {...} from "..."` at the top of this file would run before
// the assignment below and hit the same `ReferenceError` api.js itself would.
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.location = { search: "" };
const { normalizeErrorDetail } = await import("../../../frontend/js/api.js");

test("normalizeErrorDetail passes a plain string through unchanged", () => {
  assert.equal(normalizeErrorDetail("Client not found", 404), "Client not found");
});

test("normalizeErrorDetail reads .message off an object detail", () => {
  assert.equal(
    normalizeErrorDetail({ message: "Change not found", error_code: "change_not_found" }, 404),
    "Change not found",
  );
});

test("normalizeErrorDetail never renders a 422 validation array as [object Object]", () => {
  const detail = [
    { loc: ["query", "sort"], msg: "Input should be 'url', '-url', ...", type: "enum" },
  ];
  const message = normalizeErrorDetail(detail, 422);
  assert.doesNotMatch(message, /\[object Object\]/);
  assert.match(message, /sort/);
  assert.match(message, /Input should be/);
});

test("normalizeErrorDetail joins up to 3 array items and drops body/query loc noise", () => {
  const detail = [
    { loc: ["query", "limit"], msg: "must be positive", type: "int" },
    { loc: ["body", "title"], msg: "field required", type: "missing" },
  ];
  const message = normalizeErrorDetail(detail, 422);
  assert.equal(message, "limit: must be positive; title: field required");
});

test("normalizeErrorDetail falls back to the HTTP status for an empty/unknown detail", () => {
  assert.equal(normalizeErrorDetail(undefined, 500), "HTTP 500");
  assert.equal(normalizeErrorDetail(null, 500), "HTTP 500");
  assert.equal(normalizeErrorDetail([], 422), "HTTP 422");
  assert.equal(normalizeErrorDetail({}, 500), "HTTP 500");
});
