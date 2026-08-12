// Site Monitor — спільний переглядач версій і дифів (task-36 §3.3).
//
// Один компонент на 4 типи об'єктів: page, robots, sitemap, dns. Патерн
// повторює `detail-drawer.js`: фабрика шукає діалог у DOM один раз при
// створенні, `open(descriptor)` заповнює його й викликає `showModal()`,
// прибирання й повернення фокуса — на `dialog.addEventListener("close", …)`,
// а не тільки на явний `close()`, бо Escape/backdrop теж мають прибрати стан.
//
// Нові CSS-класи, що очікують на `monitor.css` (усе інше — вже існуючі
// `.state-panel`, `.table-scroll`, `.analytics-table`, `.button`,
// `.icon-button`, `.visually-hidden`, `.skeleton*`, `.drawer-kpis`):
//   .monitor-diff-toolbar        — рядок з двома select-групами вибору версій
//   .monitor-diff-select-group   — один <label>+<select> у тулбарі
//   .monitor-diff-doc-links      — рядок посилань "Open saved document"
//   .monitor-diff-doc-link       — саме посилання (модифікатор на .button)
//   .monitor-diff-meta           — блок-резюме над таблицею ("N of M changed")
//   .monitor-diff-note           — додаткове пояснювальне речення в meta-блоці
//   .monitor-diff-table          — модифікатор на <table class="analytics-table">
//   .monitor-diff-row--changed   — <tr> зі зміненим полем (колір, не єдиний сигнал)
//   .monitor-diff-changed-icon   — маленька іконка-мітка зміни в клітинці поля
//   .monitor-diff-value          — <td> зі значенням (was/now)
//   .monitor-diff-value--empty   — модифікатор для "Not present"/"None"/"Empty"
//   .monitor-diff-list           — <ul> для спискових значень (analytics_ids, sitemaps, dns values)
//   .monitor-diff-rules          — <ul> для правил robots.txt групи
//   .monitor-diff-unchanged      — <details> з незміненими полями
//   .monitor-diff-subheading     — <h3> перед блоком текстового діфу
//   .monitor-diff-text-wrap      — скрол-контейнер тексту (аналог .table-scroll,
//                                  щоб довгий рядок ніколи не скролив сторінку)
//   .monitor-diff-text           — white-space: pre контейнер рядків діфу
//   .monitor-diff-line           — один рядок unified diff
//   .monitor-diff-line--add/--del/--context — модифікатори кольору рядка
//   .monitor-diff-marker         — гліф +/−/пробіл перед рядком (aria-hidden)
//   .monitor-diff-hunk           — роздільник хунка ("Line N")

import {
  getMonitorPageVersions,
  getMonitorPageDiff,
  getMonitorResourceVersions,
  getMonitorResourceDiff,
  getMonitorDnsVersions,
  getMonitorDocumentUrl,
} from "./api.js?v=20260812-monitor5";
import { escapeHtml, icon, formatTimestamp } from "./formatters.js?v=20260812-monitor5";

// ---------------------------------------------------------------------------
// Pure data helpers (exported — consumed directly by tests and by this
// module's own renderers).
// ---------------------------------------------------------------------------

// `difflib.unified_diff(..., lineterm="", n=2)` without filenames: index 0/1
// are the empty "--- "/"+++ " header lines and only exist when there is at
// least one hunk — they carry zero information and must never reach the DOM.
const HUNK_HEADER_RE = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@$/;

export function parseUnifiedDiff(lines) {
  if (!Array.isArray(lines) || lines.length === 0) return [];
  const entries = [];
  const start = lines[0] === "--- " && lines[1] === "+++ " ? 2 : 0;
  for (let i = start; i < lines.length; i += 1) {
    const line = lines[i];
    if (typeof line !== "string" || line.length === 0) continue;
    const hunkMatch = line.match(HUNK_HEADER_RE);
    if (hunkMatch) {
      entries.push({ type: "hunk", text: line, hunkLabel: `Line ${hunkMatch[1]}` });
      continue;
    }
    const marker = line[0];
    const text = line.slice(1);
    if (marker === "+") entries.push({ type: "add", text });
    else if (marker === "-") entries.push({ type: "del", text });
    else if (marker === " ") entries.push({ type: "context", text });
    // Anything else (e.g. difflib's "\ No newline at end of file") carries no
    // renderable content under this contract — dropped rather than guessed at.
  }
  return entries;
}

// Robots groups have no stable id, only their user-agent set — the backend
// labels a changed group as `group:<sorted-lowercased-uas-joined-by-+>`
// (`app/monitor/severity.py`), so this must reproduce the exact same key or
// a real change silently fails to highlight.
export function robotsGroupKey(userAgents) {
  const list = Array.isArray(userAgents) ? userAgents : [];
  const key = list.map((ua) => String(ua).toLowerCase()).sort().join("+");
  return key || "(empty)";
}

function robotsGroupLabel(userAgents) {
  const list = Array.isArray(userAgents) ? userAgents.filter(Boolean) : [];
  return list.length ? list.join(", ") : "(empty)";
}

// robots.txt intentionally gets a structural diff, not a line-by-line one:
// `Disallow: /` under `meta-externalagent` and under `*` are different rules,
// so groups are matched by user-agent identity, not by position.
export function robotsStructureToFields(fromStructure, toStructure, changedFields = []) {
  const changed = new Set(changedFields || []);
  const fromGroups = Array.isArray(fromStructure?.groups) ? fromStructure.groups : [];
  const toGroups = Array.isArray(toStructure?.groups) ? toStructure.groups : [];
  const toIndex = new Map(toGroups.map((group) => [robotsGroupKey(group.user_agents), group]));
  const fromIndex = new Map(fromGroups.map((group) => [robotsGroupKey(group.user_agents), group]));
  // Current groups first (in their current order), then groups that only
  // existed on the "from" side (removed groups) appended at the end.
  const orderedKeys = [...toIndex.keys(), ...[...fromIndex.keys()].filter((key) => !toIndex.has(key))];

  const rows = orderedKeys.map((groupKey) => {
    const toGroup = toIndex.get(groupKey) || null;
    const fromGroup = fromIndex.get(groupKey) || null;
    const userAgents = (toGroup || fromGroup)?.user_agents || [];
    return {
      key: `group:${groupKey}`,
      label: `User-agent: ${robotsGroupLabel(userAgents)}`,
      kind: "rules",
      from: fromGroup ? fromGroup.rules || [] : null,
      to: toGroup ? toGroup.rules || [] : null,
      changed: changed.has(`group:${groupKey}`),
    };
  });

  rows.push({
    key: "sitemaps",
    label: "Sitemaps",
    kind: "list",
    from: Array.isArray(fromStructure?.sitemaps) ? fromStructure.sitemaps : null,
    to: Array.isArray(toStructure?.sitemaps) ? toStructure.sitemaps : null,
    changed: changed.has("sitemaps"),
  });
  return rows;
}

// Forward-compatible with a future sitemap structure diff: `_process_sitemaps`
// never creates a version today, so `.../resources/sitemap/versions` always
// returns `[]` and this path is not reachable yet. Kept generic (flat
// key/value diff, skipping the `kind` discriminator) rather than hardcoded to
// an unknown future shape.
export function sitemapStructureToFields(fromStructure, toStructure, changedFields = []) {
  const from = fromStructure || {};
  const to = toStructure || {};
  const changed = new Set(changedFields || []);
  const keys = [...new Set([...Object.keys(from), ...Object.keys(to)])].filter((key) => key !== "kind");
  return keys.map((key) => ({
    key,
    label: humanizeFieldKey(key),
    kind: Array.isArray(from[key]) || Array.isArray(to[key]) ? "list" : "scalar",
    from: from[key] ?? null,
    to: to[key] ?? null,
    changed: changed.has(key),
  }));
}

const SIGNAL_FIELD_LABELS = {
  title: "Title",
  meta_robots: "Meta robots",
  x_robots_tag: "X-Robots-Tag",
  canonical: "Canonical",
  description: "Meta description",
  h1: "H1",
  analytics_ids: "Analytics IDs",
};

function humanizeFieldKey(key) {
  const words = String(key).replaceAll("_", " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : key;
}

// Flat key/value signal diff — used for page signals today. Not hardcoded to
// exactly the 7 known page keys: any extra key present in either snapshot
// still gets a row (humanized label) instead of being silently dropped.
export function signalsToFields(fromSignals, toSignals, changedFields = []) {
  const from = fromSignals || {};
  const to = toSignals || {};
  const changed = new Set(changedFields || []);
  const seen = new Set();
  const orderedKeys = [...Object.keys(SIGNAL_FIELD_LABELS), ...Object.keys(to), ...Object.keys(from)];
  const rows = [];
  for (const key of orderedKeys) {
    if (seen.has(key)) continue;
    seen.add(key);
    if (!(key in from) && !(key in to)) continue;
    const fromValue = from[key];
    const toValue = to[key];
    rows.push({
      key,
      label: SIGNAL_FIELD_LABELS[key] || humanizeFieldKey(key),
      kind: Array.isArray(fromValue) || Array.isArray(toValue) ? "list" : "scalar",
      from: fromValue ?? null,
      to: toValue ?? null,
      changed: changed.has(key),
    });
  }
  return rows;
}

// DNS has no diff endpoint at all — the two selected versions are already in
// hand from the versions list, so the pair is diffed client-side. `values` is
// compared as sorted copies: a resolver returning the same records in a
// different order is not a change worth flagging, only a different *set* is.
export function diffDnsVersions(fromVersion, toVersion) {
  const from = fromVersion || {};
  const to = toVersion || {};
  const fromValues = Array.isArray(from.values) ? from.values : [];
  const toValues = Array.isArray(to.values) ? to.values : [];
  const valuesChanged = JSON.stringify([...fromValues].sort()) !== JSON.stringify([...toValues].sort());
  const fromTtl = from.ttl ?? null;
  const toTtl = to.ttl ?? null;
  const fromResolver = from.resolver ?? null;
  const toResolver = to.resolver ?? null;
  return [
    {
      key: "values", label: "Values", kind: "list",
      from: Array.isArray(from.values) ? from.values : null,
      to: Array.isArray(to.values) ? to.values : null,
      changed: valuesChanged,
    },
    { key: "ttl", label: "TTL", kind: "scalar", from: fromTtl, to: toTtl, changed: fromTtl !== toTtl },
    { key: "resolver", label: "Resolver", kind: "scalar", from: fromResolver, to: toResolver, changed: fromResolver !== toResolver },
  ];
}

// ---------------------------------------------------------------------------
// Markup builders. Kept separate from fetching/state so each stays small and
// state-driven (AGENTS §12).
// ---------------------------------------------------------------------------

const KIND_TITLES = {
  page: "Page version history",
  robots: "robots.txt version history",
  sitemap: "Sitemap version history",
  dns: "DNS record version history",
};

function titleFor(kind) {
  return KIND_TITLES[kind] || "Version history";
}

function subtitleFor(descriptor, versions) {
  const identity = descriptor.kind === "page"
    ? `Page ${descriptor.pageId || ""}`.trim()
    : descriptor.kind === "dns"
      ? [descriptor.dnsName, descriptor.dnsRecordType].filter(Boolean).join(" ")
      : descriptor.resourceUrl || "";
  const count = versions ? `${versions.length} version${versions.length === 1 ? "" : "s"} recorded` : "Loading…";
  return [identity, count].filter(Boolean).join(" — ");
}

function loadingMarkup(label) {
  const row = `<div class="skeleton__row"><div class="skeleton-line"></div><div class="skeleton-line"></div><div class="skeleton-line"></div></div>`;
  return `<div class="skeleton"><span class="visually-hidden">Loading ${escapeHtml(label)}…</span><div class="skeleton__rows skeleton__rows--2">${row.repeat(4)}</div></div>`;
}

function failureMarkup(error, label) {
  return `<div class="state-panel state-failed">${icon("wifi-off")}<p><strong>Failed to load ${escapeHtml(label)}.</strong><br>${escapeHtml(error?.message || "Unknown error")}</p></div>`;
}

function emptyMarkup(kind) {
  if (kind === "sitemap") {
    // Permanent, not a temporary empty state: `_process_sitemaps` never
    // writes a version, so this text is correct forever, not "not yet".
    return `<div class="state-panel">${icon("file-off")}<p><strong>Sitemap file history is not tracked yet.</strong><br>The sitemap's URLs are checked for broken and redirecting links, but the raw file itself is not versioned, so there is nothing to compare here.</p></div>`;
  }
  return `<div class="state-panel">${icon("history")}<p><strong>No version history yet.</strong><br>It will appear here once the next Site Monitor check records a first version.</p></div>`;
}

function documentLinkFor(kind, version) {
  if (!version) return "";
  const sha256 = kind === "page" ? version.body_blob_sha256 : kind === "robots" ? version.content_sha256 : null;
  if (!sha256) return "";
  const label = formatTimestamp(version.observed_at, "this version");
  return `<a class="monitor-diff-doc-link button button--quiet" href="${escapeHtml(getMonitorDocumentUrl(sha256))}" target="_blank" rel="noreferrer">${icon("file-text")}<span>Open saved document (${escapeHtml(label)})</span></a>`;
}

function baselineMarkup(kind, version) {
  const metaRows = [`<div><span>Captured</span><strong>${escapeHtml(formatTimestamp(version.observed_at, "Unknown time"))}</strong></div>`];
  if (version.http_status !== undefined && version.http_status !== null) {
    metaRows.push(`<div><span>HTTP status</span><strong>${escapeHtml(String(version.http_status))}</strong></div>`);
  }
  if (kind === "dns") {
    metaRows.push(`<div><span>TTL</span><strong>${version.ttl !== undefined && version.ttl !== null ? escapeHtml(String(version.ttl)) : "Not present"}</strong></div>`);
    metaRows.push(`<div><span>Resolver</span><strong>${version.resolver ? escapeHtml(version.resolver) : "Not present"}</strong></div>`);
  }
  const valuesBlock = kind === "dns" && Array.isArray(version.values) && version.values.length
    ? `<ul class="monitor-diff-list">${version.values.map((value) => `<li>${escapeHtml(String(value))}</li>`).join("")}</ul>`
    : "";
  const doc = documentLinkFor(kind, version);
  return `
    <div class="state-panel">
      ${icon("history")}
      <div>
        <p><strong>Baseline recorded.</strong><br>Only one version has been captured so far, so there is nothing to compare yet. A comparison view appears once the next check records a second version.</p>
        <div class="drawer-kpis">${metaRows.join("")}</div>
        ${valuesBlock}
        ${doc}
      </div>
    </div>`;
}

function optionMarkup(version, selectedId) {
  const label = formatTimestamp(version.observed_at, "Unknown time");
  const statusSuffix = version.http_status !== undefined && version.http_status !== null ? ` · HTTP ${version.http_status}` : "";
  return `<option value="${escapeHtml(version.id)}"${String(version.id) === String(selectedId) ? " selected" : ""}>${escapeHtml(label)}${escapeHtml(statusSuffix)}</option>`;
}

function toolbarMarkup(versions, fromVersionId, toVersionId) {
  const fromOptions = versions.map((version) => optionMarkup(version, fromVersionId)).join("");
  const toOptions = versions.map((version) => optionMarkup(version, toVersionId)).join("");
  return `
    <div class="monitor-diff-toolbar">
      <label class="monitor-diff-select-group"><span>Compare from</span><select data-role="from">${fromOptions}</select></label>
      <label class="monitor-diff-select-group"><span>Compare to</span><select data-role="to">${toOptions}</select></label>
    </div>
    <div class="monitor-diff-doc-links" id="monitor-diff-doc-links"></div>
    <div class="monitor-diff-meta" id="monitor-diff-meta"></div>
    <div id="monitor-diff-result" aria-live="polite"></div>`;
}

function compareLoadingMarkup() {
  const row = `<div class="skeleton__row"><div class="skeleton-line"></div><div class="skeleton-line"></div><div class="skeleton-line"></div></div>`;
  return `<div class="skeleton" aria-hidden="true"><div class="skeleton__rows skeleton__rows--2">${row.repeat(3)}</div></div>`;
}

function valueMarkup(row, side) {
  const value = row[side];
  if (value === null || value === undefined) return `<span class="monitor-diff-value--empty">Not present</span>`;
  if (row.kind === "list") {
    if (!Array.isArray(value) || value.length === 0) return `<span class="monitor-diff-value--empty">None</span>`;
    return `<ul class="monitor-diff-list">${value.map((item) => `<li>${escapeHtml(String(item))}</li>`).join("")}</ul>`;
  }
  if (row.kind === "rules") {
    if (!Array.isArray(value) || value.length === 0) return `<span class="monitor-diff-value--empty">No rules</span>`;
    return `<ul class="monitor-diff-rules">${value.map((rule) => `<li>${escapeHtml(rule.directive)}: ${escapeHtml(rule.path)}</li>`).join("")}</ul>`;
  }
  if (value === "") return `<span class="monitor-diff-value--empty">Empty</span>`;
  return escapeHtml(String(value));
}

function rowMarkup(row) {
  // Changed rows are marked by text AND color (AGENTS §7) — the pencil icon
  // plus a visually-hidden "Changed:" prefix carry the meaning for screen
  // readers and print/high-contrast views where the row tint disappears.
  const marker = row.changed ? `${icon("pencil", "icon monitor-diff-changed-icon")}<span class="visually-hidden">Changed: </span>` : "";
  return `<tr class="monitor-diff-row${row.changed ? " monitor-diff-row--changed" : ""}">
    <th scope="row">${marker}${escapeHtml(row.label)}</th>
    <td class="monitor-diff-value">${valueMarkup(row, "from")}</td>
    <td class="monitor-diff-value">${valueMarkup(row, "to")}</td>
  </tr>`;
}

function tableMarkup(rows) {
  return `<div class="table-scroll"><table class="analytics-table monitor-diff-table">
    <thead><tr><th>Field</th><th>Was</th><th>Now</th></tr></thead>
    <tbody>${rows.map(rowMarkup).join("")}</tbody>
  </table></div>`;
}

// Only changed fields sit in the main table; the rest hide behind a closed
// <details> — twelve signal fields for one real change would otherwise drown
// the one thing the operator opened this drawer to see.
function fieldsTableMarkup(fields) {
  if (!fields.length) return `<p class="monitor-diff-note">No fields to compare.</p>`;
  const changed = fields.filter((row) => row.changed);
  const unchanged = fields.filter((row) => !row.changed);
  const changedBlock = changed.length ? tableMarkup(changed) : `<p class="monitor-diff-note">No changed fields between these versions.</p>`;
  const unchangedBlock = unchanged.length
    ? `<details class="monitor-diff-unchanged"><summary>Show ${unchanged.length} unchanged field${unchanged.length === 1 ? "" : "s"}</summary>${tableMarkup(unchanged)}</details>`
    : "";
  return changedBlock + unchangedBlock;
}

function textDiffMarkup(textDiff) {
  if (!Array.isArray(textDiff) || textDiff.length === 0) return "";
  const entries = parseUnifiedDiff(textDiff);
  if (!entries.length) return "";
  const lines = entries.map((entry) => {
    if (entry.type === "hunk") return `<div class="monitor-diff-hunk" role="separator">${escapeHtml(entry.hunkLabel)}</div>`;
    if (entry.type === "add") return `<div class="monitor-diff-line monitor-diff-line--add"><span class="monitor-diff-marker" aria-hidden="true">+</span><span class="visually-hidden">Added: </span><ins>${escapeHtml(entry.text)}</ins></div>`;
    if (entry.type === "del") return `<div class="monitor-diff-line monitor-diff-line--del"><span class="monitor-diff-marker" aria-hidden="true">−</span><span class="visually-hidden">Removed: </span><del>${escapeHtml(entry.text)}</del></div>`;
    return `<div class="monitor-diff-line monitor-diff-line--context"><span class="monitor-diff-marker" aria-hidden="true">&nbsp;</span>${escapeHtml(entry.text)}</div>`;
  }).join("");
  return `<h3 class="monitor-diff-subheading">Visible text changes</h3><div class="monitor-diff-text-wrap"><div class="monitor-diff-text">${lines}</div></div>`;
}

function metaMarkup(kind, fields, textDiff) {
  const total = fields.length;
  const changedCount = fields.filter((row) => row.changed).length;
  const fieldsLine = total
    ? `<p>${changedCount} of ${total} field${total === 1 ? "" : "s"} changed</p>`
    : `<p>No fields to compare.</p>`;
  let note = "";
  if (kind === "page") {
    note = Array.isArray(textDiff) && textDiff.length
      ? "The visible page text changed — see the diff below."
      : "No visible text change between these versions.";
  } else if (kind === "robots") {
    note = "robots.txt is compared by rule group, not line-by-line — the same directive can mean something different under a different user-agent.";
  }
  return fieldsLine + (note ? `<p class="monitor-diff-note">${escapeHtml(note)}</p>` : "");
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function createMonitorDiffViewer({ onError, onStateChange } = {}) {
  const dialog = document.querySelector("#monitor-diff-dialog");
  const title = document.querySelector("#monitor-diff-title");
  const subtitle = document.querySelector("#monitor-diff-subtitle");
  const body = document.querySelector("#monitor-diff-body");

  let opener = null;
  let current = null; // resolved descriptor (no `trigger`) mirroring the last emitted state
  let versions = [];
  let requestToken = 0;

  dialog.addEventListener("close", () => {
    current = null;
    versions = [];
    requestToken += 1; // invalidate any fetch still in flight
    const toFocus = opener;
    opener = null;
    toFocus?.focus();
    notifyState();
  });

  function notifyState() {
    onStateChange?.(current ? { ...current } : null);
  }

  function fetchVersions(descriptor) {
    switch (descriptor.kind) {
      case "page":
        return getMonitorPageVersions(descriptor.clientId, descriptor.pageId);
      case "robots":
      case "sitemap":
        return getMonitorResourceVersions(descriptor.clientId, descriptor.kind, { url: descriptor.resourceUrl });
      case "dns":
        return getMonitorDnsVersions(descriptor.clientId, { name: descriptor.dnsName, recordType: descriptor.dnsRecordType });
      default:
        return Promise.reject(new Error(`Unsupported monitor diff kind: ${descriptor.kind}`));
    }
  }

  async function open(descriptor = {}) {
    opener = descriptor.trigger || null;
    current = { ...descriptor };
    delete current.trigger;
    versions = [];

    title.textContent = titleFor(current.kind);
    subtitle.textContent = subtitleFor(current, null);
    body.innerHTML = loadingMarkup("version history");
    if (!dialog.open) dialog.showModal();

    const token = (requestToken += 1);
    try {
      const fetched = await fetchVersions(current);
      if (token !== requestToken) return;
      versions = Array.isArray(fetched) ? fetched : [];
      await renderVersions(token);
    } catch (error) {
      if (token !== requestToken) return;
      body.innerHTML = failureMarkup(error, "version history");
      onError?.(error);
    }
  }

  function close() {
    dialog.close();
  }

  async function renderVersions(token) {
    subtitle.textContent = subtitleFor(current, versions);

    if (versions.length === 0) {
      body.innerHTML = emptyMarkup(current.kind);
      current.fromVersionId = null;
      current.toVersionId = null;
      notifyState();
      return;
    }

    if (versions.length === 1) {
      body.innerHTML = baselineMarkup(current.kind, versions[0]);
      current.fromVersionId = null;
      current.toVersionId = versions[0].id;
      notifyState();
      return;
    }

    const hasValidTo = current.toVersionId != null && versions.some((version) => version.id === current.toVersionId);
    const hasValidFrom = current.fromVersionId != null && versions.some((version) => version.id === current.fromVersionId);
    current.toVersionId = hasValidTo ? current.toVersionId : versions[0].id;
    current.fromVersionId = hasValidFrom ? current.fromVersionId : versions[1].id;

    body.innerHTML = toolbarMarkup(versions, current.fromVersionId, current.toVersionId);
    wireToolbar();
    notifyState();
    await renderComparison(token);
  }

  function wireToolbar() {
    const fromSelect = body.querySelector('select[data-role="from"]');
    const toSelect = body.querySelector('select[data-role="to"]');
    fromSelect?.addEventListener("change", () => {
      current.fromVersionId = fromSelect.value;
      onCompareChange();
    });
    toSelect?.addEventListener("change", () => {
      current.toVersionId = toSelect.value;
      onCompareChange();
    });
  }

  function onCompareChange() {
    notifyState();
    const token = (requestToken += 1);
    renderComparison(token);
  }

  async function renderComparison(token) {
    const fromVersion = versions.find((version) => version.id === current.fromVersionId) || null;
    const toVersion = versions.find((version) => version.id === current.toVersionId) || null;

    const docLinksEl = body.querySelector("#monitor-diff-doc-links");
    const metaEl = body.querySelector("#monitor-diff-meta");
    const resultEl = body.querySelector("#monitor-diff-result");
    if (docLinksEl) docLinksEl.innerHTML = [documentLinkFor(current.kind, fromVersion), documentLinkFor(current.kind, toVersion)].filter(Boolean).join("");
    if (metaEl) metaEl.innerHTML = "";
    if (resultEl) {
      resultEl.setAttribute("aria-busy", "true");
      resultEl.innerHTML = compareLoadingMarkup();
    }

    try {
      let fields = [];
      let textDiff = null;
      if (current.kind === "page") {
        const diff = await getMonitorPageDiff(current.clientId, current.pageId, { fromVersionId: current.fromVersionId, toVersionId: current.toVersionId });
        if (token !== requestToken) return;
        fields = signalsToFields(diff.from_signals, diff.to_signals, diff.changed_fields);
        textDiff = diff.text_diff || [];
      } else if (current.kind === "robots" || current.kind === "sitemap") {
        const diff = await getMonitorResourceDiff(current.clientId, current.kind, { url: current.resourceUrl, fromVersionId: current.fromVersionId, toVersionId: current.toVersionId });
        if (token !== requestToken) return;
        fields = current.kind === "robots"
          ? robotsStructureToFields(diff.from_structure, diff.to_structure, diff.changed_fields)
          : sitemapStructureToFields(diff.from_structure, diff.to_structure, diff.changed_fields);
      } else if (current.kind === "dns") {
        fields = diffDnsVersions(fromVersion, toVersion);
      }
      if (token !== requestToken) return;

      if (metaEl) metaEl.innerHTML = metaMarkup(current.kind, fields, textDiff);
      if (resultEl) {
        resultEl.removeAttribute("aria-busy");
        resultEl.innerHTML = fieldsTableMarkup(fields) + (current.kind === "page" ? textDiffMarkup(textDiff) : "");
      }
    } catch (error) {
      if (token !== requestToken) return;
      if (resultEl) {
        resultEl.removeAttribute("aria-busy");
        resultEl.innerHTML = failureMarkup(error, "diff");
      }
      onError?.(error);
    }
  }

  return { open, close };
}
