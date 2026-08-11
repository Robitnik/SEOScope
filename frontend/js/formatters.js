const numberFormatter = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
const compactFormatter = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const dateFormatter = new Intl.DateTimeFormat("en-US", { day: "2-digit", month: "short", year: "numeric" });
const shortDateFormatter = new Intl.DateTimeFormat("en-US", { day: "2-digit", month: "short" });
const dateTimeFormatter = new Intl.DateTimeFormat("en-US", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });

export const PROVIDERS = {
  ahrefs: { label: "Ahrefs", short: "AH", icon: null },
  semrush: { label: "Semrush", short: "SE", icon: "/assets/providers/semrush.svg" },
  gsc: { label: "Search Console", short: "GSC", icon: "/assets/providers/gsc.svg" },
  reporting_ninja: { label: "Reporting Ninja / GA4", short: "RN", icon: "/assets/providers/reporting-ninja.svg" },
  // Not a metric provider (task-25): it carries findings, not KPIs. Present only
  // so job-progress UI can label its cell; excluded from PROVIDER_ORDER below.
  tech_audit: { label: "Technical audit", short: "TA", icon: null },
  // Not a metric provider (task-26): it carries a post list, not KPIs. Present
  // only so job-progress UI can label its cell; excluded from PROVIDER_ORDER.
  blogs: { label: "Blogs", short: "BL", icon: null },
};

// Semrush is limited by source (paid plan required for full data) — hidden from the
// card block until that's resolved. Keep the entry in PROVIDERS so it's a one-line revert.
// `tech_audit`/`blogs` are excluded for a different reason: neither has KPIs for a card.
export const PROVIDER_ORDER = Object.keys(PROVIDERS).filter(
  (key) => key !== "semrush" && key !== "tech_audit" && key !== "blogs",
);

export const STATE_META = {
  ok: { label: "Healthy", icon: "circle-check", tone: "var(--success)" },
  attention: { label: "Needs attention", icon: "alert-triangle", tone: "var(--warning)" },
  stale: { label: "Stale data", icon: "clock-exclamation", tone: "var(--warning)" },
  failed_all: { label: "Collection failed", icon: "circle-x", tone: "var(--danger)" },
  complete: { label: "Complete data", icon: "circle-check", tone: "var(--success)" },
  partial: { label: "Partial data", icon: "alert-triangle", tone: "var(--warning)" },
  truncated: { label: "Limited by source", icon: "cut", tone: "var(--warning)" },
  auth_required: { label: "Authorization required", icon: "key", tone: "var(--danger)" },
  awaiting_auth: { label: "Awaiting sign-in", icon: "key", tone: "var(--warning)" },
  unavailable: { label: "Data not received", icon: "cloud-off", tone: "var(--warning)" },
  failed: { label: "Collection error", icon: "circle-x", tone: "var(--danger)" },
  not_configured: { label: "Not connected", icon: "plug-off", tone: "var(--ink-dim)" },
  queued: { label: "Queued", icon: "clock", tone: "var(--ink-muted)" },
  running: { label: "Running", icon: "loader-2", tone: "var(--accent)" },
  succeeded: { label: "Completed", icon: "circle-check", tone: "var(--success)" },
  cancelled: { label: "Cancelled", icon: "ban", tone: "var(--ink-muted)" },
  cancel_requested: { label: "Stopping", icon: "player-stop", tone: "var(--warning)" },
  failed_interrupted: { label: "Interrupted", icon: "alert-circle", tone: "var(--danger)" },
  skipped: { label: "Skipped", icon: "player-skip-forward", tone: "var(--ink-dim)" },
};

// task-36: Site Monitor change severity — never color-only (AGENTS §7), every
// tone pairs with an icon and a word. Run status (queued/running/succeeded/
// failed/failed_interrupted) reuses `STATE_META` above rather than a second
// dictionary — same vocabulary as every other job-shaped status in the app.
export const MONITOR_SEVERITY_META = {
  critical: { label: "Critical", icon: "alert-triangle", tone: "var(--danger)" },
  warning: { label: "Warning", icon: "alert-circle", tone: "var(--warning)" },
  info: { label: "Info", icon: "info-circle", tone: "var(--info)" },
};

const METRIC_LABELS = {
  health_score: "Health score",
  errors: "Errors",
  warnings: "Warnings",
  urls_crawled: "URLs crawled",
  authority_score: "Authority score",
  organic_traffic: "Organic traffic",
  organic_keywords: "Organic keywords",
  referring_domains: "Referring domains",
  clicks: "Clicks",
  impressions: "Impressions",
  ctr: "CTR",
  avg_position: "Average position",
  users: "Users",
  organic_users: "Organic users",
  total_conversions: "Total conversions",
  organic_conversions: "Organic conversions",
};

export const STAGE_LABELS = {
  queued: "Waiting to start",
  preflight: "Checking configuration",
  crawl: "Crawling site pages",
  probe: "Probing links, sitemap & images",
  detect: "Detecting technical issues",
  image_dims: "Measuring flagged images",
  authenticate: "Authenticating",
  fetch_current: "Fetching current period",
  fetch_previous: "Fetching previous period",
  fetch_current_gbp: "Fetching Business Profile current period",
  fetch_previous_gbp: "Fetching Business Profile previous period",
  fetch_gbp_reviews: "Fetching Business Profile reviews",
  fetch_current_daily: "Fetching current GSC daily trend",
  fetch_previous_daily: "Fetching previous GSC daily trend",
  fetch_current_pages: "Fetching current GSC pages",
  fetch_current_queries: "Fetching current GSC queries",
  fetch_previous_pages: "Fetching previous GSC pages",
  fetch_previous_queries: "Fetching previous GSC queries",
  export_issues: "Ahrefs is preparing the full issue export",
  parse_issues: "Parsing Ahrefs issues and affected URLs",
  normalize: "Normalizing",
  validate: "Validating data",
  persist: "Saving to database",
  complete: "Done",
  // task-32: phase boundaries and browser steps the adapters now report. Before
  // these existed the matrix fell through to "Waiting for status" on cells that
  // were visibly running — the UI contradicting what it already knew.
  connect: "Connecting to the service",
  discover: "Discovering available fields",
  open_session: "Opening the browser session",
  resolve_project: "Locating the project",
  unpack_export: "Unpacking the export",
  parse_affected: "Reading affected URLs",
  site_audit: "Reading the Site Audit overview",
  verify: "Verifying the collected numbers",
  kpi_complete: "Snapshot ready",
  datasets: "Collecting detail datasets",
  datasets_complete: "Detail datasets collected",
  dimensions_complete: "Pages and queries collected",
};

// Stage names are free-form by contract (`app/schemas/adapter.py`), and the
// per-dataset ones are built at runtime (`fetch_current_ga4_landing_pages`), so
// an exhaustive table is impossible by construction. This turns any unknown
// stage into readable English rather than letting it fall through to a fallback
// that would misreport a running cell as idle.
const STAGE_ROLE_PREFIXES = [
  ["fetch_current_", "current"],
  ["fetch_previous_", "previous"],
];

export function humanizeStage(stage) {
  if (!stage) return "";
  if (STAGE_LABELS[stage]) return STAGE_LABELS[stage];
  for (const [prefix, role] of STAGE_ROLE_PREFIXES) {
    if (stage.startsWith(prefix)) {
      const subject = stage.slice(prefix.length).replace(/^(ga4|gbp|gsc)_/, "").replaceAll("_", " ");
      return subject ? `Fetching ${subject} (${role})` : `Fetching ${role} period`;
    }
  }
  const words = String(stage).replaceAll("_", " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "";
}

// task-36: `change_type` is an open string by contract (`app/monitor/severity.py`
// `STATIC_SEVERITY`, plus dynamically-built tls_*/sitemap_url_* types) — this
// table covers the known catalog; anything new falls through to the same
// snake_case-to-sentence humanizer as `humanizeStage` above, so a future
// backend addition never renders as a raw identifier.
const CHANGE_TYPE_LABELS = {
  page_noindex_appeared: "Noindex appeared",
  robots_blocks_site: "robots.txt blocks the site",
  robots_unavailable: "robots.txt unavailable",
  gsc_verification_lost: "Search Console verification lost",
  analytics_tag_lost: "Analytics tag lost",
  site_down: "Site down",
  dns_ns_changed: "DNS nameservers changed",
  dns_apex_a_changed: "DNS apex A record changed",
  canonical_offsite: "Canonical points off-site",
  sitemap_lost: "Sitemap lost",
  canonical_changed: "Canonical changed",
  title_changed: "Title changed",
  description_changed: "Description changed",
  h1_changed: "H1 changed",
  page_status_error: "Page returned an error status",
  page_redirect_appeared: "Page started redirecting",
  page_gone: "Page is gone",
  sitemap_urls_removed: "URLs removed from sitemap",
  robots_rules_changed: "robots.txt rules changed",
  dns_txt_changed: "DNS TXT record changed",
  dns_mx_changed: "DNS MX record changed",
  monitor_blocked: "Blocked by site protection",
  dns_unavailable: "DNS could not be checked",
  page_new: "New page found",
  content_changed: "Page content changed",
  sitemap_lastmod_changed: "Sitemap lastmod changed",
  analytics_tag_added: "Analytics tag added",
  robots_missing: "robots.txt missing",
  page_body_changed: "Page body changed (no visible content change)",
  tls_expired: "TLS certificate expired",
  tls_expiring_3d: "TLS certificate expiring in 3 days",
  tls_expiring_14d: "TLS certificate expiring in 14 days",
  sitemap_url_redirects: "Sitemap URL redirects",
  sitemap_url_broken: "Sitemap URL is broken",
  sitemap_url_unavailable: "Sitemap URL unavailable",
  sitemap_broken_urls: "Sitemap has broken URLs",
};

function humanizeChangeType(value) {
  const words = String(value).replaceAll("_", " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "";
}

// Self-healing events (task-35 §3.4) are the same catalog with a `_resolved`
// suffix, at `info` severity — labelled relative to the original event rather
// than duplicated one-by-one in the table above.
export function changeTypeLabel(changeType) {
  if (!changeType) return "";
  if (changeType.endsWith("_resolved")) {
    const base = changeType.slice(0, -"_resolved".length);
    return `Resolved: ${CHANGE_TYPE_LABELS[base] || humanizeChangeType(base)}`;
  }
  return CHANGE_TYPE_LABELS[changeType] || humanizeChangeType(changeType);
}

export const SOURCE_LABELS = {
  api: "API",
  browser: "Browser",
  pdf: "PDF report",
  auto: "Auto",
};

export function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}

export function stateClass(state = "unavailable") {
  return `state-${String(state).replace(/[^a-z0-9_-]/gi, "-")}`;
}

export function icon(name, className = "icon") {
  return `<svg class="${className}" aria-hidden="true"><use href="/assets/icons/tabler-sprite.svg#tabler-${name}"></use></svg>`;
}

export function metricLabel(metric) {
  return METRIC_LABELS[metric.metric_key] || metric.label || metric.metric_key;
}

export function formatRange(range) {
  if (!range?.start || !range?.end) return "Period unavailable";
  return `${dateFormatter.format(new Date(`${range.start}T12:00:00`))} — ${dateFormatter.format(new Date(`${range.end}T12:00:00`))}`;
}

// Single-date form for snapshot metrics, which carry one `observed_at`/
// `data_through` instead of a range (PLAN §21: snapshot vs range must not
// look the same). Accepts a plain date (`YYYY-MM-DD`) or a datetime.
export function formatDate(value) {
  if (!value) return null;
  const iso = String(value).length <= 10 ? `${value}T12:00:00` : value;
  return dateFormatter.format(new Date(iso));
}

// Compact day/month form for table headers ("Current (19 Jul)") — the full
// range already shows above the table (periodInfoLine); this just anchors
// which column is which without repeating the year twice per row.
export function formatShortDate(value) {
  if (!value) return null;
  const iso = String(value).length <= 10 ? `${value}T12:00:00` : value;
  return shortDateFormatter.format(new Date(iso));
}

export function formatTimestamp(value, fallback = "Never updated") {
  if (!value) return fallback;
  return dateTimeFormatter.format(new Date(value));
}

export function formatValue(metric, field = "current") {
  const text = metric[`${field}_text`];
  const value = metric[`${field}_value`];
  if (text !== null && text !== undefined && text !== "") return text;
  if (value === null || value === undefined || value === "") return "No data";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  const precision = Number.isInteger(metric.precision) ? metric.precision : 2;
  const formatted = Math.abs(numeric) >= 10000 ? compactFormatter.format(numeric) : new Intl.NumberFormat("en-US", { maximumFractionDigits: precision }).format(numeric);
  if (metric.unit === "percent") return `${formatted}%`;
  if (metric.unit === "currency") return `$${formatted}`;
  return formatted;
}

export function formatDelta(metric) {
  if (metric.delta_pct !== null && metric.delta_pct !== undefined) {
    const value = Number(metric.delta_pct);
    return `${value > 0 ? "+" : ""}${numberFormatter.format(value)}%`;
  }
  if (metric.delta_abs !== null && metric.delta_abs !== undefined) {
    const value = Number(metric.delta_abs);
    return `${value > 0 ? "+" : ""}${numberFormatter.format(value)}`;
  }
  return metric.delta_reason ? "N/A" : "—";
}

export function trend(metric) {
  if (metric.is_improvement === true) return "good";
  if (metric.is_improvement === false) return "bad";
  return "neutral";
}

export function initials(name = "") {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((word) => word[0]).join("").toUpperCase() || "TY";
}

export function percent(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.min(100, Math.round(number))) : 0;
}

export function isTerminal(status) {
  return ["succeeded", "partial", "failed", "cancelled"].includes(status);
}
