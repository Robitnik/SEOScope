// Loading placeholders (frontend/AGENTS.md §6).
//
// One rule drives everything here: a skeleton must mirror the block it stands
// in for. Every section on the client page is heading + KPI strip + rows, so
// that is exactly what these builders emit — same grid, same dividers, same
// heights — and the real content replaces it without shifting the page.
//
// The markup is decorative: it is `aria-hidden`, and the one thing assistive
// tech gets is a plain "Loading …" line. Callers keep `aria-busy="true"` on the
// live region while a skeleton is mounted.

function lines(count, className = "skeleton-line") {
  return Array.from({ length: count }, () => `<div class="${className}"></div>`).join("");
}

function headingBlock({ status = true } = {}) {
  return `
    <div class="skeleton__heading">
      <div><div class="skeleton-line skeleton-line--eyebrow"></div><div class="skeleton-line skeleton-line--title"></div></div>
      ${status ? `<div class="skeleton__status"></div>` : ""}
    </div>`;
}

function kpiBlock(count) {
  if (!count) return "";
  const tiles = Array.from({ length: count }, () => `<div class="skeleton__kpi">${lines(2)}</div>`).join("");
  return `<div class="skeleton__kpis skeleton__kpis--${count}">${tiles}</div>`;
}

function rowBlock(count, columns) {
  if (!count) return "";
  const row = `<div class="skeleton__row">${lines(columns + 1)}</div>`;
  return `<div class="skeleton__rows skeleton__rows--${columns}">${row.repeat(count)}</div>`;
}

/**
 * A whole section placeholder: heading, optional KPI strip, optional chart,
 * optional table rows. `label` names what is loading for screen readers.
 */
export function sectionSkeleton({ label = "content", heading = true, status = true, kpis = 0, chart = false, rows = 0, columns = 3 } = {}) {
  return `
    <div class="skeleton">
      <span class="visually-hidden">Loading ${label}…</span>
      ${heading ? headingBlock({ status }) : ""}
      ${kpiBlock(kpis)}
      ${chart ? `<div class="skeleton__chart"></div>` : ""}
      ${rowBlock(rows, columns)}
    </div>`;
}

/** Compact placeholder for a panel nested inside an already-titled section. */
export function panelSkeleton({ label = "details", rows = 4, columns = 3 } = {}) {
  return `
    <div class="skeleton">
      <span class="visually-hidden">Loading ${label}…</span>
      ${rowBlock(rows, columns)}
    </div>`;
}

/** Client page header: logo square, client name, then the status/health columns. */
export function clientHeaderSkeleton() {
  return `
    <div class="skeleton skeleton--header">
      <span class="visually-hidden">Loading client…</span>
      <div class="skeleton__identity">
        <div class="skeleton__logo"></div>
        <div class="skeleton__stack">
          <div class="skeleton-line skeleton-line--eyebrow"></div>
          <div class="skeleton-line skeleton-line--title"></div>
          <div class="skeleton-line skeleton-line--short"></div>
        </div>
      </div>
      <div class="skeleton__status"></div>
    </div>`;
}

/** Dashboard grid: cards keep their real frame so the grid never reflows. */
export function cardSkeletons(count = 4) {
  const card = `<article class="skeleton skeleton-card">
      <span class="visually-hidden">Loading client…</span>
      ${headingBlock()}
      ${kpiBlock(2)}
      ${rowBlock(6, 3)}
    </article>`;
  return card.repeat(count);
}
