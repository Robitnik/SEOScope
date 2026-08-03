import { escapeHtml, formatDate, formatTimestamp, icon } from "./formatters.js?v=20260803-connect1";
import { formatUaNumber } from "./client-page.js?v=20260803-connect1";

const SORTS = [
  ["date_desc", "Newest first"],
  ["date_asc", "Oldest first"],
  ["words_desc", "Most words"],
  ["words_asc", "Fewest words"],
  ["title", "Title A–Z"],
];

function summaryHeader(summary) {
  const postCount = summary?.post_count;
  const blogUrl = summary?.blog_url;
  return `<div class="section-heading">
    <div>
      <p class="eyebrow">Client Blogs</p>
      <h2>${postCount === null || postCount === undefined ? "—" : formatUaNumber(postCount)} published posts</h2>
      <p class="blogs-collected">Last crawl: ${formatTimestamp(summary?.observed_at, "not crawled yet")}${blogUrl ? ` · <a href="${escapeHtml(blogUrl)}" target="_blank" rel="noreferrer">${escapeHtml(blogUrl)}</a>` : ""}</p>
    </div>
    <span class="status-label status-${escapeHtml(summary?.quality || (summary?.has_run ? "complete" : "not_configured"))}">${summary?.has_run ? escapeHtml(summary.quality || "complete") : "no run yet"}</span>
  </div>`;
}

function toolbar(state) {
  return `<div class="analytics-toolbar blogs-toolbar">
    <label class="search-field"><span class="visually-hidden">Search posts</span>${icon("search")}<input id="blogs-search" type="search" placeholder="Search title or URL" value="${escapeHtml(state.blogsSearch)}"></label>
    <label>Sort<select id="blogs-sort">${SORTS.map(([value, label]) => `<option value="${value}" ${state.blogsSort === value ? "selected" : ""}>${label}</option>`).join("")}</select></label>
  </div>`;
}

function postsTable(page) {
  const rows = page?.rows || [];
  if (!rows.length) return `<div class="state-panel"><p><strong>No matching posts.</strong></p></div>`;
  return `<div class="table-scroll"><table class="analytics-table blogs-table">
    <thead><tr><th>Title</th><th>Description</th><th>Word count</th><th>Link</th><th>Published</th></tr></thead>
    <tbody>${rows.map((post) => `<tr>
      <td class="analytics-row__value">${escapeHtml(post.title) || "—"}</td>
      <td>${escapeHtml(post.description) || "—"}</td>
      <td class="num">${post.word_count === null || post.word_count === undefined ? "—" : formatUaNumber(post.word_count)}</td>
      <td><a href="${escapeHtml(post.url)}" target="_blank" rel="noreferrer">${icon("external-link")}<span class="visually-hidden">Open post</span></a></td>
      <td>${formatDate(post.published_date) || "—"}</td>
    </tr>`).join("")}</tbody></table></div>`;
}

function pagination(page) {
  if (!page) return "";
  const start = page.total ? page.offset + 1 : 0;
  const end = Math.min(page.offset + page.limit, page.total);
  return `<div class="pagination"><button type="button" class="button button--quiet" id="blogs-prev" ${page.offset <= 0 ? "disabled" : ""}>${icon("chevron-left")}Previous</button><span>${start}–${end} of ${page.total}</span><button type="button" class="button button--quiet" id="blogs-next" ${end >= page.total ? "disabled" : ""}>Next${icon("chevron-right")}</button></div>`;
}

export function renderBlogsPanel(container, { summary, page, exportCsvUrl, exportXlsxUrl, state, onChange, onPage }) {
  if (!summary?.has_run) {
    container.innerHTML = `${summaryHeader(summary)}<div class="state-panel state-not_configured">${icon("rss")}<p><strong>No blog crawl yet.</strong><br>Run "Refresh data" above to discover and list every published post.</p></div>`;
    return;
  }
  container.innerHTML = `${summaryHeader(summary)}
    <div class="blogs-export-actions">
      <a class="button button--quiet" href="${escapeHtml(exportCsvUrl)}" download>${icon("download")}Export CSV</a>
      <a class="button button--quiet" href="${escapeHtml(exportXlsxUrl)}" download>${icon("download")}Export XLSX</a>
    </div>
    ${toolbar(state)}${postsTable(page)}${pagination(page)}`;

  let searchTimer = null;
  container.querySelector("#blogs-search")?.addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    const value = event.target.value;
    searchTimer = setTimeout(() => onChange({ blogsSearch: value }), 220);
  });
  container.querySelector("#blogs-sort")?.addEventListener("change", (event) => onChange({ blogsSort: event.target.value }));
  container.querySelector("#blogs-prev")?.addEventListener("click", () => onPage(Math.max(0, (page?.offset || 0) - (page?.limit || 25))));
  container.querySelector("#blogs-next")?.addEventListener("click", () => onPage((page?.offset || 0) + (page?.limit || 25)));
}

export function renderBlogsError(container, message) {
  container.innerHTML = `<div class="section-heading"><div><p class="eyebrow">Client Blogs</p><h2>Published posts</h2></div></div><div class="state-panel state-failed">${icon("wifi-off")}<p><strong>Failed to load blog data.</strong><br>${escapeHtml(message)}</p></div>`;
}
