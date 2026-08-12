// task-38 §1: `listMonitorRuns` used to invent its own request key from
// `clientId` alone, so the history table, the F5-resume check (two calls,
// one per kind) and the live poll tick all shared it — `request()` treats a
// shared key as "abort the previous call", which is right WITHIN one purpose
// and wrong ACROSS four. This guards the fix: the caller must name its own
// intent, and `request()`'s "same key aborts, different key doesn't" contract
// still holds either way.
//
// Same DOM-boundary shim as `api.test.mjs` — `api.js` reads `location.search`
// at module load, so the global has to exist before the dynamic `import()`.
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.location = { search: "" };
const { listMonitorRuns, isAbortError } = await import("../../../frontend/js/api.js");

function stubAbortableFetch() {
  globalThis.fetch = (_url, options) =>
    new Promise((resolve, reject) => {
      options.signal.addEventListener("abort", () => {
        const error = new Error("The operation was aborted.");
        error.name = "AbortError";
        reject(error);
      });
      // Never resolves on its own — the point of these tests is to observe
      // abort behavior, not a real response.
    });
}

test("listMonitorRuns throws without an explicit requestKey", async () => {
  await assert.rejects(
    () => listMonitorRuns("client-1", { limit: 1 }),
    /requestKey/,
  );
});

test("two calls sharing a requestKey abort the first (request() contract)", async () => {
  stubAbortableFetch();
  const first = listMonitorRuns("client-1", { limit: 1, requestKey: "same-purpose" });
  const second = listMonitorRuns("client-1", { limit: 1, requestKey: "same-purpose" });
  await assert.rejects(first, (error) => isAbortError(error));
  // `second` is still in flight against the never-resolving stub — asserting
  // it did NOT reject yet is enough to show only `first` was cancelled.
  const raced = await Promise.race([second, new Promise((resolve) => setTimeout(() => resolve("pending"), 20))]);
  assert.equal(raced, "pending");
});

test("two calls with DIFFERENT requestKeys never abort each other", async () => {
  stubAbortableFetch();
  const full = listMonitorRuns("client-1", { kind: "full", limit: 1, requestKey: "monitor-runs-resume-full-client-1" });
  const light = listMonitorRuns("client-1", { kind: "light", limit: 1, requestKey: "monitor-runs-resume-light-client-1" });
  // Neither settles from the stub, but if the second's `request()` call had
  // aborted the first (the task-38 bug), `full` would already be a rejected
  // AbortError promise by now.
  const raced = await Promise.race([
    full.catch((error) => (isAbortError(error) ? "full-aborted" : "full-error")),
    light.catch((error) => (isAbortError(error) ? "light-aborted" : "light-error")),
    new Promise((resolve) => setTimeout(() => resolve("still-pending"), 20)),
  ]);
  assert.equal(raced, "still-pending");
});
