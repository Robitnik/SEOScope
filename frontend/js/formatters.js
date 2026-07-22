const numberFormatter = new Intl.NumberFormat("uk-UA", { maximumFractionDigits: 2 });
const compactFormatter = new Intl.NumberFormat("uk-UA", { notation: "compact", maximumFractionDigits: 1 });
const dateFormatter = new Intl.DateTimeFormat("uk-UA", { day: "2-digit", month: "short", year: "numeric" });
const dateTimeFormatter = new Intl.DateTimeFormat("uk-UA", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });

export const PROVIDERS = {
  ahrefs: { label: "Ahrefs", short: "AH", icon: null },
  semrush: { label: "Semrush", short: "SE", icon: "/assets/providers/semrush.svg" },
  gsc: { label: "Search Console", short: "GSC", icon: "/assets/providers/gsc.svg" },
  reporting_ninja: { label: "Reporting Ninja / GA4", short: "RN", icon: "/assets/providers/reporting-ninja.svg" },
};

export const PROVIDER_ORDER = Object.keys(PROVIDERS);

export const STATE_META = {
  ok: { label: "Все гаразд", icon: "circle-check", tone: "var(--success)" },
  attention: { label: "Потребує уваги", icon: "alert-triangle", tone: "var(--warning)" },
  stale: { label: "Дані застаріли", icon: "clock-exclamation", tone: "var(--warning)" },
  failed_all: { label: "Збір не вдався", icon: "circle-x", tone: "var(--danger)" },
  complete: { label: "Повні дані", icon: "circle-check", tone: "var(--success)" },
  partial: { label: "Часткові дані", icon: "alert-triangle", tone: "var(--warning)" },
  truncated: { label: "Ліміт джерела", icon: "cut", tone: "var(--warning)" },
  auth_required: { label: "Потрібна авторизація", icon: "key", tone: "var(--danger)" },
  unavailable: { label: "Джерело недоступне", icon: "cloud-off", tone: "var(--danger)" },
  failed: { label: "Помилка збору", icon: "circle-x", tone: "var(--danger)" },
  not_configured: { label: "Не підключено", icon: "plug-off", tone: "var(--ink-dim)" },
  queued: { label: "У черзі", icon: "clock", tone: "var(--ink-muted)" },
  running: { label: "Виконується", icon: "loader-2", tone: "var(--accent)" },
  succeeded: { label: "Завершено", icon: "circle-check", tone: "var(--success)" },
  cancelled: { label: "Скасовано", icon: "ban", tone: "var(--ink-muted)" },
  cancel_requested: { label: "Зупиняємо", icon: "player-stop", tone: "var(--warning)" },
  failed_interrupted: { label: "Перервано", icon: "alert-circle", tone: "var(--danger)" },
  skipped: { label: "Пропущено", icon: "player-skip-forward", tone: "var(--ink-dim)" },
};

const METRIC_LABELS = {
  health_score: "Стан сайту",
  errors: "Помилки",
  warnings: "Попередження",
  urls_crawled: "Перевірено URL",
  authority_score: "Авторитет домену",
  organic_traffic: "Органічний трафік",
  organic_keywords: "Органічні ключі",
  referring_domains: "Домени-донори",
  clicks: "Кліки",
  impressions: "Покази",
  ctr: "CTR",
  avg_position: "Середня позиція",
  users: "Користувачі",
  organic_users: "Органічні користувачі",
  total_conversions: "Усі конверсії",
  organic_conversions: "Органічні конверсії",
};

export const STAGE_LABELS = {
  queued: "Очікує запуску",
  preflight: "Перевірка налаштувань",
  authenticate: "Авторизація",
  fetch_current: "Поточний період",
  fetch_previous: "Попередній період",
  normalize: "Нормалізація",
  validate: "Перевірка",
  persist: "Запис у базу",
  complete: "Готово",
};

export const SOURCE_LABELS = {
  api: "API",
  browser: "Браузер",
  pdf: "PDF-звіт",
  auto: "Авто",
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
  if (!range?.start || !range?.end) return "Період не визначено";
  return `${dateFormatter.format(new Date(`${range.start}T12:00:00`))} — ${dateFormatter.format(new Date(`${range.end}T12:00:00`))}`;
}

export function formatTimestamp(value, fallback = "Ще не оновлювали") {
  if (!value) return fallback;
  return dateTimeFormatter.format(new Date(value));
}

export function formatValue(metric, field = "current") {
  const text = metric[`${field}_text`];
  const value = metric[`${field}_value`];
  if (text !== null && text !== undefined && text !== "") return text;
  if (value === null || value === undefined || value === "") return "Немає даних";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  const precision = Number.isInteger(metric.precision) ? metric.precision : 2;
  const formatted = Math.abs(numeric) >= 10000 ? compactFormatter.format(numeric) : new Intl.NumberFormat("uk-UA", { maximumFractionDigits: precision }).format(numeric);
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
  return metric.delta_reason ? "н/д" : "—";
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
