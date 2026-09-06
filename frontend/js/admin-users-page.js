// Адмінка користувачів (task-71): список, створення, роль, активність,
// перевидача пароля, відкликання сесій.
//
// Головна відповідальність екрана — момент видачі пароля. Він показується
// РІВНО ОДИН раз, і сторінка має зробити цей момент неможливим проґавити:
// окрема панель, копіювання одним кліком, явне попередження.

import {
  createUser,
  listUsers,
  resetUserPassword,
  revokeUserSessions,
  updateUser,
} from "./api.js?v=20260906-t72";
import { currentUser } from "./session.js?v=20260906-t72";

const ROLES = [
  ["admin", "Administrator", "Everything, plus accounts, batch reports, imports and cloud"],
  ["member", "Member", "Daily work: collection, tools, single-client reports"],
  ["viewer", "Read-only", "Can open every page, cannot change anything"],
];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function formatDate(value) {
  if (!value) return "Never";
  return new Date(value).toLocaleString("en-GB", {
    day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function roleName(role) {
  return ROLES.find(([value]) => value === role)?.[1] || role;
}

export async function initAdminUsersPage() {
  const root = document.querySelector("#admin-users-page-root");
  if (!root) return;
  root.replaceChildren();
  root.append(buildShell());
  await refresh(root);
}

function buildShell() {
  const wrap = el("div", "admin-page");

  const head = el("header", "admin-head");
  const copy = el("div", "admin-copy");
  copy.append(el("p", "eyebrow", "Administration"));
  copy.append(el("h1", "admin-title", "People"));
  copy.append(
    el(
      "p",
      "admin-lead",
      "Accounts are issued here. The system generates the password — it is shown once and cannot be recovered, only reissued.",
    ),
  );
  head.append(copy);

  const headActions = el("div", "admin-head-actions");
  // Журнал не кладемо сьомим табом у шапку: вона вже на межі ширини, і
  // зайвий пункт виштовхує весь ряд навігації. Дві адмінські сторінки
  // натомість посилаються одна на одну.
  const auditLink = el("a", "button button--quiet", "Activity log");
  auditLink.href = "#/admin/audit";
  const addButton = el("button", "button button--primary", "Add person");
  addButton.type = "button";
  addButton.id = "admin-add-user";
  headActions.append(auditLink, addButton);
  head.append(headActions);
  wrap.append(head);

  const createPanel = el("section", "admin-create");
  createPanel.id = "admin-create-panel";
  createPanel.hidden = true;
  createPanel.append(buildCreateForm());
  wrap.append(createPanel);

  const secret = el("section", "admin-secret");
  secret.id = "admin-secret-panel";
  secret.hidden = true;
  secret.setAttribute("role", "alert");
  wrap.append(secret);

  const status = el("p", "admin-status");
  status.id = "admin-status";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  wrap.append(status);

  const list = el("div", "admin-table-wrap");
  list.id = "admin-users-list";
  wrap.append(list);

  return wrap;
}

function buildCreateForm() {
  const form = el("form", "admin-form");
  form.id = "admin-create-form";
  form.noValidate = true;

  const emailField = el("div", "admin-field");
  const emailLabel = el("label", null, "Work email");
  emailLabel.htmlFor = "admin-new-email";
  const email = document.createElement("input");
  email.id = "admin-new-email";
  email.type = "email";
  email.autocomplete = "off";
  email.required = true;
  emailField.append(emailLabel, email);

  const nameField = el("div", "admin-field");
  const nameLabel = el("label", null, "Full name");
  nameLabel.htmlFor = "admin-new-name";
  const name = document.createElement("input");
  name.id = "admin-new-name";
  name.type = "text";
  name.autocomplete = "off";
  name.required = true;
  nameField.append(nameLabel, name);

  const roleField = el("div", "admin-field");
  const roleLabelEl = el("label", null, "Role");
  roleLabelEl.htmlFor = "admin-new-role";
  const role = document.createElement("select");
  role.id = "admin-new-role";
  ROLES.forEach(([value, label, hint]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    option.title = hint;
    if (value === "member") option.selected = true;
    role.append(option);
  });
  const roleHint = el("p", "admin-hint", ROLES[1][2]);
  roleHint.id = "admin-role-hint";
  role.addEventListener("change", () => {
    roleHint.textContent = ROLES.find(([value]) => value === role.value)?.[2] || "";
  });
  roleField.append(roleLabelEl, role, roleHint);

  const error = el("p", "admin-error");
  error.id = "admin-create-error";
  error.setAttribute("role", "alert");
  error.hidden = true;

  const actions = el("div", "admin-form-actions");
  const submit = el("button", "button button--primary", "Create account");
  submit.type = "submit";
  const cancel = el("button", "button button--quiet", "Cancel");
  cancel.type = "button";
  cancel.id = "admin-create-cancel";
  actions.append(submit, cancel);

  form.append(emailField, nameField, roleField, error, actions);
  return form;
}

function showSecret(root, { user, password, reason }) {
  const panel = root.querySelector("#admin-secret-panel");
  panel.replaceChildren();
  panel.hidden = false;

  panel.append(el("p", "eyebrow", reason));
  panel.append(el("h2", "admin-secret-title", user.email));

  const row = el("div", "admin-secret-row");
  const value = el("code", "admin-secret-value", password);
  value.id = "admin-secret-value";
  const copy = el("button", "button button--primary", "Copy password");
  copy.type = "button";
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(password);
      copy.textContent = "Copied";
    } catch {
      // Clipboard API недоступний (не-https контекст, відмова в дозволі) —
      // виділяємо текст, щоб людина скопіювала руками, а не лишалась ні з чим.
      const range = document.createRange();
      range.selectNodeContents(value);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      copy.textContent = "Press Ctrl+C";
    }
  });
  row.append(value, copy);
  panel.append(row);

  panel.append(
    el(
      "p",
      "admin-secret-note",
      "Shown once. It is stored only as a hash — nobody, including administrators, can read it again. Send it to the person now; if it is lost, issue a new one.",
    ),
  );

  const dismiss = el("button", "button button--quiet", "I have saved it");
  dismiss.type = "button";
  dismiss.addEventListener("click", () => {
    panel.hidden = true;
    panel.replaceChildren();
  });
  panel.append(dismiss);
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function setStatus(root, message) {
  const status = root.querySelector("#admin-status");
  if (status) status.textContent = message || "";
}

async function refresh(root) {
  const list = root.querySelector("#admin-users-list");
  list.replaceChildren(el("p", "admin-loading", "Loading accounts…"));
  try {
    const users = await listUsers();
    list.replaceChildren(renderTable(root, users));
  } catch (error) {
    list.replaceChildren(
      el("p", "admin-error", error?.message || "Could not load accounts. Reload the page to retry."),
    );
    return;
  }
  wireCreate(root);
}

function wireCreate(root) {
  const addButton = root.querySelector("#admin-add-user");
  const panel = root.querySelector("#admin-create-panel");
  const form = root.querySelector("#admin-create-form");
  if (!addButton || addButton.dataset.wired === "true") return;
  addButton.dataset.wired = "true";

  addButton.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) root.querySelector("#admin-new-email").focus();
  });
  root.querySelector("#admin-create-cancel").addEventListener("click", () => {
    panel.hidden = true;
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = root.querySelector("#admin-create-error");
    error.hidden = true;
    const email = root.querySelector("#admin-new-email").value.trim();
    const name = root.querySelector("#admin-new-name").value.trim();
    const role = root.querySelector("#admin-new-role").value;
    if (!email || !name) {
      error.textContent = "Email and full name are both required.";
      error.hidden = false;
      return;
    }
    const submit = form.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
      const created = await createUser({ email, name, role });
      form.reset();
      panel.hidden = true;
      showSecret(root, {
        user: created.user,
        password: created.password,
        reason: "New account — password shown once",
      });
      await refresh(root);
    } catch (err) {
      error.textContent = err?.message || "Could not create the account.";
      error.hidden = false;
    } finally {
      submit.disabled = false;
    }
  });
}

function renderTable(root, users) {
  const table = el("table", "admin-table");
  const head = el("thead");
  const headRow = el("tr");
  ["Person", "Role", "State", "Sessions", "Last sign-in", "Actions"].forEach((label) => {
    const th = el("th", null, label);
    th.scope = "col";
    headRow.append(th);
  });
  head.append(headRow);
  table.append(head);

  const body = el("tbody");
  const me = currentUser();
  users.forEach((user) => body.append(renderRow(root, user, me)));
  table.append(body);
  return table;
}

function renderRow(root, user, me) {
  const row = el("tr", user.is_active ? null : "is-disabled");

  const person = el("td", "admin-person");
  person.append(el("strong", null, user.name));
  person.append(el("span", "admin-email", user.email));
  if (me && me.id === user.id) person.append(el("span", "admin-you", "You"));
  row.append(person);

  const roleCell = el("td");
  const select = document.createElement("select");
  select.className = "admin-role-select";
  select.setAttribute("aria-label", `Role for ${user.name}`);
  ROLES.forEach(([value, label]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    option.selected = value === user.role;
    select.append(option);
  });
  select.addEventListener("change", async () => {
    const previous = user.role;
    select.disabled = true;
    try {
      await updateUser(user.id, { role: select.value });
      setStatus(root, `${user.name} is now ${roleName(select.value)}.`);
      await refresh(root);
    } catch (error) {
      select.value = previous;
      setStatus(root, error?.message || "Could not change the role.");
    } finally {
      select.disabled = false;
    }
  });
  roleCell.append(select);
  row.append(roleCell);

  // Стан не покладається лише на колір (AGENTS §7): текст + окремий клас.
  row.append(el("td", "admin-state", user.is_active ? "Active" : "Disabled"));
  row.append(el("td", "admin-sessions", String(user.active_sessions)));
  row.append(el("td", "admin-last", formatDate(user.last_login_at)));

  const actions = el("td", "admin-actions");

  const reset = el("button", "button button--quiet", "New password");
  reset.type = "button";
  reset.addEventListener("click", async () => {
    reset.disabled = true;
    try {
      const result = await resetUserPassword(user.id);
      showSecret(root, {
        user: result.user,
        password: result.password,
        reason: "New password issued — shown once, all sessions signed out",
      });
      await refresh(root);
    } catch (error) {
      setStatus(root, error?.message || "Could not issue a new password.");
    } finally {
      reset.disabled = false;
    }
  });
  actions.append(reset);

  if (user.active_sessions > 0) {
    const revoke = el("button", "button button--quiet", "Sign out everywhere");
    revoke.type = "button";
    revoke.addEventListener("click", async () => {
      revoke.disabled = true;
      try {
        const result = await revokeUserSessions(user.id);
        setStatus(root, `Signed ${user.name} out of ${result.revoked} session(s).`);
        await refresh(root);
      } catch (error) {
        setStatus(root, error?.message || "Could not revoke sessions.");
      } finally {
        revoke.disabled = false;
      }
    });
    actions.append(revoke);
  }

  const toggle = el("button", "button button--quiet", user.is_active ? "Disable" : "Enable");
  toggle.type = "button";
  toggle.addEventListener("click", async () => {
    toggle.disabled = true;
    try {
      await updateUser(user.id, { is_active: !user.is_active });
      setStatus(
        root,
        user.is_active
          ? `${user.name} is disabled and signed out everywhere.`
          : `${user.name} can sign in again.`,
      );
      await refresh(root);
    } catch (error) {
      setStatus(root, error?.message || "Could not change the account state.");
    } finally {
      toggle.disabled = false;
    }
  });
  actions.append(toggle);

  row.append(actions);
  return row;
}
