import { escapeHtml, icon } from "./formatters.js?v=20260812-monitor5";
import { formatUaCtr, formatUaDelta, formatUaNumber, formatUaPosition } from "./client-page.js?v=20260812-monitor5";

const PAGE_SIZE = 20;
const SORT_OPTIONS = [
  { key: "clicks", label: "Clicks" },
  { key: "impressions", label: "Impressions" },
  { key: "ctr", label: "CTR" },
  { key: "position", label: "Position" },
];

// One row = current period's own row (always present, since it drives the
// list) joined with the matching previous-period row by dimension_value, if
// any. A page/query absent from the previous period keeps `*_previous` as
// `null` — never `0` — and is flagged "new" (PLAN §12).
export function mergeRows(currentRows, previousRows) {
  const byValue = new Map((previousRows || []).map((row) => [row.dimension_value, row]));
  return (currentRows || []).map((row) => {
    const previous = byValue.get(row.dimension_value) || null;
    return {
      dimension_value: row.dimension_value,
      rank: row.rank,
      quality: row.quality,
      clicks_current: row.clicks,
      clicks_previous: previous ? previous.clicks : null,
      impressions_current: row.impressions,
      impressions_previous: previous ? previous.impressions : null,
      ctr_current: row.ctr,
      position_current: row.position,
    };
  });
}

function emptyPanel(title, note) {
  return `<div class="state-panel">${icon("table-off")}<p><strong>${escapeHtml(title)}</strong>${note ? `<br>${escapeHtml(note)}` : ""}</p></div>`;
}

function rowMarkup(row) {
  const isNew = row.clicks_previous === null && row.clicks_current !== null;
  const deltaClicks = row.clicks_previous === null || row.clicks_current === null ? null : row.clicks_current - row.clicks_previous;
  const trend = deltaClicks === null ? "" : deltaClicks > 0 ? "good" : deltaClicks < 0 ? "bad" : "neutral";
  return `
    <tr class="${row.rank === null ? "analytics-row--remainder" : ""}">
      <td class="analytics-row__value">${escapeHtml(row.dimension_value)}${isNew ? `<small class="tag-new">new</small>` : ""}</td>
      <td class="num">${formatUaNumber(row.clicks_current)}</td>
      <td class="num muted">${formatUaNumber(row.clicks_previous)}</td>
      <td class="num delta" data-trend="${trend}">${deltaClicks === null ? "—" : formatUaDelta(deltaClicks)}</td>
      <td class="num">${formatUaNumber(row.impressions_current)}</td>
      <td class="num muted">${formatUaNumber(row.impressions_previous)}</td>
      <td class="num">${formatUaCtr(row.ctr_current)}</td>
      <td class="num">${formatUaPosition(row.position_current)}</td>
    </tr>`;
}

function pagination(page, pageCount, total) {
  if (pageCount <= 1) return "";
  return `
    <nav class="pagination" aria-label="Table pages">
      <button type="button" class="button button--quiet" id="cp-page-prev" ${page <= 1 ? "disabled" : ""}>${icon("chevron-left")}Back</button>
      <span>Page ${page} of ${pageCount} · ${total} rows</span>
      <button type="button" class="button button--quiet" id="cp-page-next" ${page >= pageCount ? "disabled" : ""}>Next${icon("chevron-right")}</button>
    </nav>`;
}

export function createAnalyticsTab({ container, getState, onChange }) {
  let meta = null;
  let rows = [];

  function setData(currentPayload, previousPayload) {
    meta = currentPayload;
    rows = mergeRows(currentPayload.rows, previousPayload.rows);
  }

  function toolbar(state) {
    const noun = state.tab === "page" ? "pages" : "queries";
    return `
      <div class="analytics-toolbar">
        <label class="search-field">
          <span class="visually-hidden">Search ${noun}</span>
          ${icon("search")}
          <input type="search" id="cp-search" placeholder="${state.tab === "page" ? "Search by URL" : "Search by query"}" value="${escapeHtml(state.search)}" autocomplete="off">
        </label>
        <label class="sort-field">Sort by
          <select id="cp-sort-select">${SORT_OPTIONS.map((option) => `<option value="${option.key}" ${option.key === state.sortBy ? "selected" : ""}>${option.label}</option>`).join("")}</select>
        </label>
      </div>`;
  }

  function bindEvents(state) {
    let searchTimer = null;
    container.querySelector("#cp-search")?.addEventListener("input", (event) => {
      clearTimeout(searchTimer);
      const value = event.target.value;
      searchTimer = setTimeout(() => onChange({ search: value, page: 1 }), 250);
    });
    container.querySelector("#cp-sort-select")?.addEventListener("change", (event) => onChange({ sortBy: event.target.value, page: 1 }));
    container.querySelector("#cp-page-prev")?.addEventListener("click", () => onChange({ page: state.page - 1 }));
    container.querySelector("#cp-page-next")?.addEventListener("click", () => onChange({ page: state.page + 1 }));
  }

  function render() {
    const state = getState();
    const searchInput = container.querySelector("#cp-search");
    const hadFocus = document.activeElement === searchInput;
    const caret = hadFocus ? searchInput.selectionStart : null;

    if (!meta) { container.innerHTML = ""; return; }
    if (!meta.period_start) {
      container.innerHTML = `${toolbar(state)}${emptyPanel("No data collected for this period yet.", "GSC dimensional data will appear here once at least one exact period has been collected for this range.")}`;
      bindEvents(state);
      return;
    }

    const query = state.search.trim().toLocaleLowerCase("en-US");
    const filtered = query ? rows.filter((row) => row.dimension_value.toLocaleLowerCase("en-US").includes(query)) : rows;

    if (!filtered.length) {
      container.innerHTML = `${toolbar(state)}${emptyPanel(query ? `No results found for "${state.search}".` : "No rows for this period.", "")}`;
      bindEvents(state);
      return;
    }

    const sortKey = `${state.sortBy}_current`;
    const sorted = [...filtered].sort((a, b) => (Number(b[sortKey]) || 0) - (Number(a[sortKey]) || 0));
    const pageCount = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
    const page = Math.min(state.page, pageCount);
    const pageRows = sorted.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
    const noun = state.tab === "page" ? "Page" : "Query";

    container.innerHTML = `
      ${toolbar(state)}
      ${meta.truncated ? `<p class="truncated-note">${icon("cut")}List truncated to the top ${meta.rows.length} by impressions; the "other" row sums the rest and is not presented as a full list.</p>` : ""}
      <div class="table-scroll">
        <table class="analytics-table">
          <caption class="visually-hidden">${noun === "Page" ? "Pages" : "Queries"}: clicks, impressions, CTR and position for the current and previous period</caption>
          <thead><tr>
            <th scope="col">${noun}</th>
            <th scope="col">Clicks, now</th>
            <th scope="col">Clicks, before</th>
            <th scope="col">Δ clicks</th>
            <th scope="col">Impressions, now</th>
            <th scope="col">Impressions, before</th>
            <th scope="col">CTR</th>
            <th scope="col">Position</th>
          </tr></thead>
          <tbody>${pageRows.map(rowMarkup).join("")}</tbody>
        </table>
      </div>
      ${pagination(page, pageCount, sorted.length)}
      <p class="visually-hidden" aria-live="polite">${sorted.length} rows, page ${page} of ${pageCount}</p>`;

    bindEvents({ ...state, page });
    if (hadFocus) {
      const newInput = container.querySelector("#cp-search");
      newInput?.focus();
      if (caret !== null) newInput?.setSelectionRange(caret, caret);
    }
  }

  return { setData, render };
}
