"""Web Archive (task-59): a self-hosted, Wayback-style copy of every active
client's site, captured twice a day and replayable byte-for-byte.

Deliberately independent from `app/monitor/**` (Site Monitor): the monitor is
a watchdog — cheap, hourly, SEO-signals-only, no subresources kept. This
module is a full archive — expensive, twice daily, every asset kept once.
Sharing a fingerprint/storage layer between the two would mean editing the
monitor's live alerting path for an unrelated feature; see the task-59 plan
§1.1 for the measured cost of NOT doing that (one extra HTML GET per URL per
capture, ~4.9s/page).

Submodules:
- `urls` — URL normalization (the `url_key` every table keys lookups by).
- `fingerprint` — "did the page really change" (task-59 §0.2/§0.3).
- `storage` — content-addressed gzip blob store, own `webarchive/` prefix.
- `assets` — subresource discovery from captured HTML/CSS.
- `rewrite` — replay-time URL rewriting (HTML attributes + CSS `url()`).
- `capture` — the per-client, per-URL capture pipeline.
- `replay` — timestamp format + nearest-snapshot resolution helpers.
- `scheduler` — `ArchiveScheduler`, a thread inside the worker process.
- `cloud_sync` — pull-sweeper uploading new blobs to B2 under `webarchive/`.
"""

from __future__ import annotations
