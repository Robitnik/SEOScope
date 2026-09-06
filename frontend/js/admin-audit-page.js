// Журнал дій (task-72): «хто що коли зробив».
//
// Екран будується навколо одного питання оператора — «що сталося з цим
// клієнтом / що робила ця людина», — тому фільтри стоять першими, а стрічка
// вантажиться курсором: таблиця росте найшвидше в системі, і «сторінка 50»
// через OFFSET коштувала б скану всього, що перед нею.

import { auditExportUrl, getAuditFacets, listAuditEvents } from "./api.js?v=20260906-t72";

const PAGE_SIZE = 100;

const state = {
  filters: {},
  cursor: null,
  loading: false,
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function formatTime(value) {
  return new Date(value).toLocaleString("en-GB", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

// Дію показуємо як «Client · update», а не сирим `client.update`: у стрічці
// на сотні рядків саме перший сегмент дає око зачепитись.
function actionLabel(action) {
  const parts = action.split(".");
  const head = parts.shift() || action;
  const title = head.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
  return parts.length ? `${title} · ${parts.join(" ").replace(/_/g, " ")}` : title;
}

function outcomeClass(outcome) {
  if (outcome === "denied") return "audit-outcome audit-outcome--denied";
  if (outcome === "failure") return "audit-outcome audit-outcome--failure";
  return "audit-outcome audit-outcome--success";
}

export async function initAdminAuditPage(query) {
  const root = document.querySelector("#admin-audit-page-root");
  if (!root) return;
  state.filters = {};
  state.cursor = null;

  // Дозволяємо прийти сюди з посилання «History» конкретного клієнта.
  const resourceType = query?.get?.("resource_type");
  const resourceId = query?.get?.("resource_id");
  if (resourceType) state.filters.resource_type = resourceType;
  if (resourceId) state.filters.resource_id = resourceId;

  root.replaceChildren(buildShell());
  wireFilters(root);
  await Promise.all([loadFacets(root), reload(root)]);
}

function buildShell() {
  const wrap = el("div", "admin-page");

  const head = el("header", "admin-head");
  const copy = el("div", "admin-copy");
  copy.append(el("p", "eyebrow", "Administration"));
  copy.append(el("h1", "admin-title", "Activity"));
  copy.append(
    el(
      "p",
      "admin-lead",
      "Every change made through the tool, every sign-in, and every refused attempt. Read-only and append-only — nobody can edit this log.",
    ),
  );
  head.append(copy);

  const headActions = el("div", "admin-head-actions");
  const peopleLink = el("a", "button button--quiet", "People");
  peopleLink.href = "#/admin/users";
  const exportButton = el("a", "button button--quiet", "Export CSV");
  exportButton.id = "audit-export";
  exportButton.setAttribute("download", "");
  headActions.append(peopleLink, exportButton);
  head.append(headActions);
  wrap.append(head);

  wrap.append(buildFilters());

  const status = el("p", "admin-status");
  status.id = "audit-status";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  wrap.append(status);

  const tableWrap = el("div", "admin-table-wrap");
  tableWrap.id = "audit-table-wrap";
  wrap.append(tableWrap);

  const more = el("button", "button button--quiet", "Load older");
  more.type = "button";
  more.id = "audit-more";
  more.hidden = true;
  wrap.append(more);

  return wrap;
}

function buildFilters() {
  const form = el("form", "audit-filters");
  form.id = "audit-filters";

  const search = el("div", "admin-field");
  const searchLabel = el("label", null, "Search");
  searchLabel.htmlFor = "audit-q";
  const searchInput = document.createElement("input");
  searchInput.id = "audit-q";
  searchInput.type = "search";
  searchInput.placeholder = "Person, path, resource…";
  search.append(searchLabel, searchInput);

  const action = el("div", "admin-field");
  const actionLabelEl = el("label", null, "Action");
  actionLabelEl.htmlFor = "audit-action";
  const actionSelect = document.createElement("select");
  actionSelect.id = "audit-action";
  actionSelect.append(new Option("All actions", ""));
  action.append(actionLabelEl, actionSelect);

  const actor = el("div", "admin-field");
  const actorLabel = el("label", null, "Person");
  actorLabel.htmlFor = "audit-actor";
  const actorSelect = document.createElement("select");
  actorSelect.id = "audit-actor";
  actorSelect.append(new Option("Everyone", ""));
  actor.append(actorLabel, actorSelect);

  const outcome = el("div", "admin-field");
  const outcomeLabel = el("label", null, "Result");
  outcomeLabel.htmlFor = "audit-outcome";
  const outcomeSelect = document.createElement("select");
  outcomeSelect.id = "audit-outcome";
  [["", "Any result"], ["success", "Succeeded"], ["failure", "Failed"], ["denied", "Refused"]]
    .forEach(([value, label]) => outcomeSelect.append(new Option(label, value)));
  outcome.append(outcomeLabel, outcomeSelect);

  const since = el("div", "admin-field");
  const sinceLabel = el("label", null, "From");
  sinceLabel.htmlFor = "audit-since";
  const sinceInput = document.createElement("input");
  sinceInput.id = "audit-since";
  sinceInput.type = "date";
  since.append(sinceLabel, sinceInput);

  const reset = el("button", "button button--quiet", "Reset");
  reset.type = "button";
  reset.id = "audit-reset";

  form.append(search, action, actor, outcome, since, reset);
  return form;
}

async function loadFacets(root) {
  try {
    const facets = await getAuditFacets();
    const actionSelect = root.querySelector("#audit-action");
    facets.actions.forEach((item) => {
      actionSelect.append(new Option(`${actionLabel(item.action)} (${item.count})`, item.action));
    });
    const actorSelect = root.querySelector("#audit-actor");
    facets.actors.forEach((item) => {
      if (item.email) actorSelect.append(new Option(`${item.email} (${item.count})`, item.actor_id));
    });
  } catch {
    // Фасети — зручність, а не дані: без них фільтри лишаються робочими
    // (пошук і дати), тож мовчазна деградація тут доречна.
  }
}

function wireFilters(root) {
  const form = root.querySelector("#audit-filters");
  let debounce = null;

  form.addEventListener("input", (event) => {
    if (event.target.id !== "audit-q") return;
    clearTimeout(debounce);
    debounce = setTimeout(() => applyFilters(root), 300);
  });
  form.addEventListener("change", (event) => {
    if (event.target.id === "audit-q") return;
    applyFilters(root);
  });
  root.querySelector("#audit-reset").addEventListener("click", () => {
    form.reset();
    state.filters = {};
    reload(root);
  });
  root.querySelector("#audit-more").addEventListener("click", () => loadMore(root));
}

function readFilters(root) {
  const q = root.querySelector("#audit-q").value.trim();
  const action = root.querySelector("#audit-action").value;
  const actorId = root.querySelector("#audit-actor").value;
  const outcome = root.querySelector("#audit-outcome").value;
  const since = root.querySelector("#audit-since").value;
  const filters = { ...state.filters };
  delete filters.q;
  delete filters.action;
  delete filters.actor_id;
  delete filters.outcome;
  delete filters.since;
  if (q) filters.q = q;
  if (action) filters.action = action;
  if (actorId) filters.actor_id = actorId;
  if (outcome) filters.outcome = outcome;
  // Дата з <input type=date> — це локальна доба; на бекенді порівняння в UTC,
  // тож віддаємо початок дня явно, а не голий `YYYY-MM-DD`.
  if (since) filters.since = new Date(`${since}T00:00:00`).toISOString();
  return filters;
}

function applyFilters(root) {
  state.filters = readFilters(root);
  reload(root);
}

async function reload(root) {
  state.cursor = null;
  const wrap = root.querySelector("#audit-table-wrap");
  wrap.replaceChildren(el("p", "admin-loading", "Loading activity…"));
  await load(root, { replace: true });
}

async function loadMore(root) {
  await load(root, { replace: false });
}

async function load(root, { replace }) {
  if (state.loading) return;
  state.loading = true;
  const status = root.querySelector("#audit-status");
  const more = root.querySelector("#audit-more");
  try {
    const page = await listAuditEvents({
      ...state.filters,
      limit: PAGE_SIZE,
      cursor: replace ? undefined : state.cursor,
    });
    state.cursor = page.next_cursor;
    more.hidden = !page.next_cursor;

    const wrap = root.querySelector("#audit-table-wrap");
    if (replace) {
      if (!page.events.length) {
        wrap.replaceChildren(
          el("p", "admin-loading", "No activity matches these filters. Widen the date range or clear the search."),
        );
        status.textContent = "";
        root.querySelector("#audit-export").href = auditExportUrl(state.filters);
        return;
      }
      wrap.replaceChildren(buildTable(page.events));
    } else {
      const body = wrap.querySelector("tbody");
      page.events.forEach((event) => body.append(buildRow(event)));
    }
    const shown = wrap.querySelectorAll("tbody tr").length;
    status.textContent = `${shown} event${shown === 1 ? "" : "s"} shown${page.next_cursor ? ", more available" : ""}.`;
    root.querySelector("#audit-export").href = auditExportUrl(state.filters);
  } catch (error) {
    root.querySelector("#audit-table-wrap").replaceChildren(
      el("p", "admin-error", error?.message || "Could not load the activity log."),
    );
  } finally {
    state.loading = false;
  }
}

function buildTable(events) {
  const table = el("table", "admin-table audit-table");
  const head = el("thead");
  const row = el("tr");
  ["When", "Who", "Action", "Resource", "Result", ""].forEach((label) => {
    const th = el("th", null, label);
    th.scope = "col";
    row.append(th);
  });
  head.append(row);
  table.append(head);

  const body = el("tbody");
  events.forEach((event) => body.append(buildRow(event)));
  table.append(body);
  return table;
}

function buildRow(event) {
  const row = el("tr");
  row.append(el("td", "audit-when", formatTime(event.ts)));

  const who = el("td", "audit-who");
  who.append(el("strong", null, event.actor_email || event.actor_kind));
  if (event.ip) who.append(el("span", "audit-ip", event.ip));
  row.append(who);

  row.append(el("td", "audit-action", actionLabel(event.action)));
  row.append(
    el("td", "audit-resource", event.resource_label || event.resource_id || event.resource_type || "—"),
  );

  const outcome = el("td");
  // Результат не покладається лише на колір (AGENTS §7): текст + клас.
  outcome.append(el("span", outcomeClass(event.outcome), event.outcome));
  if (event.status_code) outcome.append(el("span", "audit-status-code", String(event.status_code)));
  row.append(outcome);

  const actions = el("td", "audit-details-cell");
  const hasDetails = event.details && Object.keys(event.details).length > 0;
  if (hasDetails || event.path) {
    const toggle = el("button", "button button--quiet", "Details");
    toggle.type = "button";
    const details = el("pre", "audit-details");
    details.hidden = true;
    details.textContent = JSON.stringify(
      { path: event.path, method: event.method, user_agent: event.user_agent, ...event.details },
      null,
      2,
    );
    toggle.setAttribute("aria-expanded", "false");
    toggle.addEventListener("click", () => {
      details.hidden = !details.hidden;
      toggle.setAttribute("aria-expanded", String(!details.hidden));
    });
    actions.append(toggle, details);
  }
  row.append(actions);
  return row;
}
